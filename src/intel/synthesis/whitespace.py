"""Whitespace / gap analysis — angles, formats, offers the competitive set is NOT using."""
from __future__ import annotations

import json
from typing import Any

import anthropic

from ..config import load_settings
from ..storage import connect


WHITESPACE_PROMPT = """You are a strategist hunting for whitespace in a competitive set.

You'll get a JSON summary of every competitor's recent creative themes, hook styles, and offers.
Identify angles, formats, value props, or offer types that are MISSING from the set — places where
no one is playing yet, that would still be plausible for the vertical.

Return STRICT JSON:
{
  "whitespace": [
    {
      "angle": "short label",
      "rationale": "why it's whitespace given the set",
      "testable_hypothesis": "If we ran X, we'd expect Y",
      "effort": "low | medium | high",
      "confidence": 0.0-1.0
    }
  ]
}

Rules: 3-6 items. No generic platitudes ('try video'). Anchor each to the gap in the corpus.
"""


def detect_whitespace(*, model: str | None = None, vertical: str | None = None) -> dict[str, Any]:
    """Pull recent creative + offers, ask Claude where the gaps are."""
    settings = load_settings()
    model = model or settings.reasoning_model
    summary: dict[str, Any] = {"competitors": {}}
    with connect() as conn:
        comps = conn.execute(
            "SELECT id, name, vertical FROM competitors" + (" WHERE vertical=?" if vertical else ""),
            (vertical,) if vertical else (),
        ).fetchall()
        for c in comps:
            entry: dict[str, Any] = {
                "name": c["name"],
                "vertical": c["vertical"],
                "ad_themes": [],
                "offer_kinds": [],
            }
            ads = conn.execute(
                "SELECT body_text, cta_type FROM ads WHERE competitor_id=? AND active=1 LIMIT 30",
                (c["id"],),
            ).fetchall()
            entry["ad_themes"] = [
                {"body": (a["body_text"] or "")[:200], "cta": a["cta_type"]} for a in ads
            ]
            offers = conn.execute(
                "SELECT kind, value, threshold FROM offers WHERE competitor_id=? "
                "ORDER BY observed_at DESC LIMIT 20",
                (c["id"],),
            ).fetchall()
            entry["offer_kinds"] = [dict(o) for o in offers]
            summary["competitors"][c["id"]] = entry

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=WHITESPACE_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Vertical: {vertical or 'mixed'}\n\n"
                    f"Competitive corpus:\n{json.dumps(summary, default=str)}"
                ),
            }
        ],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[: -3]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {"whitespace": [], "error": "model returned non-JSON", "raw": raw[:500]}
