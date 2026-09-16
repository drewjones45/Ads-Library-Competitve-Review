"""Per-client creative taxonomies.

The creative attribute set is not universal. It was built for JD Sports and the
furniture/retail sets, where an ad sells a transaction: a product, a price, a
discount, a deadline. Applied to a B2B service advertiser it degenerates —
measured on Spectrum Reach's 48 creatives, four of thirteen scalar attributes came
back with ZERO variance (`production_style` 100% polished_brand, `logo_visible`
100% true, `before_after_present` and `urgency_cues.present` 100% false) and a
fifth was 92% one value. A constant column cannot correlate with CTR or ROAS, so
those are dead tables. Meanwhile `product_emphasis` reported "60% lifestyle
forward" for a company with no product to show, and the retail `key_features`
enum (price_visible, discount_badge, free_shipping_badge...) went entirely unused.

So a taxonomy is per client. `RETAIL` is the original set, lifted verbatim, and is
the default — every existing deployment keeps rendering byte-identically.

Selection is deliberately NOT implicit for existing clients. `CLIENT_TAXONOMY`
pins each known client, and anything unlisted falls back to RETAIL; an explicit
`name=` argument (or INTEL_TAXONOMY) overrides both. That ordering is what lets a
v2 of a client run a new taxonomy while its v1 reports, built from the same
competitor id, keep resolving to the old one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Taxonomy:
    """One client-domain's creative attribute set.

    `schema_json` is the block spliced into the vision prompt, so what the model
    is asked to emit and what the dashboard tabulates cannot drift apart — they
    are two faces of the same object.
    """
    name: str
    label: str
    schema_json: str
    guidance: str
    scalar_attrs: list[tuple[str, str]]
    nested_attrs: list[tuple[str, str]]
    list_attrs: list[tuple[str, str]]
    tag_meta: dict[str, dict[str, Any]]


_RETAIL_SCALAR = [
    ("production_style", "Production style"),
    ("photography_style", "Photography style"),
    ("product_emphasis", "Product emphasis"),
    ("hook_style", "Hook style"),
    ("emotional_vs_rational", "Emotional vs rational"),
    ("aspect_ratio_guess", "Aspect ratio"),
    ("background_color", "Background colour"),
    ("model_gender", "Model gender"),
    ("logo_visible", "Retailer logo visible"),
    ("before_after_present", "Before/after present"),
]
_RETAIL_LIST = [
    ("value_props", "Value props"),
    ("key_features", "Key features"),
    ("products_visible", "Products shown"),
    ("seasonal_tags", "Seasonal hooks"),
]
_RETAIL_NESTED = [
    ("text_overlay.density", "Text-overlay density"),
    ("text_overlay.copy_lean", "Copy lean"),
    ("urgency_cues.present", "Urgency cues present"),
    ("casting.people_visible", "People visible"),
]
_RETAIL_TAGS: dict[str, dict[str, Any]] = {
    "production_style": {
        "desc": "How the ad was produced — brand-polished vs creator-style vs meme graphic.",
        "opts": ["polished_brand", "ugc_creator_style", "meme_graphic", "mixed"],
    },
    "photography_style": {
        "desc": "How the product or subject is shot or rendered.",
        "opts": ["model_on_figure", "flat_lay", "lifestyle", "studio_product_only",
                 "screenshot_ui", "text_only", "mixed"],
    },
    "product_emphasis": {
        "desc": "Whether the frame sells the product itself or the lifestyle around it.",
        "opts": ["product_forward", "lifestyle_forward", "balanced"],
    },
    "hook_style": {
        "desc": "The persuasion device the creative leads with.",
        "opts": ["problem_solution", "social_proof", "urgency", "founder_story", "demo",
                 "testimonial", "meme", "aesthetic", "unknown"],
    },
    "emotional_vs_rational": {
        "desc": "Whether the appeal is feeling-led or reason-led.",
        "opts": ["emotional", "rational", "mixed"],
    },
    "aspect_ratio_guess": {
        "desc": "Frame shape as judged from the rendered creative.",
        "opts": ["1:1", "4:5", "9:16", "16:9", "other"],
    },
    "background_color": {
        "desc": "Dominant background treatment behind the subject.",
        "opts": ["white", "black", "gray", "beige", "brown", "red", "orange", "yellow",
                 "green", "blue", "purple", "pink", "multi", "gradient", "n/a"],
    },
    "model_gender": {
        "desc": "Presented gender of the people on screen, if any.",
        "opts": ["male", "female", "mixed", "ambiguous", "not_visible"],
    },
    "logo_visible": {
        "desc": "Whether a retailer or brand logo appears in the creative.",
        "opts": ["yes", "no"],
    },
    "before_after_present": {
        "desc": "Whether the creative shows a before/after comparison.",
        "opts": ["yes", "no"],
    },
    "text_overlay.density": {
        "desc": "How much type is burned into the creative.",
        "opts": ["none", "light", "medium", "heavy"],
    },
    "text_overlay.copy_lean": {
        "desc": "What the on-image copy leads with.",
        "opts": ["offer_led", "benefit_led", "brand_led", "none"],
    },
    "urgency_cues.present": {
        "desc": "Whether the creative uses scarcity or deadline cues.",
        "opts": ["yes", "no"],
    },
    "casting.people_visible": {
        "desc": "Whether any person appears in the creative.",
        "opts": ["yes", "no"],
    },
    "value_props": {
        "desc": "Benefits the ad argues for. Multi-select — an ad can carry several.",
        "opts": ["efficacy", "price", "sustainability", "inclusivity", "convenience",
                 "social_proof", "novelty"],
    },
    "key_features": {
        "desc": "Visual elements present in the frame. Multi-select — an ad can carry several.",
        "opts": ["price_visible", "discount_badge", "free_shipping_badge", "free_gift_badge",
                 "brand_logo", "cta_button_in_image", "countdown_timer", "before_after",
                 "star_rating_visible", "review_quote_overlay", "model_present", "creator_face",
                 "lifestyle_setting", "product_close_up", "multi_product_collage",
                 "video_thumbnail", "text_only_card", "price_compare", "limited_time_text",
                 "shipping_callout"],
    },
    "products_visible": {
        "desc": "Product types shown, as free-form noun phrases. Open vocabulary.",
        "opts": [],
    },
    "seasonal_tags": {
        "desc": "Seasonal or calendar hooks the creative leans on. Open vocabulary.",
        "opts": [],
    },
}


def _dig(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _norm(v: Any) -> str | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _pick_windows(conn: sqlite3.Connection) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """Choose the reporting window and the period to compare it against.

    `ad_performance` can hold several overlapping windows — a 90-day pull and a
    30-day one both legitimately live there. Summing across them double-counts
    every ad that appears in both, so exactly ONE window is selected as the
    reporting period: the widest window ending at the latest date_stop.

    The comparison period is then the most recent window that ends on or before
    the reporting window starts, i.e. the immediately-preceding period.
    """
    rows = conn.execute(
        "SELECT date_start, date_stop, COUNT(*) n FROM ad_performance "
        "WHERE date_start IS NOT NULL GROUP BY 1,2"
    ).fetchall()
    if not rows:
        return None, None
    wins = [(r["date_start"], r["date_stop"]) for r in rows]
    latest_stop = max(w[1] for w in wins)
    current = min((w for w in wins if w[1] == latest_stop), key=lambda w: w[0])
    prior_cands = [w for w in wins if w[1] <= current[0]]
    prior = max(prior_cands, key=lambda w: w[1]) if prior_cands else None
    return current, prior


def _fetch(conn: sqlite3.Connection, competitor_ids: list[str] | None) -> list[dict]:
    """One row per owned ad for the current reporting window, with its weekly
    series, its prior-period totals, and the vision analysis of its best
    available creative attached.

    An ad can have several creative rows (dynamic creative serves multiple
    variants, and a rendered preview sits alongside the raw asset). The preview
    is preferred when present because it is the ad as actually served; otherwise
    the first analyzed asset wins.
    """
    current, prior = _pick_windows(conn)
    where_parts, params = [], []
    if competitor_ids:
        where_parts.append(f"oa.competitor_id IN ({','.join('?' * len(competitor_ids))})")
        params.extend(competitor_ids)
    # Scope the metric join to ONE window — see _pick_windows.
    join_extra = ""
    if current:
        join_extra = " AND p.date_start = ? AND p.date_stop = ?"
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = conn.execute(f"""
        SELECT oa.platform_ad_id, oa.competitor_id, oa.account_name, oa.ad_name,
               oa.campaign_name, oa.creative_class, oa.object_type, oa.cta_type,
               oa.title, oa.body, oa.account_id,
               oa.audience_stage, oa.audience_gender, oa.audience_age,
               oa.audience_geo, oa.audience_name, oa.optimization_goal,
               oa.created_time,
               SUM(p.impressions) AS impressions, SUM(p.spend) AS spend,
               SUM(p.clicks) AS clicks, SUM(p.link_clicks) AS link_clicks,
               SUM(p.purchases) AS purchases, SUM(p.revenue) AS revenue,
               SUM(p.thruplays) AS thruplays, SUM(p.video_p100) AS video_p100,
               SUM(p.video_3s) AS video_3s, SUM(p.video_plays) AS video_plays,
               SUM(p.landing_page_views) AS landing_page_views,
               SUM(p.view_content) AS view_content,
               SUM(p.add_to_cart) AS add_to_cart,
               SUM(p.initiate_checkout) AS initiate_checkout,
               MAX(p.frequency) AS frequency,
               -- One row per ad in the scoped window, so MAX just picks that
               -- ad's blob; NULL for ads ingested before attribution existed.
               MAX(p.attribution_json) AS attribution_json
        FROM owned_ads oa
        LEFT JOIN ad_performance p
          ON p.platform_ad_id = oa.platform_ad_id{join_extra}
        {where}
        GROUP BY oa.platform_ad_id
    """, ([*( [current[0], current[1]] if current else [])] + params)).fetchall()

    # Weekly series for the sparklines, projected onto ONE canonical timeline.
    #
    # Meta only returns buckets in which an ad actually delivered, so each ad's
    # raw series has a different length and start. Summing those element-wise
    # would add week 3 of one ad to week 1 of another. Every ad is therefore
    # mapped onto the full sorted set of buckets, with 0 for weeks it did not run
    # — which is also the truthful value for those weeks.
    all_buckets = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT bucket_start FROM ad_performance_series ORDER BY 1"
        ).fetchall()
    ]
    bidx = {b: i for i, b in enumerate(all_buckets)}
    n_b = len(all_buckets)
    series_by_ad: dict[str, dict[str, list[float]]] = {}
    for s in conn.execute(
        "SELECT platform_ad_id, bucket_start, impressions, spend, clicks, "
        "  purchases, revenue, video_3s, video_plays "
        "FROM ad_performance_series"
    ).fetchall():
        i = bidx.get(s["bucket_start"])
        if i is None:
            continue
        d = series_by_ad.setdefault(s["platform_ad_id"], {
            k: [0.0] * n_b for k in ("im", "sp", "ck", "pu", "rv", "v3", "vp")
        })
        d["im"][i] = s["impressions"] or 0
        d["sp"][i] = s["spend"] or 0
        d["ck"][i] = s["clicks"] or 0
        d["pu"][i] = s["purchases"] or 0
        d["rv"][i] = s["revenue"] or 0
        d["v3"][i] = s["video_3s"] or 0
        d["vp"][i] = s["video_plays"] or 0

    ranks = _fetch_rankings(conn)
    daily, days = _fetch_daily(conn)

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["_rank"] = ranks["by_ad"].get(d["platform_ad_id"])
        d["_daily"] = daily.get(d["platform_ad_id"])
        # Prefer the rendered preview's analysis; fall back to any analyzed asset.
        cre = conn.execute("""
            SELECT c.asset_type, c.analysis_json, c.asset_path
            FROM creatives c
            JOIN owned_ads oa ON oa.ad_db_id = c.ad_id
            WHERE oa.platform_ad_id = ? AND c.analysis_json IS NOT NULL
            ORDER BY CASE c.asset_type WHEN 'ad_preview' THEN 0 ELSE 1 END, c.id
            LIMIT 1
        """, (d["platform_ad_id"],)).fetchone()
        d["analysis"] = None
        d["asset_path"] = None
        d["asset_type"] = None
        if cre:
            d["asset_path"] = cre["asset_path"]
            d["asset_type"] = cre["asset_type"]
            if cre["analysis_json"]:
                try:
                    d["analysis"] = json.loads(cre["analysis_json"])
                except json.JSONDecodeError:
                    d["analysis"] = None
        # Every asset on the ad, preview first. The card shows one thumbnail, but
        # the lightbox pages through all of them — dynamic-creative ads carry a
        # separate video_thumb per placement variant, and until this landed those
        # variants were downloaded and then never shown anywhere.
        d["gallery"] = [
            {"t": a["asset_type"], "p": a["asset_path"]}
            for a in conn.execute("""
                SELECT c.asset_type, c.asset_path FROM creatives c
                JOIN owned_ads oa ON oa.ad_db_id = c.ad_id
                WHERE oa.platform_ad_id = ? AND c.asset_path IS NOT NULL
                ORDER BY CASE c.asset_type
                           WHEN 'ad_preview' THEN 0 WHEN 'video' THEN 1 ELSE 2 END,
                         c.id
            """, (d["platform_ad_id"],)).fetchall()
        ]
        if not d["asset_path"] and d["gallery"]:
            # No analyzed creative (typically a DPA ad) — still show whatever
            # asset exists so the drill-down isn't blank.
            d["asset_path"] = d["gallery"][0]["p"]
            d["asset_type"] = d["gallery"][0]["t"]
        # An ad in owned_ads that did not deliver in this window (e.g. it only
        # ran in the comparison period) has no metrics here — drop it rather
        # than carrying a phantom zero-impression row into the tables.
        if not (d.get("impressions") or d.get("spend")):
            continue
        d["series"] = series_by_ad.get(d["platform_ad_id"])
        d["_window"] = current
        d["_prior_window"] = prior
        d["_buckets"] = all_buckets
        d["_rank_window"] = ranks["window"]
        d["_days"] = days
        out.append(d)
    return out


def _fetch_rankings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Meta's quality/engagement/conversion rankings, and the window they cover.

    These are NOT read from the main reporting window. Meta only defines them
    over recent delivery: on these accounts a 90-day query returns UNKNOWN for
    every ad (25/25 measured), while a 30-day query ranks 190 of 331. The
    dashboard therefore reads them from whichever ingested window actually has
    rankings, and reports ROAS for that same window so the two axes of the
    rating plot describe the same period.

    Returns {"window": (start, stop) | None, "by_ad": {ad_id: {...}}}.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(ad_performance)")}
    if "quality_ranking" not in have:
        return {"window": None, "by_ad": {}}

    win = conn.execute(
        "SELECT date_start, date_stop, "
        "  SUM(quality_ranking IS NOT NULL AND quality_ranking != 'UNKNOWN') ranked "
        "FROM ad_performance WHERE date_start IS NOT NULL "
        "GROUP BY date_start, date_stop HAVING ranked > 0 "
        "ORDER BY ranked DESC LIMIT 1"
    ).fetchone()
    if not win:
        return {"window": None, "by_ad": {}}

    by_ad: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT platform_ad_id, quality_ranking, engagement_rate_ranking, "
        "  conversion_rate_ranking, spend, revenue, purchases, impressions "
        "FROM ad_performance WHERE date_start = ? AND date_stop = ?",
        (win["date_start"], win["date_stop"]),
    ):
        by_ad[r["platform_ad_id"]] = {
            "q": r["quality_ranking"] or "UNKNOWN",
            "e": r["engagement_rate_ranking"] or "UNKNOWN",
            "c": r["conversion_rate_ranking"] or "UNKNOWN",
            "sp": round(r["spend"] or 0, 2),
            "rv": round(r["revenue"] or 0, 2),
            "pu": r["purchases"] or 0,
            "im": r["impressions"] or 0,
        }
    return {"window": (win["date_start"], win["date_stop"]), "by_ad": by_ad}


def _fetch_daily(conn: sqlite3.Connection) -> tuple[dict[str, list], list[str]]:
    """Per-ad daily metrics as a SPARSE series, plus the canonical day axis.

    Sparse on purpose. A dense matrix would be 623 ads x 90 days x N metrics of
    mostly zeros — a large payload shipped to the browser so the timeline can
    animate. Most ads deliver on only a handful of days, so each ad instead
    carries [dayIndex, spend, purchases, revenue, impressions, video_3s,
    video_plays] only for days it actually ran, and the browser accumulates.
    Impressions and 3-second views ride along so the scale/kill chart can judge
    on CPM and cost-per-3s-view (upper-funnel metrics), not just CPA/ROAS.
    A day absent from the list is a day with no delivery, which is exactly zero.
    """
    have = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ad_daily'"
    ).fetchone()
    if not have:
        return {}, []

    days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM ad_daily ORDER BY 1")]
    if not days:
        return {}, []
    didx = {d: i for i, d in enumerate(days)}

    out: dict[str, list] = {}
    for r in conn.execute(
        "SELECT platform_ad_id, day, spend, purchases, revenue, "
        "  impressions, video_3s, video_plays, clicks "
        "FROM ad_daily WHERE spend > 0 OR purchases > 0 ORDER BY platform_ad_id, day"
    ):
        i = didx.get(r["day"])
        if i is None:
            continue
        out.setdefault(r["platform_ad_id"], []).append([
            i, round(r["spend"] or 0, 2), round(r["purchases"] or 0),
            round(r["revenue"] or 0, 2), round(r["impressions"] or 0),
            round(r["video_3s"] or 0), round(r["video_plays"] or 0),
            round(r["clicks"] or 0),
        ])
    return out, days


def _fetch_prior(conn: sqlite3.Connection, prior: tuple[str, str] | None,
                 competitor_ids: list[str] | None) -> list[dict]:
    """The comparison period's own ad population, with the same filter facets.

    The comparison is NOT "these same ads, earlier" — most current ads simply
    did not exist in the prior period, so that framing reports every new ad as
    infinite growth and inflates every delta. Instead the prior period is
    summed over the ads that actually ran *then*, filtered by the same facets,
    which is what a period-over-period number is supposed to mean.
    """
    if not prior:
        return []
    where = ["p.date_start=?", "p.date_stop=?"]
    params: list[Any] = [prior[0], prior[1]]
    if competitor_ids:
        where.append(f"oa.competitor_id IN ({','.join('?' * len(competitor_ids))})")
        params.extend(competitor_ids)
    rows = conn.execute(f"""
        SELECT oa.competitor_id, oa.account_name, oa.creative_class,
               oa.audience_stage, oa.audience_gender, oa.audience_age, oa.audience_geo,
               SUM(p.impressions) im, SUM(p.spend) sp, SUM(p.clicks) ck,
               SUM(p.purchases) pu, SUM(p.revenue) rv,
               SUM(p.video_3s) v3, SUM(p.video_plays) vp,
               MAX(p.attribution_json) attribution_json
        FROM owned_ads oa
        JOIN ad_performance p ON p.platform_ad_id = oa.platform_ad_id
        WHERE {' AND '.join(where)}
        GROUP BY oa.platform_ad_id
    """, params).fetchall()
    return [dict(r) for r in rows]


def _bucket_stats(rows: list[dict]) -> dict[str, float]:
    """Impression-weighted aggregate for a set of ads."""
    imp = sum(r.get("impressions") or 0 for r in rows)
    spend = sum(r.get("spend") or 0 for r in rows)
    clicks = sum(r.get("clicks") or 0 for r in rows)
    lclicks = sum(r.get("link_clicks") or 0 for r in rows)
    purch = sum(r.get("purchases") or 0 for r in rows)
    rev = sum(r.get("revenue") or 0 for r in rows)
    thru = sum(r.get("thruplays") or 0 for r in rows)
    return {
        "ads": len(rows),
        "impressions": imp,
        "spend": spend,
        "clicks": clicks,
        "ctr": (100.0 * clicks / imp) if imp else 0.0,
        "link_ctr": (100.0 * lclicks / imp) if imp else 0.0,
        "cpm": (1000.0 * spend / imp) if imp else 0.0,
        "cpc": (spend / clicks) if clicks else 0.0,
        "purchases": purch,
        "revenue": rev,
        "roas": (rev / spend) if spend else 0.0,
        "cpa": (spend / purch) if purch else 0.0,
        "thruplay_rate": (100.0 * thru / imp) if imp else 0.0,
    }


# Vision analyses below this confidence are excluded from attribute rollups.
# Meta caps ad-thumbnail downloads hard — some arrive at 64x64, where nothing
# beyond rough colour is genuinely legible and the analyser correctly self-reports
# ~0.3 confidence. Letting those vote would manufacture attribute distributions
# out of unreadable pixels, which is worse than a smaller honest sample.
MIN_ANALYSIS_CONFIDENCE = 0.45


def _is_readable(r: dict) -> bool:
    a = r.get("analysis") or {}



# The retail schema block, as the vision prompt has always carried it. Kept as a
# string here so RETAIL and any new taxonomy are described the same way.
_RETAIL_SCHEMA = """{
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
}"""

_RETAIL_GUIDANCE = """The "creative_context" hint provided by the caller (if any) tells you which type of asset this is — gate the brand-store-specific fields accordingly (return null for those four when context is meta_ad/website)."""

RETAIL = Taxonomy(
    name="retail",
    label="Retail / commerce (default)",
    schema_json=_RETAIL_SCHEMA,
    guidance=_RETAIL_GUIDANCE,
    scalar_attrs=_RETAIL_SCALAR,
    nested_attrs=_RETAIL_NESTED,
    list_attrs=_RETAIL_LIST,
    tag_meta=_RETAIL_TAGS,
)


# --------------------------------------------------------------------------- B2B
# Built by reading Spectrum Reach's own highest-spend creative rather than by
# adapting the retail list. Every attribute below was observed VARYING across
# those ads; that is the entry requirement, since an attribute that does not vary
# produces a dashboard table that cannot correlate with anything.
#
# The organising insight is that a B2B service advertiser sells a considered
# purchase, not a transaction. The buyer's questions are "can I afford this", "will
# it be complicated", "will it reach the right people" — not "is this worth $120
# today". Three consequences shape the schema:
#
#   * There is no product to photograph. `product_emphasis` / `products_visible`
#     are meaningless, and the model was visibly improvising free text like
#     "TV advertising inventory" to fill them.
#   * A person in frame is ambiguous in a way retail never is. Spectrum's ads show
#     the BUYER (a small-business owner with a tablet), the AUDIENCE THE BUYER
#     WANTS TO REACH (sports fans celebrating in a living room), or nobody. Retail
#     casting fields cannot express that distinction, and it is the strategy.
#   * The ad sells access to inventory and moments. Which inventory — live MLB, NFL,
#     general TV, streaming — is the main campaign axis, and had no field at all.
_B2B_SCHEMA = """{
  "subject_depicted": "buyer_smb_owner | audience_being_reached | both | none — WHO is in frame. The buyer is the business owner being sold to (often with a laptop/tablet, in a workplace). The audience is the end consumer the buyer wants to reach (viewers, fans, shoppers). 'none' for icon/illustration/text-only creative.",
  "inventory_context": "live_sports_mlb | live_sports_nfl | live_sports_other | general_tv | streaming | multi_platform | unspecified — which media inventory the ad is selling access to.",
  "objection_handled": "cost | complexity | ad_waste | minimum_commitment | measurement_doubt | none — the buyer objection the creative pre-empts. 'cost' = price/budget barrier ('you don't need a big budget'). 'complexity' = skill/effort barrier ('no technical expertise'). 'ad_waste' = irrelevant reach ('without ad waste', 'just your local area'). 'measurement_doubt' = can't prove results.",
  "proof_device": "capability_stack | visual_metaphor | stat_or_metric | testimonial | case_example | none — how the claim is evidenced. capability_stack = a list of features summing to an outcome ('+ targeting + reporting = ROI'). visual_metaphor = a concept image standing in for the argument (carrying a ball of money).",
  "outcome_promised": "new_customers | roi_revenue | growth | awareness | ease | none — the business result the ad promises.",
  "local_emphasis": "explicit_local | implicit | none — whether local/geographic targeting is stated as a benefit ('just in your local area', 'your neighborhood').",
  "response_mechanism": "call_now_phone | learn_more_traffic | form_lead | message | none — the response the creative asks for, as rendered IN the ad (a visible phone number means call_now_phone).",
  "visual_mode": "photography_real | conceptual_metaphor | line_art_illustration | text_only_card | screen_ui | video_thumbnail | mixed — how the frame is made. Replaces retail's product-photography styles.",
  "brand_consistency": "on_brand_blue | off_palette | neutral — Spectrum's identity is a saturated blue; flag creative that departs from it.",

  "text_overlay": {
    "present": true | false,
    "density": "none | light | medium | heavy",
    "copy_lean": "offer_led | benefit_led | brand_led | objection_led | none"
  },
  "value_props": ["affordability" | "local_targeting" | "audience_quality" | "ease_of_use" | "measurement" | "premium_inventory" | "flexibility" | "expert_support" | "reach_scale" | ...],
  "hook_style": "problem_solution | objection_rebuttal | opportunity_moment | capability_demo | social_proof | aspiration | question | unknown — how the creative opens its argument. 'opportunity_moment' = a time-bound audience moment (the season, the big game).",
  "emotional_vs_rational": "emotional | rational | mixed",
  "casting": {
    "people_visible": true | false,
    "approx_count": int,
    "setting": "workplace | home | stadium_venue | retail_storefront | abstract | none",
    "diversity_signals": ["age_range_broad", "skin_tone_diverse", "single_demo", "n/a"]
  },
  "seasonal_tags": ["mlb_season", "football_season", "holiday", "back_to_school", "summer", ...],

  "dominant_colors_hex": ["#aabbcc", ...],
  "aspect_ratio_guess": "1:1 | 4:5 | 9:16 | 16:9 | other",
  "background_color": "white | black | gray | beige | brown | red | orange | yellow | green | blue | purple | pink | multi | gradient | n/a",
  "notable_text": "verbatim string of the largest/most prominent on-image text, or null",
  "cta_verbatim_text": "string or null — exact CTA text rendered in the image.",
  "creative_context": "meta_ad | website | unknown",
  "summary_one_line": "<= 18 words: what this ad sells and the argument it makes",
  "confidence": 0.0-1.0
}"""

_B2B_GUIDANCE = """This advertiser sells advertising itself — media inventory and campaign services — to
small and mid-sized businesses. It has NO physical product, so do not look for one and do not
invent one; judge the ad on who it depicts, which inventory it sells, which buyer objection it
answers, and how it evidences the claim.

