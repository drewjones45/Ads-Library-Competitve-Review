from __future__ import annotations

import json
from typing import Any

import anthropic

from ..config import load_settings


CLUSTER_PROMPT = """You cluster marketing ad copy into hook/theme buckets. Return STRICT JSON.

Schema:
{
  "clusters": [
    {
      "theme": "short theme label, e.g. 'problem_solution_acne'",
      "hook_style": "problem_solution | social_proof | urgency | founder_story | demo | testimonial | aesthetic | other",
      "ad_archive_ids": ["id1", "id2"],
      "exemplar": "one verbatim line that best represents the cluster"
    }
  ],
  "whitespace": ["short angle/theme the set is NOT using, but should consider"]
}

Rules:
- 3-7 clusters. Don't over-split.
- If body text is empty, skip that ad.
- Whitespace items must be plausible for the brand vertical, not generic platitudes.
"""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def cluster_hooks(ads: list[dict[str, Any]], *, model: str | None = None, vertical: str = "") -> dict[str, Any]:
    """Cluster a set of ads' body text into hook themes + surface whitespace."""
    if not ads:
        return {"clusters": [], "whitespace": []}
    settings = load_settings()
    model = model or settings.reasoning_model
    rows = []
    for a in ads:
        if not a.get("body_text"):
            continue
        rows.append({"id": a.get("ad_archive_id"), "body": a["body_text"][:500]})
    if not rows:
        return {"clusters": [], "whitespace": []}
    client = _client()
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=CLUSTER_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Vertical: {vertical or 'unknown'}\n\n"
                    f"Ads ({len(rows)} total):\n{json.dumps(rows, indent=2)}"
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
        return {"clusters": [], "whitespace": [], "error": "model returned non-JSON"}
