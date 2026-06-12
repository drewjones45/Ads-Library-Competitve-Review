"""Video ad analysis pipeline — download, frame-extract, transcribe, analyze.

Why this module exists
----------------------
The image analyzer treats a video poster as a still image, missing pacing,
narrative arc, voice-over claims, music mood, and brand-reveal timing. This
module sends a frame *sequence* + optional voice-over transcript to the vision
LLM in a single call, returning a video-aware JSON taxonomy.

Architectural constraint: Meta's fbcdn video URLs expire within hours, so the
mp4 download + frame extraction MUST happen during the scrape (Playwright
context still open + cookies still valid). Defer to later cron = 403 forever.

Audio transcription is OPTIONAL. If neither `OPENAI_API_KEY` nor
`INTEL_WHISPER_API_KEY` is set, the pipeline runs visual-only and tags
`transcript_quality="skipped"`. We log that fact ONCE per session.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from ..config import load_settings
from .creative import _strip_code_fence

log = logging.getLogger("intel.video")

# ---------------------------------------------------------------------------
# System-dep + API-key checks (silent failure modes that confuse users later)
# ---------------------------------------------------------------------------

# Cache the ffmpeg/ffprobe presence check so we don't hit `shutil.which` on
# every frame-extract call.
_FFMPEG_OK: bool | None = None
_FFMPEG_DETAIL: str = ""


def check_ffmpeg() -> tuple[bool, str]:
    """Return (ok, detail). Cached after first call."""
    global _FFMPEG_OK, _FFMPEG_DETAIL
    if _FFMPEG_OK is not None:
        return _FFMPEG_OK, _FFMPEG_DETAIL
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        _FFMPEG_OK = False
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        _FFMPEG_DETAIL = f"missing: {', '.join(missing)} — install via `brew install ffmpeg`"
    else:
        _FFMPEG_OK = True
        _FFMPEG_DETAIL = f"{ffmpeg}, {ffprobe}"
    return _FFMPEG_OK, _FFMPEG_DETAIL


# Module-level latch so we only log "Whisper key not set" once per session,
# even if 500 video ads stream through.
_WHISPER_SKIP_LOGGED = False


def _whisper_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("INTEL_WHISPER_API_KEY")


# Silence OpenAI SDK debug logging so auth headers can't leak.
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Download (Playwright APIRequestContext required for fbcdn cookies)
# ---------------------------------------------------------------------------

def download_video(api_request_context, url: str, dest: Path, timeout_ms: int = 30_000) -> bool:
    """Download an mp4 via Playwright's request context (inherits session cookies).

    `api_request_context` is a Playwright APIRequestContext (typically
    `browser_context.request`). A plain httpx GET will 403 on fbcdn.

    Returns True if the file was written and is non-trivially sized.
    """
    try:
        r = api_request_context.get(url, timeout=timeout_ms)
        if not r.ok:
            log.debug("video %s returned HTTP %d", url[:80], r.status)
            return False
        body = r.body()
        if len(body) < 10_000:
            log.debug("video %s body suspiciously small (%d bytes)", url[:80], len(body))
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return True
    except Exception as e:
        log.debug("video download failed %s: %s", url[:80], e)
        return False


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def probe_video(path: Path) -> dict[str, Any]:
    """Return {duration_sec, width, height, has_audio}. Empty dict on failure."""
    ok, _ = check_ffmpeg()
    if not ok or not path.exists():
        return {}
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout)
        streams = data.get("streams") or []
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0)
        return {
            "duration_sec": round(duration, 2),
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "has_audio": has_audio,
        }
    except Exception as e:
        log.debug("ffprobe failed on %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# Frame extraction (scene-change with even-spacing fallback)
# ---------------------------------------------------------------------------

_SHOWINFO_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def extract_scene_frames(
    video_path: Path, out_dir: Path, *,
    duration_sec: float | None = None,
    min_n: int = 4, max_n: int = 12,
    scene_threshold: float = 0.4,
) -> list[dict[str, Any]]:
    """Extract scene-change frames (clamped 4–12). Falls back to evenly-spaced
    if scene-detect returns too few.

    Returns [{path: str, t_sec: float, idx: int}, ...] sorted by t_sec.
    File names: `frame_NN_tMS.jpg` so timestamps are visible at the filesystem
    level without opening the sidecar.
    """
    ok, _ = check_ffmpeg()
    if not ok or not video_path.exists():
        return []
    out_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: detect scene-change timestamps via showinfo log.
    timestamps: list[float] = []
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "info",
                "-i", str(video_path),
                "-vf", f"select='gt(scene,{scene_threshold})',showinfo",
                "-vsync", "vfr", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        for line in proc.stderr.splitlines():
            m = _SHOWINFO_PTS_RE.search(line)
            if m:
                timestamps.append(float(m.group(1)))
    except Exception as e:
        log.debug("scene-detect failed on %s: %s", video_path, e)

    # Always include t=0 as the opening frame (scene-detect skips first).
    if not timestamps or timestamps[0] > 0.5:
        timestamps.insert(0, 0.0)

    # If still too few, fall back to evenly-spaced sampling.
    if len(timestamps) < min_n:
        dur = duration_sec or 0
        if not dur:
            probe = probe_video(video_path)
            dur = probe.get("duration_sec", 0)
        if dur > 0:
            step = dur / (min_n + 1)
            timestamps = [round(step * (i + 1), 2) for i in range(min_n)]

    # Cap to max_n by even-stride decimation.
    if len(timestamps) > max_n:
        stride = len(timestamps) / max_n
        timestamps = [timestamps[int(i * stride)] for i in range(max_n)]

    # Phase 2: extract one frame per timestamp.
    frames: list[dict[str, Any]] = []
    for idx, t_sec in enumerate(sorted(timestamps)):
        ms = int(round(t_sec * 1000))
        out_path = out_dir / f"frame_{idx:02d}_t{ms:05d}.jpg"
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-ss", f"{t_sec:.3f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-q:v", "3",  # ~85% quality JPEG
                    str(out_path),
                ],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000:
                frames.append({"path": str(out_path), "t_sec": round(t_sec, 3), "idx": idx})
        except Exception as e:
            log.debug("frame extract t=%.2f failed: %s", t_sec, e)
    return frames


# ---------------------------------------------------------------------------
# Audio extract + Whisper transcribe
# ---------------------------------------------------------------------------

def extract_audio(video_path: Path, out_path: Path) -> bool:
    """Mono/16kHz/32kbps mp3 — cuts Whisper cost ~3× with no transcription
    quality loss."""
    ok, _ = check_ffmpeg()
    if not ok or not video_path.exists():
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-i", str(video_path),
                "-vn", "-ac", "1", "-ar", "16000",
                "-acodec", "libmp3lame", "-b:a", "32k",
                str(out_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 200
    except Exception as e:
        log.debug("audio extract failed on %s: %s", video_path, e)
        return False


def transcribe_audio(audio_path: Path) -> tuple[str | None, str]:
    """Whisper-1 voice-over transcription. Off by default; on when
    OPENAI_API_KEY (or INTEL_WHISPER_API_KEY) is set.

    Returns (text, quality) where quality ∈ {voice, music_only, empty,
    failed, skipped}.
    """
    global _WHISPER_SKIP_LOGGED
    key = _whisper_key()
    if not key:
        if not _WHISPER_SKIP_LOGGED:
            log.info("Whisper API key not set (OPENAI_API_KEY/INTEL_WHISPER_API_KEY); "
                     "voice-over transcription disabled for this session")
            _WHISPER_SKIP_LOGGED = True
        return None, "skipped"
    if not audio_path.exists() or audio_path.stat().st_size < 200:
        return None, "empty"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        text = (resp or "").strip() if isinstance(resp, str) else (getattr(resp, "text", "") or "").strip()
        if not text:
            return "", "music_only"
        # Heuristic: very short alphanum content is usually OST/jingle artifacts.
        if len(text) < 6:
            return text, "music_only"
        return text, "voice"
    except Exception as e:
        log.warning("Whisper transcription failed for %s: %s", audio_path.name, e)
        return None, "failed"


# ---------------------------------------------------------------------------
# Top-level process_video — orchestrates the full pipeline (no LLM)
# ---------------------------------------------------------------------------

def process_video(
    *,
    api_request_context,
    video_urls: list[str],
    asset_dir: Path,
    ad_archive_id: str,
) -> dict[str, Any] | None:
    """Download + extract + transcribe a single ad's video.

    Returns the sidecar dict that gets written to `video_meta.json` (and is
    also stamped onto the ad dict for the runner), or None if no usable video
    landed.

    Idempotent: if `video.mp4` already exists, skips re-download.
    """
    ok, detail = check_ffmpeg()
    if not ok:
        log.error("video processing for ad %s skipped: %s", ad_archive_id, detail)
        return None
    if not video_urls:
        return None

    ad_dir = asset_dir / ad_archive_id
    ad_dir.mkdir(parents=True, exist_ok=True)
    video_path = ad_dir / "video.mp4"

    # Pick the first URL we can fetch. Meta sometimes returns multiple
    # qualities; the first is usually the highest-served.
    downloaded = video_path.exists() and video_path.stat().st_size > 10_000
    if not downloaded:
        for url in video_urls:
            if ".m3u8" in url:
                # HLS manifest — ffmpeg can pull it but the Playwright direct
                # download won't work. Skip for v1; defer to a future enhancement.
                log.debug("ad %s: skipping m3u8 URL (HLS not yet supported)", ad_archive_id)
                continue
            if download_video(api_request_context, url, video_path):
                downloaded = True
                break
        if not downloaded:
            log.debug("ad %s: no video URL produced a usable mp4", ad_archive_id)
            return None

    probe = probe_video(video_path)
    if not probe:
        log.debug("ad %s: ffprobe failed", ad_archive_id)
        return None

    duration = probe.get("duration_sec", 0)
    # Skip pathological cases: too long (>90s commercial cap) or too short (<2s).
    if duration < 2 or duration > 90:
        log.info("ad %s: video duration %.1fs out of bounds; skipping", ad_archive_id, duration)
        return None

    frames = extract_scene_frames(
        video_path, ad_dir,
        duration_sec=duration,
    )
    if not frames:
        log.warning("ad %s: frame extraction returned 0 frames", ad_archive_id)
        return None

    transcript_full: str | None = None
    transcript_quality = "skipped"
    if probe.get("has_audio") and _whisper_key():
        audio_path = ad_dir / "audio.mp3"
        if extract_audio(video_path, audio_path):
            transcript_full, transcript_quality = transcribe_audio(audio_path)
            try:
                audio_path.unlink()  # always delete intermediate mp3
            except Exception:
                pass

    sidecar = {
        "duration_sec": duration,
        "width": probe.get("width", 0),
        "height": probe.get("height", 0),
        "has_audio": probe.get("has_audio", False),
        "frames": frames,
        "transcript_full": transcript_full,
        "transcript_quality": transcript_quality,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_path": str(video_path),
    }
    (ad_dir / "video_meta.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return sidecar


# ---------------------------------------------------------------------------
# Vision LLM — multi-frame video analysis
# ---------------------------------------------------------------------------

# Same overall schema as CREATIVE_TAXONOMY_PROMPT but image-intrinsic fields
# are pinned to a representative_frame_t_sec the LLM picks, and a video_meta
# sub-block is added. We document the diff inline so the LLM doesn't average
# across frames (which produces noise).
VIDEO_TAXONOMY_PROMPT = """You are a marketing creative analyst. You are looking at a
SEQUENCE OF FRAMES sampled from a single video ad (in temporal order; each frame is
labeled with its timestamp), plus an optional voice-over transcript. Return STRICT
JSON matching the schema below — no prose, no markdown fence.

