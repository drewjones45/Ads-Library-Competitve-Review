from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

import anthropic
from PIL import Image
import imagehash

from ..config import load_settings


def build_taxonomy_prompt(taxonomy: "Taxonomy | None" = None) -> str:
    """The vision system prompt for one taxonomy.

    The schema the model is asked to emit comes from the same Taxonomy object the
    dashboard tabulates, so the two cannot drift: adding an attribute in one place
    adds it in both.
    """
    from .taxonomies import RETAIL
    t = taxonomy or RETAIL
    return (
        "You are a marketing creative analyst. Examine the provided advertising image\n"
        "and return STRICT JSON matching the schema below — no prose, no markdown fence.\n"
        "\nSchema:\n"
        f"{t.schema_json}\n"
        f"\n{t.guidance}\n"
        "\nIf any other field is genuinely unknowable from the image, use null or an "
        "empty array. Be specific, not generic.\n"
    )


# Back-compat: the retail prompt as a module constant, unchanged for every caller
# that has not been taught about taxonomies.
CREATIVE_TAXONOMY_PROMPT = build_taxonomy_prompt()


# Default context hint when the caller doesn't supply one. Adapters that produce
# non-ad creatives (Amazon brand stores, etc.) should pass a specific context.
_CONTEXT_VALUES = {"meta_ad", "brand_store", "brand_store_hero", "website", "unknown"}


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _encode_image(path: Path) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        mime = "image/jpeg"
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def perceptual_hash(path: Path) -> str:
    """phash for visual dedup (Section 18 asset dedup)."""
    with Image.open(path) as im:
        return str(imagehash.phash(im))


def analyze_creative_image(
    image_path: str | Path,
    *,
    model: str | None = None,
    context: str = "unknown",
    taxonomy: Any = None,
) -> dict[str, Any]:
    """Run the Section-6 taxonomy on a single creative image.

    `context` is a hint about the asset's source surface (meta_ad, brand_store,
    brand_store_hero, website, unknown). The model uses this to gate the
    brand-store-specific fields appropriately.

    Returns the parsed JSON dict, or {'error': ...} on failure.
    """
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {p}"}
    if context not in _CONTEXT_VALUES:
        context = "unknown"
    settings = load_settings()
    model = model or settings.vision_model
    mime, b64 = _encode_image(p)
    client = _client()
    user_text = (
        f"creative_context = {context}\n\n"
        "Classify this creative per the schema. Bake the creative_context value "
        "into the output JSON's 'creative_context' field verbatim."
    )
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=build_taxonomy_prompt(taxonomy),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = _strip_code_fence(raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"json parse failed: {e}", "raw": raw[:500]}
    # Belt-and-suspenders: ensure creative_context is set even if the model
    # forgot to echo it back.
    if not result.get("creative_context"):
        result["creative_context"] = context
    # Attach phash so downstream can dedup re-uploads of the same image.
    try:
        result["_phash"] = perceptual_hash(p)
    except Exception:
        pass
    return result


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()
