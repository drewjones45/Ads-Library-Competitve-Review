"""Stub LLM client for running the eval suite without an Anthropic API key.

Activated by `INTEL_LLM_STUB=1`. The eval runner monkeypatches
`anthropic.Anthropic` in the analysis modules; tasks run end-to-end through
the real graders but the model call returns a hand-authored canned response
matched to the system prompt + input.

The point isn't to fake out the LLM in production — it's to exercise the
schema, parsing, and grader plumbing for the LLM-backed tasks in environments
without an API key (CI, local-dev-without-key, Claude Code authoring sessions).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


# Toggle: set INTEL_LLM_STUB=1 to activate.
def stub_enabled() -> bool:
    return os.environ.get("INTEL_LLM_STUB") == "1"


@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Message:
    content: list[_Block]


class _StubMessages:
    def create(self, *, model: str = "", max_tokens: int = 0, system: str = "",
               messages: list | None = None, **_: Any) -> _Message:
        text = _route(system or "", messages or [])
        return _Message(content=[_Block(type="text", text=text)])


class StubClient:
    """Drop-in replacement for anthropic.Anthropic() used during eval stub runs."""
    def __init__(self, *_, **__):
        self.messages = _StubMessages()


# ---------- routing ----------

def _route(system: str, messages: list) -> str:
    """Pick a canned response based on the system prompt's distinctive header."""
    head = system.strip()[:80]
    if head.startswith("You are a marketing creative analyst"):
        return _creative_taxonomy_response(messages)
    if head.startswith("You extract marketing offers"):
        return _offers_response(messages)
    if head.startswith("You cluster marketing ad copy"):
        return _cluster_response(messages)
    if head.startswith("You are a strategist hunting for whitespace"):
        return _whitespace_response()
    if head.startswith("You are a senior marketing strategist"):
        return _briefing_response(messages)
    if head.startswith("You analyze a brand homepage"):
        return _homepage_hero_response()
    return "{}"


# ---------- per-prompt canned responses ----------

# Lifestyle bedroom scene (fixtures/images/lifestyle.jpg) — Bob's Discount Furniture
# bedroom, rattan-detail wood bed, nightstand, dresser/mirror, soft pink walls,
# lavender accents, brand logo bottom-centre.
_LIFESTYLE_TAXONOMY = {
    "photography_style": "lifestyle",
    "production_style": "polished_brand",
    "product_emphasis": "lifestyle_forward",
    "products_visible": ["bed", "nightstand", "dresser", "mirror", "table lamp"],
    "key_features": ["brand_logo", "lifestyle_setting", "multi_product_collage"],
    "text_overlay": {"present": True, "density": "light", "copy_lean": "brand_led"},
    "urgency_cues": {"present": False, "examples": []},
    "value_props": ["aesthetic"],
    "hook_style": "aesthetic",
    "emotional_vs_rational": "emotional",
    "casting": {"people_visible": False, "approx_count": 0, "diversity_signals": ["n/a"]},
    "dominant_colors_hex": ["#f3d9d6", "#c79a7e", "#dccab0", "#7a5b48"],
    "aspect_ratio_guess": "1:1",
    "logo_visible": True,
    "logo_brand": "Bob's Discount Furniture",
    "seasonal_tags": [],
    "notable_text": "BOB'S Discount Furniture · mybobs.com",
    "background_color": "pink",
    "scene_description": "Pink bedroom with rattan-wood bed, nightstand, dresser, lavender accents",
    "model_gender": "not_visible",
    "model_demo": None,
    "product_in_use": "displayed_only",
    "before_after_present": False,
    "cta_verbatim_text": None,
    "creative_context": "unknown",
    "hero_banner_present": None,
    "category_nav_visible": None,
    "shoppable_imagery": None,
    "product_grouping": "collection",
    "certifications_visible": [],
    "awards_or_rankings": [],
    "summary_one_line": "Bob's Discount Furniture lifestyle bedroom set with rattan wood and pink walls.",
    "confidence": 0.9,
}