Schema:
{
  // ---- Static-frame fields: pin these to ONE representative frame, not
  // averaged across the whole video. Pick the frame that best represents the
  // ad's primary visual identity (often a mid-ad brand+product shot). Report
  // which frame you picked in `representative_frame_t_sec`.
  "representative_frame_t_sec": float,    // the timestamp you chose to anchor static fields
  "photography_style": "model_on_figure | flat_lay | lifestyle | studio_product_only | screenshot_ui | text_only | mixed",
  "production_style": "polished_brand | ugc_creator_style | meme_graphic | mixed",
  "product_emphasis": "product_forward | lifestyle_forward | balanced",
  "products_visible": ["short noun phrases — products seen anywhere in the spot"],
  "key_features": ["enum-ish flags. Use the same values as image analysis: 'price_visible', 'discount_badge', 'free_shipping_badge', 'free_gift_badge', 'brand_logo', 'cta_button_in_image', 'countdown_timer', 'before_after', 'star_rating_visible', 'review_quote_overlay', 'model_present', 'creator_face', 'lifestyle_setting', 'product_close_up', 'multi_product_collage', 'video_thumbnail', 'text_only_card', 'price_compare', 'limited_time_text', 'shipping_callout'. Only include features actually visible somewhere in the spot."],
  "text_overlay": {
    "present": true | false,
    "density": "none | light | medium | heavy",
    "copy_lean": "offer_led | benefit_led | brand_led | none"
  },
  "urgency_cues": {"present": true | false, "examples": ["countdown", "ends soon", ...]},
  "value_props": ["efficacy" | "price" | "sustainability" | "inclusivity" | "convenience" | "social_proof" | "novelty" | ...],
  "hook_style": "problem_solution | social_proof | urgency | founder_story | demo | testimonial | meme | aesthetic | unknown",
  "emotional_vs_rational": "emotional | rational | mixed",
  "casting": {"people_visible": true | false, "approx_count": int, "diversity_signals": [...]},
  "dominant_colors_hex": ["#aabbcc", ...],
  "aspect_ratio_guess": "1:1 | 4:5 | 9:16 | 16:9 | other",
  "logo_visible": true | false,
  "logo_brand": "string or null",
  "seasonal_tags": [...],
  "notable_text": "verbatim string of the most prominent on-image text across the spot, or null",
  "background_color": "white | black | ... | gradient | n/a",
  "scene_description": "string or null — ≤12 words describing the representative frame's setting",
  "model_gender": "male | female | mixed | ambiguous | not_visible",
  "model_demo": "string or null",
  "product_in_use": "in_use | displayed_only | packaging_only | n/a",
  "before_after_present": true | false,
  "cta_verbatim_text": "string or null — final CTA shown in the video",
  "creative_context": "meta_ad",
  "hero_banner_present": null,
  "category_nav_visible": null,
  "shoppable_imagery": null,
  "product_grouping": "collection | theme | use_case | single | n/a",
  "certifications_visible": [...],
  "awards_or_rankings": [...],

  // ---- VIDEO-SPECIFIC sub-block. These describe the spot as a whole.
  "video_meta": {
    "duration_sec": float,                       // from sidecar — echo verbatim
    "scene_count": int,                          // count distinct visual scenes you can identify
    "hook_at_seconds": float | null,             // when the attention grab happens (often t=0-2s)
    "brand_reveal_at_seconds": float | null,     // when the brand logo or name first becomes prominent
    "cta_at_seconds": float | null,              // when the CTA / "shop now" moment appears
    "pacing": "slow | medium | fast | frenetic",
    "visual_motion": "static | moderate | kinetic",
    "voice_over_summary": "string or null — ≤20 words summarizing the spoken claim. Null if no voice-over.",
    "music_mood": "upbeat | chill | tense | emotional | none | null",
    "ends_with": "logo | product | cta | person | text | other",
    "transcript_full": "string or null — paste the supplied transcript verbatim if any",
    "transcript_quality": "voice | music_only | empty | failed | skipped"
  },

  "summary_one_line": "<= 18 words plain description of what the spot is selling and how",
  "confidence": 0.0-1.0
}

