"""Structured hero/banner promotional posture extractor.

One LLM call per homepage observation against the cropped hero image (top
~1500px of the full-page screenshot). Produces a single structured record
per brand per observation describing today's promotional posture:
headline, subhead, primary CTA, claimed offer, expiration, urgency cues.

Complements but doesn't replace the existing text-based offer extractor
(`analysis/offers.py`), which produces 0..N offer rows from the page's
text regions. The hero extractor produces 0..1 unified records into
`homepage_promos` — different shape, different cardinality.

If vision is unavailable (no API key, or no screenshot), falls back to
a text-only call against `parsed["regions"]["hero"]` +
`parsed["regions"]["banner_or_promo"]`.

The runner caches by SHA-256 of the hero crop bytes; if the crop hasn't
changed since the prior observation, this module isn't called at all.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from ..config import load_settings


log = logging.getLogger("intel.homepage_hero")


HERO_SYSTEM_PROMPT = """You analyze a brand homepage's promotional posture.

Return STRICT JSON with these fields (use null for unobserved):
{
  "headline": "string or null — the dominant promotional headline (e.g. 'Up to 30% off all bedroom')",
  "subhead": "string or null — supporting line if present",
  "primary_cta_text": "string or null — exact CTA button/link text (e.g. 'Shop Now')",
  "primary_cta_url": "string or null — only if URL is rendered in image, else null",
  "offer_claim": "string or null — free-form offer description even if not parseable as a kind/value",
  "offer_value": <number or null — numeric value if extractable (e.g. 30 for '30% off', 50 for '$50 off')>,
  "offer_kind": "percent_off | dollar_off | bogo | free_shipping | free_gift | bundle | tiered | code | gwp | other | null",
  "expiration": "string or null — date or phrase (e.g. 'Ends Sunday', '2025-12-25', 'Today only')",
  "channel_callouts": ["array of channels mentioned (e.g. 'in-store', 'online', 'app')"],
  "urgency_cues": ["array of urgency phrases (e.g. 'Today only', 'Limited time', 'While supplies last', 'Last chance')"],
  "confidence": 0.0-1.0
}

Rules:
- Extract from what's VISIBLE in the image (or text). Don't infer from brand knowledge.
- If no promotional content is present (e.g. brand-storytelling hero), return all fields null with low confidence.
- `headline` must be the dominant promotional message. Generic brand taglines ("Live big. Save lots.") are headlines only if there's a discount associated; pure brand value-props go in subhead or null.
- For offer_value, return a number only. "30% off" → 30, "$50 off" → 50, "Free shipping" → null (kind is free_shipping).
- channel_callouts / urgency_cues: empty array [] if none.

Return ONLY the JSON object. No prose, no code fence."""


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _encode_image(path: Path) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/png"
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        mime = "image/png"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def _hero_text_corpus(parsed: dict) -> str:
    regions = (parsed or {}).get("regions") or {}
    hero = regions.get("hero") or []
    banner = regions.get("banner_or_promo") or []
    meta = (parsed or {}).get("meta") or {}
    parts: list[str] = []
    if meta.get("title"):
        parts.append(f"PAGE_TITLE: {meta['title']}")
    if meta.get("description"):
        parts.append(f"META_DESCRIPTION: {meta['description']}")
    if hero:
        parts.append("HERO:\n" + "\n".join(hero[:6]))
    if banner:
        parts.append("BANNER_OR_PROMO:\n" + "\n".join(banner[:8]))
    return "\n\n".join(parts).strip()


def _validate_promo_dict(d: Any) -> dict | None:
    """Drop unknown keys, coerce types, return None if not a dict."""
    if not isinstance(d, dict):
        return None
    allowed = {
        "headline", "subhead", "primary_cta_text", "primary_cta_url",
        "offer_claim", "offer_value", "offer_kind", "expiration",
        "channel_callouts", "urgency_cues", "confidence",
    }
    out = {k: d[k] for k in allowed if k in d}
    # Type coercions
    for k in ("channel_callouts", "urgency_cues"):
        v = out.get(k)
        if v is None:
            out[k] = []
        elif isinstance(v, str):
            out[k] = [v]
        elif not isinstance(v, list):
            out[k] = []
    val = out.get("offer_value")
    if isinstance(val, str):
        try:
            out["offer_value"] = float(val.replace("%", "").replace("$", "").strip())
        except Exception:
            out["offer_value"] = None
    conf = out.get("confidence")
    if not isinstance(conf, (int, float)):
        out["confidence"] = 0.5
    return out


def extract_hero_promo(parsed: dict, hero_image_path: str | None) -> dict | None:
    """Extract a structured promotional record from the homepage hero region.

    Tries vision first when `hero_image_path` exists; falls back to a text-only
    call against the parsed hero/banner regions. Returns None if no promo content
    is detectable or if the LLM call fails entirely (logged).
    """
    settings = load_settings()
    model = settings.vision_model

    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed — skipping hero extraction")
        return None

    try:
        client = anthropic.Anthropic()
    except Exception as e:
        log.info("anthropic client unavailable (%s) — skipping hero extraction", e)
        return None

    content: list[dict[str, Any]] = []
    if hero_image_path and Path(hero_image_path).exists():
        try:
            mime, b64 = _encode_image(Path(hero_image_path))
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
            mode = "vision"
        except Exception as e:
            log.warning("hero encode failed: %s — falling back to text", e)
            mode = "text"
    else:
        mode = "text"

    text_corpus = _hero_text_corpus(parsed)
    if mode == "vision":
        content.append({
            "type": "text",
            "text": (
                "Extract today's promotional posture from this brand homepage hero. "
                f"Page text excerpts for cross-reference:\n\n{text_corpus[:2_000]}\n\n"
                "Return strict JSON per the schema."
            ),
        })
    else:
        if not text_corpus:
            log.info("no hero/banner text available for text-only fallback — skipping")
            return None
        content.append({
            "type": "text",
            "text": (
                f"Extract promotional posture from these homepage text regions:\n\n{text_corpus[:4_000]}\n\n"
                "Return strict JSON per the schema."
            ),
        })

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1_200,
            system=HERO_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        log.warning("hero LLM call failed (%s)", e)
        return None

    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = _strip_code_fence(raw)
    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("hero JSON parse failed (%s): %s", e, raw[:200])
        return None

    promo = _validate_promo_dict(parsed_json)
    if not promo:
        return None

    # If literally everything is null/empty and confidence is low, treat as no-promo.
    has_signal = any(
        promo.get(k) for k in ("headline", "subhead", "primary_cta_text", "offer_claim", "offer_kind")
    )
    if not has_signal and (promo.get("confidence") or 0) < 0.3:
        return None

    promo["source_mode"] = mode
    return promo