# Low-quality / heavily blurred bedroom (fixtures/images/low_quality.jpg).
# Same scene type but indeterminate detail — the failure-mode test asserts the
# model self-reports uncertainty via confidence < 0.85.
_LOW_QUALITY_TAXONOMY = {
    "photography_style": "lifestyle",
    "production_style": "polished_brand",
    "product_emphasis": "lifestyle_forward",
    "products_visible": ["bed", "nightstand"],
    "key_features": ["lifestyle_setting"],
    "text_overlay": {"present": False, "density": "none", "copy_lean": "none"},
    "urgency_cues": {"present": False, "examples": []},
    "value_props": [],
    "hook_style": "aesthetic",
    "emotional_vs_rational": "emotional",
    "casting": {"people_visible": False, "approx_count": 0, "diversity_signals": ["n/a"]},
    "dominant_colors_hex": ["#e8d7d2", "#b89476", "#d6c5ad"],
    "aspect_ratio_guess": "1:1",
    "logo_visible": False,
    "logo_brand": None,
    "seasonal_tags": [],
    "notable_text": None,
    "background_color": "beige",
    "scene_description": "Blurred indoor scene, appears to show bedroom furniture",
    "model_gender": "not_visible",
    "model_demo": None,
    "product_in_use": "displayed_only",
    "before_after_present": False,
    "cta_verbatim_text": None,
    "creative_context": "unknown",
    "hero_banner_present": None,
    "category_nav_visible": None,
    "shoppable_imagery": None,
    "product_grouping": "n/a",
    "certifications_visible": [],
    "awards_or_rankings": [],
    "summary_one_line": "Heavily blurred indoor scene, appears to show furniture but details indiscernible.",
    "confidence": 0.45,
}


def _creative_taxonomy_response(messages: list) -> str:
    """Route between the two image fixtures by base64-image size.

    The lifestyle.jpg and low_quality.jpg fixtures differ substantially in
    file size (low-quality is smaller due to blur compression), which is a
    cheap, deterministic discriminator at the stub layer.
    """
    b64_len = _first_image_b64_len(messages)
    # Empirically lifestyle ≈ 80k+ b64 chars; low_quality ≈ 40k.
    if b64_len > 60_000:
        payload = _LIFESTYLE_TAXONOMY
    else:
        payload = _LOW_QUALITY_TAXONOMY
    # If a creative_context hint was passed via user text, echo it back.
    user_text = _first_text(messages)
    m = re.search(r"creative_context\s*=\s*(\w+)", user_text)
    if m:
        payload = dict(payload)
        payload["creative_context"] = m.group(1)
    return json.dumps(payload)


def _offers_response(messages: list) -> str:
    """R2 / website hero text → offers JSON.

    The R2 task feeds a hard-coded "Memorial Day Sale 20% off SAVE20 +
    free shipping over $50" string. We respond with the three implied offer
    rows. For any other text (e.g. website-source extraction calls), return
    an empty list — that matches the existing 'no offers extracted' state
    in the local db.
    """
    text = _first_text(messages).lower()
    offers: list[dict] = []
    pct_match = re.search(r"(\d+)\s*%\s*off", text)
    if pct_match:
        offers.append({
            "kind": "percent_off",
            "value": f"{pct_match.group(1)}%",
            "threshold": None,
            "description": f"{pct_match.group(1)}% off entire order",
            "starts_on": None, "ends_on": None,
            "confidence": 0.92,
        })
    code_match = re.search(r"\bcode\s+([a-z0-9]{3,10})", text)
    if code_match:
        offers.append({
            "kind": "code",
            "value": code_match.group(1).upper(),
            "threshold": None,
            "description": f"Promo code {code_match.group(1).upper()}",
            "starts_on": None, "ends_on": None,
            "confidence": 0.92,
        })
    if "free shipping" in text:
        thr = None
        thr_match = re.search(r"orders?\s+over\s+\$?(\d+)", text)
        if thr_match:
            thr = f"orders over ${thr_match.group(1)}"
        offers.append({
            "kind": "free_shipping",
            "value": "free shipping",
            "threshold": thr,
            "description": f"Free shipping{(' on ' + thr) if thr else ''}",
            "starts_on": None, "ends_on": None,
            "confidence": 0.9,
        })
    return json.dumps(offers)


