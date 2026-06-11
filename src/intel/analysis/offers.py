from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from ..config import load_settings


OFFER_SCHEMA_PROMPT = """You extract marketing offers from competitor text. Return STRICT JSON only.

Schema (an array of offer objects):
[
  {
    "kind": "percent_off" | "dollar_off" | "bogo" | "free_shipping" | "free_gift" | "code" | "gwp" | "bundle" | "tiered",
    "value": "string — e.g. '20%', '$50', 'buy 2 get 1', 'SAVE10'",
    "threshold": "string|null — e.g. 'orders over $50', 'first purchase'",
    "description": "one-sentence plain-language description",
    "starts_on": "ISO date or null",
    "ends_on": "ISO date or null",
    "confidence": 0.0-1.0
  }
]

Rules:
- Empty array if no offer is present. Do NOT invent offers.
- Treat ambiguous phrases like 'shop now' as NOT an offer.
- One object per distinct offer (e.g. 20% off + free shipping = two objects).
- Codes mentioned in the text get kind='code' with value=the code.
"""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def extract_offers_from_text(text: str, *, model: str | None = None) -> list[dict[str, Any]]:
    """Run Claude on a text blob and return a list of offer dicts. Empty if none."""
    if not text or not text.strip():
        return []
    settings = load_settings()
    model = model or settings.cheap_model
    client = _client()
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        system=OFFER_SCHEMA_PROMPT,
        messages=[{"role": "user", "content": f"Text to analyze:\n\n{text[:6000]}"}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    # Be defensive: the model may wrap JSON in prose despite instructions.
    raw = _strip_code_fence(raw)
    try:
        offers = json.loads(raw)
        if isinstance(offers, list):
            return [o for o in offers if isinstance(o, dict) and o.get("kind")]
    except json.JSONDecodeError:
        pass
    return []


def extract_offers_from_observation(parsed: dict, *, model: str | None = None) -> list[dict[str, Any]]:
    """Concatenate the high-signal regions of a parsed page and run extraction."""
    chunks: list[str] = []
    regions = parsed.get("regions") or {}
    for region_key in ("hero", "banner_or_promo", "popup_or_modal"):
        for s in regions.get(region_key, []):
            chunks.append(f"[{region_key}] {s}")
    meta = parsed.get("meta") or {}
    for k in ("title", "description", "og_title", "og_description"):
        v = meta.get(k)
        if v:
            chunks.append(f"[meta:{k}] {v}")
    if not chunks:
        # Fall back to a slice of visible text.
        if parsed.get("visible_text_excerpt"):
            chunks.append(parsed["visible_text_excerpt"][:3000])
    text = "\n".join(chunks)
    return extract_offers_from_text(text, model=model)


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # remove first fence line
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()
