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


CREATIVE_TAXONOMY_PROMPT = """You are a marketing creative analyst. Examine the provided advertising image
and return STRICT JSON matching the schema below — no prose, no markdown fence.

Schema:
{
  "photography_style": "model_on_figure | flat_lay | lifestyle | studio_product_only | screenshot_ui | text_only | mixed",
  "production_style": "polished_brand | ugc_creator_style | meme_graphic | mixed",
  "product_emphasis": "product_forward | lifestyle_forward | balanced",
  "products_visible": ["short noun phrases — what specific product types are shown, e.g. 'sectional sofa', 'dining table', 'mattress', 'recliner', 'area rug'. Empty array if no products."],
  "key_features": ["enum-ish flags for notable visual elements present. Use these values:
       'price_visible', 'discount_badge', 'free_shipping_badge', 'free_gift_badge',
       'brand_logo', 'cta_button_in_image', 'countdown_timer', 'before_after',
       'star_rating_visible', 'review_quote_overlay', 'model_present', 'creator_face',
       'lifestyle_setting', 'product_close_up', 'multi_product_collage', 'video_thumbnail',
       'text_only_card', 'price_compare', 'limited_time_text', 'shipping_callout'.
     Only list features actually visible. Empty array if none apply."],
  "text_overlay": {
    "present": true | false,
    "density": "none | light | medium | heavy",
    "copy_lean": "offer_led | benefit_led | brand_led | none"
  },
  "urgency_cues": {
    "present": true | false,
    "examples": ["countdown", "ends soon", "limited stock", ...]
  },
  "value_props": ["efficacy" | "price" | "sustainability" | "inclusivity" | "convenience" | "social_proof" | "novelty" | ...],
  "hook_style": "problem_solution | social_proof | urgency | founder_story | demo | testimonial | meme | aesthetic | unknown",
  "emotional_vs_rational": "emotional | rational | mixed",
  "casting": {
    "people_visible": true | false,
    "approx_count": int,
    "diversity_signals": ["age_range_broad", "skin_tone_diverse", "single_demo", "n/a"]
  },
  "dominant_colors_hex": ["#aabbcc", ...],   // up to 5
  "aspect_ratio_guess": "1:1 | 4:5 | 9:16 | 16:9 | other",
  "logo_visible": true | false,
  "logo_brand": "string or null — the brand name shown on the logo, if any (helps detect co-branded or 3rd party ads)",
  "seasonal_tags": ["holiday", "back_to_school", "summer", "valentines", ...],
  "notable_text": "verbatim string of the largest/most prominent on-image text, or null",

  // ---- extended taxonomy (added Phase A1) ----
  "background_color": "white | black | gray | beige | brown | red | orange | yellow | green | blue | purple | pink | multi | gradient | n/a",
  "scene_description": "string or null — ≤12 words describing the scene if lifestyle (e.g. 'living room with grey sectional', 'bedroom morning light'). Null when no scene.",
  "model_gender": "male | female | mixed | ambiguous | not_visible",
  "model_demo": "string or null — brief demographic descriptor when person visible (e.g. 'woman, 30s, light skin tone'). Null if no person or detail not evident.",
  "product_in_use": "in_use | displayed_only | packaging_only | n/a",
  "before_after_present": true | false,
  "cta_verbatim_text": "string or null — exact CTA text rendered in the image (e.g. 'Shop Now', 'Save 30%'). Null if no CTA visible.",
  "creative_context": "meta_ad | brand_store | brand_store_hero | website | unknown",
  // The next four apply mainly to Amazon Brand Store layouts. Use null (not false) when not applicable.
  "hero_banner_present": true | false | null,
  "category_nav_visible": true | false | null,
  "shoppable_imagery": true | false | null,
  "product_grouping": "collection | theme | use_case | single | n/a",
  "certifications_visible": ["e.g. 'FSC', 'OEKO-TEX', 'GreenGuard'. Empty array if none."],
  "awards_or_rankings": ["e.g. '#1 best-seller', 'Editor's Choice', 'As seen on...'. Empty array if none."],

  "summary_one_line": "<= 18 words plain description of what the ad is selling and how",
  "confidence": 0.0-1.0
}

The "creative_context" hint provided by the caller (if any) tells you which type of asset this is — gate the brand-store-specific fields accordingly (return null for those four when context is meta_ad/website).

If any other field is genuinely unknowable from the image, use null or an empty array. Be specific, not generic.
"""


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
        system=CREATIVE_TAXONOMY_PROMPT,
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
