"""Dedicated text-ad analyzer for Google Ads Transparency Center text/responsive ads.

A Google text ad has no image — just multiple headlines and multiple descriptions.
This is the text-only sibling of `analyze_creative_image`: it classifies the ad on
the SAME attributes the image taxonomy uses (tone = emotional_vs_rational, sale via
offer_kind, hook_style, value_props) so the dashboards' heatmaps/filters stay
coherent, but BROKEN OUT per component — each headline and each description gets its
own mini-classification — plus an ad-level rollup.

Invoked from the creative-analysis batch (`analyze_pending`) when it encounters a
`text_ad` creative: it reads the headlines/descriptions from the `text_ad_meta.json`
sidecar (which IS the creative's asset_path) and calls `analyze_text_ad`. The result
is stored as the creative's `analysis_json`, flowing through the same store machinery
as every other creative. Uses the cheap text model (high volume, no vision needed).

Returns the structured dict below (always with `summary_one_line` + `creative_context`),
or `{"error": ...}` on failure (matching `analyze_creative_image`'s contract).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import load_settings

log = logging.getLogger("intel.text_ad_analysis")

TEXT_AD_SCHEMA_PROMPT = """You analyze a GOOGLE TEXT AD (a search/responsive ad — multiple headlines + multiple descriptions, no image). Classify it on the same attributes a creative analyst uses, broken out per component, then roll up to the ad level.

Return STRICT JSON with these fields (use null / [] for unobserved). Reuse the EXACT enum values given:
{
  "headlines": [
    {"text": "verbatim headline",
     "intent": "brand | offer | benefit | feature | cta | urgency | other",
     "sale_status": "on_sale | no_sale | unclear",
     "urgency": true|false}
  ],
  "descriptions": [
    {"text": "verbatim description",
     "copy_lean": "offer_led | benefit_led | brand_led | feature_led | none",
     "value_props": ["efficacy|price|sustainability|inclusivity|convenience|social_proof|novelty|quality|selection|service"],
     "urgency": true|false}
  ],
  "sale_status": "on_sale | no_sale | unclear",
  "offer_kind": "percent_off | dollar_off | bogo | free_shipping | free_gift | code | gwp | bundle | tiered | financing | none",
  "offer_value": "string or null — e.g. '20%', '$50', '0% APR for 60 months'",
  "emotional_vs_rational": "emotional | rational | mixed",
  "hook_style": "problem_solution | social_proof | urgency | founder_story | demo | testimonial | meme | aesthetic | unknown",
  "urgency_cues": {"present": true|false, "examples": ["verbatim urgency phrases"]},
  "value_props": ["deduped union of the descriptions' value_props"],
  "primary_cta": "string or null — the clearest call-to-action across the copy",
  "summary_one_line": "string <=18 words — the ad's offer + primary ask",
  "confidence": 0.0-1.0
}

Rules:
- Classify ONLY from the provided text. Do not invent offers or infer from brand knowledge.
- One object per provided headline and per provided description, in order, text copied verbatim.
- sale_status='on_sale' only if a discount/deal/sale is actually stated.
- Keep arrays tight. Empty [] if none.
- Return ONLY the JSON object. No prose, no code fence."""

_ALLOWED_KEYS = {
    "headlines", "descriptions", "sale_status", "offer_kind", "offer_value",
    "emotional_vs_rational", "hook_style", "urgency_cues", "value_props",
    "primary_cta", "summary_one_line", "confidence",
}
_LIST_KEYS = ("headlines", "descriptions", "value_props")


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _validate(d: Any, meta: dict) -> dict | None:
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
    # headlines/descriptions must be lists of dicts — drop malformed items.
    for k in ("headlines", "descriptions"):
        out[k] = [x for x in out.get(k, []) if isinstance(x, dict)]
    uc = out.get("urgency_cues")
    if not isinstance(uc, dict):
        out["urgency_cues"] = {"present": False, "examples": []}
    else:
        uc.setdefault("present", False)
        if not isinstance(uc.get("examples"), list):
            uc["examples"] = []
    conf = out.get("confidence")
    if not isinstance(conf, (int, float)):
        out["confidence"] = 0.5
    if not out.get("summary_one_line"):
        first = (meta.get("headlines") or [None])[0]
        out["summary_one_line"] = first or "(text ad)"
    return out


def analyze_text_ad(meta: dict, *, model: str | None = None) -> dict[str, Any]:
    """Classify a Google text ad from its `text_ad_meta.json` sidecar dict.
    Returns the structured dict described in TEXT_AD_SCHEMA_PROMPT (always with
    `summary_one_line` + `creative_context`), or `{"error": ...}` on failure."""
    headlines = [str(h) for h in (meta.get("headlines") or []) if str(h).strip()]
    descriptions = [str(d) for d in (meta.get("descriptions") or []) if str(d).strip()]
    if not headlines and not descriptions:
        return {"error": "empty text ad"}

    settings = load_settings()
    model = model or settings.cheap_model

    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic SDK not installed"}
    try:
        client = anthropic.Anthropic()
    except Exception as e:
        return {"error": f"anthropic client unavailable: {e}"}

    user_text = (
        "Classify this Google text ad. Return strict JSON per the schema.\n\n"
        + "HEADLINES:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + "DESCRIPTIONS:\n" + "\n".join(f"- {d}" for d in descriptions)
    )
    if meta.get("link_url"):
        user_text += f"\n\nLANDING URL: {meta['link_url']}"

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1_500,
            system=TEXT_AD_SCHEMA_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as e:
        return {"error": f"LLM call failed: {type(e).__name__}: {e}"}

    raw = _strip_code_fence("".join(b.text for b in msg.content if b.type == "text"))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {e}: {raw[:200]}"}

    result = _validate(parsed, meta)
    if result is None:
        return {"error": "analyzer returned a non-object"}
    result["creative_context"] = "google_text_ad"
    result["ad_format"] = "text"
    return result
