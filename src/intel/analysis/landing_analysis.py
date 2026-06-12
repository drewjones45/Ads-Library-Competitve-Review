"""Dedicated landing-page analyzer.

One vision call per landing-page screenshot — the destination an ad's traffic
lands on. Unlike the ad-creative taxonomy (which describes a single ad image),
this reads the *page* as a conversion experience: above-the-fold offer, primary
CTA, what the page is asking the visitor to do, trust signals, friction, and a
strategist read of what works / what's missing. Conceptually the landing-page
sibling of the homepage `extract_hero_promo` Site Content Analysis.

Invoked from the creative-analysis batch (`analyze_pending`) when it encounters
a `landing_page_image` creative — it reads the URL + landing section from the
`landing_meta.json` sidecar next to the screenshot and calls
`analyze_landing_page`. The result is stored as the creative's `analysis_json`,
so it flows through the same store/dedup machinery as every other creative.

Returns a dict with a `summary_one_line` (used in the dashboard's "Where ads
send traffic" rows) plus the structured fields below, or `{"error": ...}` on
failure (matching `analyze_creative_image`'s contract so the batch records it).
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from ..config import load_settings

log = logging.getLogger("intel.landing_analysis")

# Landing screenshots are full-page (often 4000px+ tall). Crop to the top of the
# page before sending: the offer, headline, primary CTA and first trust band —
# what matters most for a paid-traffic destination — live above and just below
# the fold, and a giant image is both costly and downscaled to illegibility by
# the API. Mirrors the homepage hero-crop cost optimization, just taller.
_MAX_ANALYZE_HEIGHT = 2400

LANDING_SYSTEM_PROMPT = """You analyze a screenshot of a brand's LANDING PAGE — the destination a paid ad sends traffic to. You are reading the PAGE as a conversion experience, not a single ad creative.

Return STRICT JSON with these fields (use null / [] for unobserved):
{
  "page_type": "homepage | product_listing | product_detail | category | samples | lead_form | quote | store_locator | content_article | promo_landing | other | null",
  "headline": "string or null — the dominant above-the-fold headline",
  "subhead": "string or null — supporting line if present",
  "primary_cta": "string or null — the single most prominent call-to-action button/link text",
  "offer": "string or null — any promotion/discount/incentive shown (e.g. '20% off first order', 'Free samples'), else null",
  "page_intent": "string — one sentence: what is this page asking the visitor to DO? (browse products, buy a specific item, request a quote, order samples, find a store, read content)",
  "message_match": "string — does the page present a clear, focused destination consistent with its classified section, or is it a generic landing (e.g. dropped onto the homepage)? One sentence.",
  "trust_signals": ["array — reviews/ratings, guarantees, badges, financing, free-shipping, awards visible on the page"],
  "friction_points": ["array — things that would hurt conversion: long forms, unclear CTA, popup walls, no pricing, slow-looking hero, cluttered layout"],
  "what_works": ["array — 1-3 things the page does well for paid traffic"],
  "what_misses": ["array — 1-3 things the page should fix / is missing"],
  "summary_one_line": "string — a single crisp sentence summarizing the page's offer + primary ask (shown in the dashboard)",
  "confidence": 0.0-1.0
}

Rules:
- Describe only what is VISIBLE in the screenshot. Do not infer from brand knowledge.
- The page was classified into a section ("section" given below) from the ad's link — use it to judge message_match, but trust what you actually see.
- Keep arrays to the 1-3 most important items. Empty array [] if none.
- summary_one_line is required and must be specific (e.g. "Living-room category page leading with a 25%-off sitewide banner and a 'Shop Now' CTA").

Return ONLY the JSON object. No prose, no code fence."""

_ALLOWED_KEYS = {
    "page_type", "headline", "subhead", "primary_cta", "offer", "page_intent",
    "message_match", "trust_signals", "friction_points", "what_works",
    "what_misses", "summary_one_line", "confidence",
}
_LIST_KEYS = ("trust_signals", "friction_points", "what_works", "what_misses")


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _encode_screenshot(path: Path) -> tuple[str, str]:
    """Return (media_type, base64). Crop the top of the page via PIL when
    available (keeps the analyzed region legible); else send the PNG as-is."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            if h > _MAX_ANALYZE_HEIGHT:
                im = im.crop((0, 0, w, _MAX_ANALYZE_HEIGHT))
            import io
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG", optimize=True)
            return "image/png", base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        log.info("landing screenshot crop failed (%s) — sending full image", e)
        return "image/png", base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _validate(d: Any) -> dict | None:
    if not isinstance(d, dict):
        return None
    out = {k: d[k] for k in _ALLOWED_KEYS if k in d}
    for k in _LIST_KEYS:
        v = out.get(k)
        if v is None:
            out[k] = []
        elif isinstance(v, str):
            out[k] = [v]
        elif not isinstance(v, list):
            out[k] = []
    conf = out.get("confidence")
    if not isinstance(conf, (int, float)):
        out["confidence"] = 0.5
    if not out.get("summary_one_line"):
        out["summary_one_line"] = out.get("headline") or out.get("page_intent") or "(landing page)"
    return out


def analyze_landing_page(
    screenshot_path: str | Path,
    *,
    url: str,
    section: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Analyze a landing-page screenshot. Returns the structured dict described
    in LANDING_SYSTEM_PROMPT (always with `summary_one_line` + `creative_context`),
    or `{"error": ...}` on failure."""
    path = Path(screenshot_path)
    if not path.exists():
        return {"error": f"screenshot not found: {path}"}

    settings = load_settings()
    model = model or settings.vision_model

    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic SDK not installed"}
    try:
        client = anthropic.Anthropic()
    except Exception as e:
        return {"error": f"anthropic client unavailable: {e}"}

    try:
        mime, b64 = _encode_screenshot(path)
    except Exception as e:
        return {"error": f"encode failed: {type(e).__name__}: {e}"}

    user_text = (
        f"Landing page URL: {url}\n"
        f"Classified section: {section or 'unknown'}\n\n"
        "Analyze this landing page screenshot and return strict JSON per the schema."
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
        {"type": "text", "text": user_text},
    ]

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1_500,
            system=LANDING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        return {"error": f"LLM call failed: {type(e).__name__}: {e}"}

    raw = _strip_code_fence("".join(b.text for b in msg.content if b.type == "text"))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {e}: {raw[:200]}"}

    result = _validate(parsed)
    if result is None:
        return {"error": "analyzer returned a non-object"}
    # Tag context + provenance so the dashboard / gallery can recognize the shape.
    result["creative_context"] = "landing_page"
    result["landing_url"] = url
    if section:
        result["landing_section"] = section
    return result