def _cluster_response(messages: list) -> str:
    """R7 → clusters JSON. Parse ad IDs out of the user message and bucket
    them naively into 3 themes so the grader's `list_non_empty('clusters')`
    plus shape checks pass.
    """
    text = _first_text(messages)
    ids: list[str] = re.findall(r'"id":\s*"([^"]+)"', text)
    third = max(1, len(ids) // 3)
    out = {
        "clusters": [
            {
                "theme": "value_pricing_urgency",
                "hook_style": "urgency",
                "ad_archive_ids": ids[:third],
                "exemplar": "Save big — limited-time deals on bedroom + living room",
            },
            {
                "theme": "lifestyle_aesthetic",
                "hook_style": "aesthetic",
                "ad_archive_ids": ids[third:2 * third],
                "exemplar": "More styles. More ways to love your space.",
            },
            {
                "theme": "social_proof_quality",
                "hook_style": "social_proof",
                "ad_archive_ids": ids[2 * third:],
                "exemplar": "The furniture customers love — at everyday low prices",
            },
        ],
        "whitespace": [
            "founder/maker-story content — none of the set leans on this angle",
            "sustainability/responsible-sourcing messaging",
        ],
    }
    return json.dumps(out)


def _whitespace_response() -> str:
    """R8 → whitespace JSON with non-empty testable_hypothesis on first item."""
    out = {
        "whitespace": [
            {
                "angle": "AI-staged room visualization",
                "rationale": "Furniture set leans on studio + lifestyle product shots; nobody is using generative styling to let shoppers preview a room.",
                "testable_hypothesis": "If we render an empty-room photo and overlay AI-styled variations, CTR on the configurator ad will beat a static lifestyle baseline by ≥15%.",
                "effort": "medium",
                "confidence": 0.65,
            },
            {
                "angle": "Transparent supply-chain sourcing story",
                "rationale": "Value/pricing dominates the set's messaging — none lean ethical sourcing or US-made provenance.",
                "testable_hypothesis": "If we run a 'where it comes from' creative against under-35 audiences, save-rate should beat the discount-led control by ≥10%.",
                "effort": "low",
                "confidence": 0.6,
            },
            {
                "angle": "Designer-collab capsule drops",
                "rationale": "Set is dominated by everyday-low-price positioning; no limited-edition or designer-led capsules.",
                "testable_hypothesis": "A 4-week capsule with a known interior designer drives 2x media efficiency vs an unbranded promo creative.",
                "effort": "high",
                "confidence": 0.55,
            },
        ]
    }
    return json.dumps(out)


def _briefing_response(messages: list) -> str:
    """R5 / F2 → markdown briefing.

    For R5 we want to surface ≥2 `[#ad:<id>]` citations using REAL
    ad_archive_id values from the corpus JSON the runner just passed us.
    Parse the corpus, pull a handful of distinct ad ids, render them in the
    body so the citation graders find them in the seed db.

    F2 short-circuits in briefing.py before reaching the LLM (empty corpus),
    so we don't have to handle the zero-activity case here.
    """
    text = _first_text(messages)
    corpus: dict[str, Any] = {}
    # Briefing user message format (briefing.py):
    #   "Date range: ...\n\nCorpus (JSON):\n{...}\n\nWrite the briefing now."
    # Slice between the two markers, then JSON-parse.
    if "Corpus (JSON):" in text:
        body = text.split("Corpus (JSON):", 1)[1]
        if "Write the briefing now." in body:
            body = body.split("Write the briefing now.", 1)[0]
        try:
            corpus = json.loads(body.strip())
        except Exception:
            corpus = {}
    ad_ids: list[str] = []
    comp_names: list[str] = []
    offers: list[str] = []
    for cid, v in (corpus.get("competitors") or {}).items():
        comp_names.append(v.get("name") or cid)
        for ad in (v.get("new_ads") or [])[:2]:
            aid = ad.get("ad_archive_id")
            if aid and aid not in ad_ids:
                ad_ids.append(aid)
        for o in (v.get("offers") or [])[:2]:
            if o.get("kind"):
                offers.append(o["kind"])
        if len(ad_ids) >= 6:
            break

    cite = lambda aid: f"[#ad:{aid}]"
    cites = " ".join(cite(a) for a in ad_ids[:4])
    if not cites:
        # Defensive: still emit something that won't break the grader chain.
        cites = "(no ads in window)"
    competitor_list = ", ".join(comp_names[:6]) or "the tracked set"

    body = (
        "# Competitive Briefing — recent window\n\n"
        "## TL;DR\n"
        f"- {len(ad_ids)} new ads detected across {competitor_list}; sample citations: {cites}.\n"
        "- Value-pricing + urgency dominates the offer mix; aesthetic-led brand storytelling is the minority hook.\n"
        "- No competitor in the set is leaning on sustainability or designer-collab angles — open whitespace.\n\n"
        "## What changed by competitor\n"
        f"Tracked brands ({competitor_list}) shipped routine refreshes; the standout move is the volume of "
        f"new ad units across the set, e.g. {cite(ad_ids[0]) if ad_ids else ''} and "
        f"{cite(ad_ids[1]) if len(ad_ids) > 1 else ''} which carry similar copy structure.\n\n"
        "## Patterns across the set\n"
        "Hook mix skews to urgency + aesthetic; CTA mix favors `Shop Now` / `Learn More`. (inferred)\n\n"
        "## Whitespace / opportunities\n"
        "- Run a designer-collab capsule (none of the set is here).\n"
        "- Test transparent sourcing/provenance creative against under-35 audiences.\n\n"
        "## Recommended actions (next 7 days)\n"
        "- [low] Mirror the urgency-led creative format winning in the set; instrument lift vs the baseline.\n"
        "- [med] Stand up a 1-page transparent-sourcing landing test.\n"
        "- [high] Scope a designer-collab capsule for a Q3 ship.\n"
    )
    return body


def _homepage_hero_response() -> str:
    """Placeholder — not exercised by the current eval suite but kept here so
    the stub doesn't return `{}` if a brand-homepage ingest is run under stub
    mode. Returns minimal valid structured promo."""
    out = {
        "headline": "Today's promotional posture (stub)",
        "subhead": None,
        "primary_cta_text": "Shop Now",
        "primary_cta_url": None,
        "offer_claim": None,
        "offer_value": None,
        "offer_kind": None,
        "expiration": None,
        "channel_callouts": [],
        "urgency_cues": [],
        "confidence": 0.5,
    }
    return json.dumps(out)


# ---------- helpers ----------

def _first_text(messages: list) -> str:
    """Pull the first text block from the first user message."""
    if not messages:
        return ""
    m0 = messages[0]
    content = m0.get("content") if isinstance(m0, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                return str(blk.get("text") or "")
    return ""


def _first_image_b64_len(messages: list) -> int:
    """Length of the first base64-encoded image in the user message, or 0."""
    if not messages:
        return 0
    content = messages[0].get("content") if isinstance(messages[0], dict) else None
    if not isinstance(content, list):
        return 0
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "image":
            src = blk.get("source") or {}
            data = src.get("data") or ""
            return len(data)
    return 0


# ---------- patching ----------

# Modules that instantiate anthropic.Anthropic() at call time. The eval runner
# patches each module-level binding to StubClient when stub mode is on.
_PATCH_MODULES = (
    "intel.analysis.creative",
    "intel.analysis.offers",
    "intel.analysis.themes",
    "intel.analysis.homepage_hero",
    "intel.synthesis.whitespace",
    "intel.synthesis.briefing",
)


def install_stub() -> list[tuple[str, Any]]:
    """Monkeypatch `anthropic.Anthropic` to StubClient in every analyzer
    module that imports it. Returns the list of (module_name, original_attr)
    pairs so callers can restore."""
    import importlib
    saved: list[tuple[str, Any]] = []
    for name in _PATCH_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if not hasattr(mod, "anthropic"):
            continue
        original = mod.anthropic
        # Swap the entire bound `anthropic` reference for a tiny shim whose
        # `.Anthropic` attribute is the stub. Cleaner than patching globally.
        class _AnthropicShim:
            Anthropic = StubClient
        mod.anthropic = _AnthropicShim
        saved.append((name, original))
    return saved


def restore_stub(saved: list[tuple[str, Any]]) -> None:
    import importlib
    for name, original in saved:
        try:
            mod = importlib.import_module(name)
            mod.anthropic = original
        except Exception:
            pass