If a field is genuinely unknowable from the frames + transcript, use null or an empty array.
The supplied `duration_sec` and `transcript_quality` values should be echoed back verbatim
in `video_meta` — they're authoritative.
"""


def _encode_frame(frame_path: Path) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(str(frame_path))
    mime = mime or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        mime = "image/jpeg"
    return mime, base64.b64encode(frame_path.read_bytes()).decode("ascii")


def analyze_creative_video(
    sidecar_path: str | Path,
    *,
    model: str | None = None,
    max_frames: int = 12,
) -> dict[str, Any]:
    """Run the video taxonomy on a single ad's frame sequence + transcript.

    `sidecar_path` is the path to the `video_meta.json` written by
    `process_video()`. The sidecar contains absolute paths to the frame files
    + the transcript text.

    Returns the parsed JSON dict, or `{'error': ...}` on failure.
    """
    sp = Path(sidecar_path)
    if not sp.exists():
        return {"error": f"video sidecar not found: {sp}"}
    try:
        sidecar = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"sidecar parse failed: {e}"}

    frames = (sidecar.get("frames") or [])[:max_frames]
    if not frames:
        return {"error": "no frames in sidecar"}

    settings = load_settings()
    model = model or settings.vision_model

    # Build the message content: prompt intro, then [frame, label] pairs, then
    # transcript text. Pre-compute base64 once per frame.
    content: list[dict[str, Any]] = []
    intro = (
        f"creative_context = meta_ad\n\n"
        f"This is a video ad. Duration: {sidecar.get('duration_sec', 0):.1f}s. "
        f"{len(frames)} frames follow in temporal order, each labeled with its "
        f"timestamp. After the frames, the voice-over transcript (if any) is "
        f"provided. Return JSON per the schema; bake the duration_sec and "
        f"transcript_quality values into video_meta verbatim."
    )
    content.append({"type": "text", "text": intro})
    for f in frames:
        fp = Path(f["path"])
        if not fp.exists():
            continue
        mime, b64 = _encode_frame(fp)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
        content.append({"type": "text", "text": f"^ frame at t={f['t_sec']:.2f}s"})

    transcript = sidecar.get("transcript_full")
    quality = sidecar.get("transcript_quality", "skipped")
    if transcript:
        content.append({
            "type": "text",
            "text": f"\n---\nVoice-over transcript (quality={quality}):\n{transcript}",
        })
    else:
        content.append({"type": "text", "text": f"\n---\nNo transcript (quality={quality})."})

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2500,
        system=VIDEO_TAXONOMY_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = _strip_code_fence(raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"json parse failed: {e}", "raw": raw[:500]}

    # Belt-and-suspenders: pin the sidecar's authoritative values onto the
    # video_meta block so a hallucinating LLM can't override them.
    vm = result.setdefault("video_meta", {})
    vm["duration_sec"] = sidecar.get("duration_sec", 0)
    vm["transcript_quality"] = quality
    if transcript and not vm.get("transcript_full"):
        vm["transcript_full"] = transcript
    result.setdefault("creative_context", "meta_ad")
    return result