The `subject_depicted` call is the one most worth getting right. Ask whose story the frame tells:
a business owner being sold to, or the consumers that business wants to reach. A person watching
television or cheering at a game is the AUDIENCE. A person working, planning, or holding a tablet
in a workplace is the BUYER."""

_B2B_SCALAR = [
    ("subject_depicted", "Who is depicted"),
    ("inventory_context", "Inventory / context sold"),
    ("objection_handled", "Buyer objection answered"),
    ("proof_device", "Proof device"),
    ("outcome_promised", "Outcome promised"),
    ("local_emphasis", "Local targeting emphasis"),
    ("response_mechanism", "Response mechanism"),
    ("visual_mode", "Visual mode"),
    ("hook_style", "Hook style"),
    ("emotional_vs_rational", "Emotional vs rational"),
    ("brand_consistency", "Brand palette"),
    ("aspect_ratio_guess", "Aspect ratio"),
    ("background_color", "Background colour"),
]
_B2B_NESTED = [
    ("text_overlay.density", "Text-overlay density"),
    ("text_overlay.copy_lean", "Copy lean"),
    ("casting.people_visible", "People visible"),
    ("casting.setting", "Setting"),
]
_B2B_LIST = [
    ("value_props", "Value props"),
    ("seasonal_tags", "Seasonal hooks"),
]

_B2B_TAGS: dict[str, dict[str, Any]] = {
    "subject_depicted": {
        "desc": "Who the frame shows: the business being sold to, or the audience that business wants to reach.",
        "opts": ["buyer_smb_owner", "audience_being_reached", "both", "none"],
    },
    "inventory_context": {
        "desc": "Which media inventory the ad sells access to.",
        "opts": ["live_sports_mlb", "live_sports_nfl", "live_sports_other", "general_tv",
                 "streaming", "multi_platform", "unspecified"],
    },
    "objection_handled": {
        "desc": "The buyer objection the creative pre-empts.",
        "opts": ["cost", "complexity", "ad_waste", "minimum_commitment", "measurement_doubt", "none"],
    },
    "proof_device": {
        "desc": "How the ad evidences its claim.",
        "opts": ["capability_stack", "visual_metaphor", "stat_or_metric", "testimonial",
                 "case_example", "none"],
    },
    "outcome_promised": {
        "desc": "The business result promised.",
        "opts": ["new_customers", "roi_revenue", "growth", "awareness", "ease", "none"],
    },
    "local_emphasis": {
        "desc": "Whether local/geographic targeting is stated as a benefit.",
        "opts": ["explicit_local", "implicit", "none"],
    },
    "response_mechanism": {
        "desc": "The response the creative asks for, as rendered in the ad.",
        "opts": ["call_now_phone", "learn_more_traffic", "form_lead", "message", "none"],
    },
    "visual_mode": {
        "desc": "How the frame is made — photograph, concept image, illustration or type.",
        "opts": ["photography_real", "conceptual_metaphor", "line_art_illustration",
                 "text_only_card", "screen_ui", "video_thumbnail", "mixed"],
    },
    "brand_consistency": {
        "desc": "Whether the creative sits in Spectrum's blue identity.",
        "opts": ["on_brand_blue", "off_palette", "neutral"],
    },
    "hook_style": {
        "desc": "How the creative opens its argument.",
        "opts": ["problem_solution", "objection_rebuttal", "opportunity_moment", "capability_demo",
                 "social_proof", "aspiration", "question", "unknown"],
    },
    "emotional_vs_rational": {
        "desc": "Whether the appeal is feeling-led or reason-led.",
        "opts": ["emotional", "rational", "mixed"],
    },
    "aspect_ratio_guess": {
        "desc": "Frame shape as judged from the rendered creative.",
        "opts": ["1:1", "4:5", "9:16", "16:9", "other"],
    },
    "background_color": {
        "desc": "Dominant background treatment behind the subject.",
        "opts": ["white", "black", "gray", "beige", "brown", "red", "orange", "yellow",
                 "green", "blue", "purple", "pink", "multi", "gradient", "n/a"],
    },
    "text_overlay.density": {
        "desc": "How much type is burned into the creative.",
        "opts": ["none", "light", "medium", "heavy"],
    },
    "text_overlay.copy_lean": {
        "desc": "What the on-image copy leads with.",
        "opts": ["offer_led", "benefit_led", "brand_led", "objection_led", "none"],
    },
    "casting.people_visible": {
        "desc": "Whether any person appears in the creative.",
        "opts": ["yes", "no"],
    },
    "casting.setting": {
        "desc": "Where the scene is set, when there is one.",
        "opts": ["workplace", "home", "stadium_venue", "retail_storefront", "abstract", "none"],
    },
    "value_props": {
        "desc": "Benefits the ad argues for. Multi-select — an ad can carry several.",
        "opts": ["affordability", "local_targeting", "audience_quality", "ease_of_use",
                 "measurement", "premium_inventory", "flexibility", "expert_support", "reach_scale"],
    },
    "seasonal_tags": {
        "desc": "Seasonal or calendar hooks the creative leans on. Open vocabulary.",
        "opts": [],
    },
}

SPECTRUM_B2B = Taxonomy(
    name="spectrum_b2b",
    label="B2B media / advertising services",
    schema_json=_B2B_SCHEMA,
    guidance=_B2B_GUIDANCE,
    scalar_attrs=_B2B_SCALAR,
    nested_attrs=_B2B_NESTED,
    list_attrs=_B2B_LIST,
    tag_meta=_B2B_TAGS,
)


# --------------------------------------------------------------------------- registry
REGISTRY: dict[str, Taxonomy] = {t.name: t for t in (RETAIL, SPECTRUM_B2B)}

# Per-client assignment. Every existing client is pinned to RETAIL *explicitly*
# rather than by omission, so that adding a taxonomy for one of them later is a
# visible one-line edit and never an accident of fallback order.
CLIENT_TAXONOMY: dict[str, str] = {
    "bobs": "retail",
    "trex": "retail",
    "revlon": "retail",
    "philo": "retail",
    "amcplus": "retail",
    "wegmans": "retail",
    "jdsports": "retail",
    "edwardjones": "retail",
    # Spectrum v1 stays on RETAIL deliberately. Its published reports were built
    # and analysed under that taxonomy, and re-rendering them under another would
    # silently change history. The B2B set is opted into per run (see resolve()).
    "spectrum": "retail",
}


def resolve(client: str | None = None, name: str | None = None) -> Taxonomy:
    """Pick a taxonomy: explicit name > INTEL_TAXONOMY > client pin > RETAIL.

    Explicit beats per-client on purpose. A v2 of an existing client runs from the
    same competitor id as its v1, so client lookup alone could not tell them apart
    — the run says which taxonomy it wants, and v1 keeps resolving to what it was
    built with.
    """
    chosen = name or os.environ.get("INTEL_TAXONOMY") or CLIENT_TAXONOMY.get(client or "", "retail")
    try:
        return REGISTRY[chosen]
    except KeyError:
        raise KeyError(
            f"unknown taxonomy {chosen!r}; available: {', '.join(sorted(REGISTRY))}"
        ) from None
