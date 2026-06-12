"""Landing-page insights — parse, classify, and aggregate ad destination URLs.

Pure functions, no I/O. Consumers (dashboard, strategy report, CLI) load ad rows
from the DB, run `parse_url` + `classify_url` on each `link_url`, then group
via `aggregate_landing_pages`.

The classifier is rule-based and intentionally conservative. Unknown buckets
fall through to `unknown` (or `off_brand_other` for off-host URLs) rather than
being force-fit into an inappropriate category — better to under-classify and
let analysts see the long tail than to lie.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import parse_qs, urlsplit

# Params we strip from the "clean" URL but parse out as separate fields for
# the dashboard's filter axes. utm_* is industry standard; the click-id list
# covers Meta (fbclid), Google (gclid), DoubleClick (dclid), Microsoft (msclkid).
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
_CLICK_ID_KEYS = ("fbclid", "gclid", "dclid", "msclkid", "ttclid", "twclid")

# All keys we strip before producing clean_url. Anything else stays.
_STRIPPED_KEYS = set(_UTM_KEYS) | set(_CLICK_ID_KEYS) | {
    # Google Analytics / GA4 client params.
    "_ga", "_gl", "_gac",
    # Hubspot / Marketo / Pardot.
    "hsCtaTracking", "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad",
    "mkt_tok", "pi_ad_id", "pi_campaign_id",
    # Generic affiliate IDs that don't change the page.
    "ref", "referrer", "src", "source",
}

_TEMPLATE_RE = re.compile(r"\{\{([^}]+)\}\}")

# Hosts known to be measurement / redirect intermediaries. URLs landing here
# are not the brand's "real" destination — they're click-tracking pixels that
# 302 to the real landing page (which we can't see without following).
_TRACKER_HOSTS = frozenset({
    "ad.doubleclick.net",
    "doubleclick.net",
    "googleadservices.com",
    "googletagmanager.com",
    "l.facebook.com",
    "lm.facebook.com",
    "click.linksynergy.com",
    "go.skimresources.com",
    "redirect.viglink.com",
})

# Short-link shorteners. Hosts here are treated as opaque redirects when not
# on the brand's own domain. (Bit.ly etc. would land here if seen.)
_SHORTLINK_HOSTS = frozenset({
    "bit.ly", "tinyurl.com", "ow.ly", "buff.ly", "t.co", "lnk.bio",
    "fb.me", "shorturl.at",
})

# ---------------------------------------------------------------------------
# parse_url
# ---------------------------------------------------------------------------

def parse_url(raw: str | None) -> dict[str, Any]:
    """Decompose a raw ad destination URL into normalized parts + parsed
    tracking params. Returns the canonical dict shape consumed by classify_url
    and aggregate_landing_pages. Returns a "blank" dict for None / unparseable
    input rather than raising — the caller usually has hundreds of ads to
    process and one bad row shouldn't crash the run.
    """
    blank = {
        "raw": raw or "",
        "scheme": None, "host": "", "path": "",
        "first_segment": "",
        "clean_url": "",
        "query_params": {},
        "utm": {k: None for k in _UTM_KEYS},
        "click_ids": {k: None for k in _CLICK_ID_KEYS},
        "has_template": False,
        "template_keys": [],
    }
    if not raw or not isinstance(raw, str):
        return blank

    has_template = "{{" in raw
    template_keys = []
    if has_template:
        template_keys = [m.group(1).strip() for m in _TEMPLATE_RE.finditer(raw)]

    try:
        # `urlsplit` is tolerant of weird inputs that `urlparse` chokes on.
        # If the URL has no scheme, treat it as host-only (typical of bare
        # short-links like "www.fixbv.com" we see in RTG's corpus).
        s = urlsplit(raw if "://" in raw else f"https://{raw}")
    except Exception:
        return {**blank, "raw": raw, "has_template": has_template, "template_keys": template_keys}

    host = (s.hostname or "").lower()
    # Strip 'www.' for matching consistency — the brand-host check ignores it
    # and so should the classifier.
    if host.startswith("www."):
        host = host[4:]

    # Trim trailing slash on path EXCEPT when the path is empty/'/'-only.
    path = s.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    # First path segment — "products" for "/products/decking/x", "" for "/".
    first_segment = ""
    if path and path != "/":
        first_segment = path.lstrip("/").split("/", 1)[0].lower()

    # Parse query params (keep first occurrence of repeated keys for simplicity).
    query_params: dict[str, str] = {}
    if s.query:
        for k, vs in parse_qs(s.query, keep_blank_values=True).items():
            query_params[k] = vs[0] if vs else ""

    utm = {k: query_params.get(k) for k in _UTM_KEYS}
    click_ids = {k: query_params.get(k) for k in _CLICK_ID_KEYS}

    # Build clean_url = scheme://host/path (no query, no fragment).
    scheme = s.scheme or "https"
    # If we synthesized https:// above for a bare host, keep the synthesized
    # scheme but flag in `raw` that the input was bare.
    clean_url = f"{scheme}://{host}{path}" if host else ""

    return {
        "raw": raw,
        "scheme": s.scheme or None,
        "host": host,
        "path": path,
        "first_segment": first_segment,
        "clean_url": clean_url,
        "query_params": query_params,
        "utm": utm,
        "click_ids": click_ids,
        "has_template": has_template,
        "template_keys": template_keys,
    }


# ---------------------------------------------------------------------------
# classify_url
# ---------------------------------------------------------------------------
#
# Pattern catalog — one regex per bucket, applied in priority order. The first
# match wins. Ordering matters: e.g., we test `product_detail` (most specific)
# before `product_browse` (more general).

_PRODUCT_DETAIL_RE = re.compile(
    r"(?:^|/)(?:p|product|dp|item)/[^/]+", re.I
)
_PRODUCT_BROWSE_RE = re.compile(
    r"(?:^|/)(?:products?|shop[a-z0-9.-]*|collections?|category|categories|c|catalog)(?:/|$)", re.I
)
_WHERE_TO_BUY_RE = re.compile(
    r"(?:^|/)(?:find-(?:a-)?(?:pro|builder|dealer|store|contractor|retailer)|"
    r"find-trex|stores?|dealers?|retailers?|where-to-buy|locate-(?:a-)?dealer)(?:/|$)",
    re.I,
)
_SAMPLES_RE = re.compile(
    r"(?:^|/)(?:order-samples?|free-samples?|samples?|shop-samples?)(?:/|$)", re.I
)
_QUOTE_LEAD_RE = re.compile(
    r"(?:^|/)(?:request-(?:a-)?quote|get-(?:a-)?quote|talk-to-an?-expert|"
    r"contact|start-your-(?:deck-)?(?:journey|project)|consult(?:ation)?|estimate|"
    r"build-your-(?:deck|home)|plan-your-(?:deck|home|project))(?:/|$)",
    re.I,
)
_INSPIRATION_RE = re.compile(
    r"(?:^|/)(?:blog|inspiration|deck-ideas?|look-?book|design-guide|articles?|"
    r"how-to|guides?|stories|tips|outdoor-living-(?:blog|source-?book)|magazine|"
    r"what-to-know|deck-design-(?:guide|tips)|plans?)(?:/|$)",
    re.I,
)
_BRAND_STORY_RE = re.compile(
    r"(?:^|/)(?:why-[a-z0-9-]+|about(?:-us)?|sustainability|history|"
    r"vs-(?:the-)?competition|our-(?:story|innovation|history|process)|"
    r"trex-vs-(?:the-)?competition|trusted|testimonials|why-choose-[a-z0-9-]+)(?:/|$)",
    re.I,
)


def classify_url(parsed: dict, brand_host: str = "") -> str:
    """Bucket a parsed URL into a single classification string. See the module
    docstring for the full taxonomy. `brand_host` is the canonical lowercased
    host (without `www.`) for the competitor being analyzed — required to
    distinguish on-brand vs off-brand traffic correctly.
    """
    if not parsed or not isinstance(parsed, dict) or not parsed.get("raw"):
        return "unknown"

    # Template placeholders win regardless of host (a URL that *would have*
    # been on-brand if Meta had filled in the dynamic substitution).
    if parsed.get("has_template"):
        return "template_unfilled"

    host = (parsed.get("host") or "").lower()
    brand_host = (brand_host or "").lower().lstrip(".")
    if brand_host.startswith("www."):
        brand_host = brand_host[4:]

    # Tracker / shortlink off-brand: explicit list wins.
    if host in _TRACKER_HOSTS:
        return "off_brand_tracker"
    if host in _SHORTLINK_HOSTS:
        return "off_brand_short"

    # Brand-affiliated dealer network subdomains — e.g. trexdealers.com,
    # {brand}dealers.com, {brand}retailers.com, {brand}stores.com. These are
    # off-brand by host but the marketer's intent is "go buy this near you,"
    # so classify as where_to_buy rather than burying as off-brand-other.
    if host and any(
        host.endswith(suffix)
        for suffix in ("dealers.com", "retailers.com", "stores.com", "locations.com", "showrooms.com")
    ):
        return "where_to_buy"

    # On-brand?
    is_on_brand = bool(brand_host) and (host == brand_host or host.endswith("." + brand_host))
    if not is_on_brand:
        # Off-brand — but might still be a dealer subdomain we care to bucket.
        # Heuristic: "*.dealers.trex.com" → off-brand-other; "fixbv.com" → short.
        path = parsed.get("path") or "/"
        path_segs = [p for p in path.split("/") if p]
        if len(path_segs) <= 1 and len(host.split(".")) <= 3:
            # Opaque short host with shallow path — likely a redirect.
            return "off_brand_short"
        return "off_brand_other"

    # On-brand: dispatch by path patterns. Priority order matters.
    path = parsed.get("path") or "/"

    if path in ("", "/"):
        return "homepage"
    if _PRODUCT_DETAIL_RE.search(path):
        return "product_detail"
    if _PRODUCT_BROWSE_RE.search(path):
        return "product_browse"
    if _WHERE_TO_BUY_RE.search(path):
        return "where_to_buy"
    if _SAMPLES_RE.search(path):
        return "samples"
    if _QUOTE_LEAD_RE.search(path):
        return "quote_lead"
    if _INSPIRATION_RE.search(path):
        return "inspiration_content"
    if _BRAND_STORY_RE.search(path):
        return "brand_story"
    return "unknown"


# ---------------------------------------------------------------------------
# brand_host_for
# ---------------------------------------------------------------------------

def brand_host_for(competitor: Any) -> str:
    """Pull the canonical host (lowercased, www-stripped) from the competitor's
    `website` source. Returns "" when the brand has no website source — those
    brands will see all of their links classified as off-brand (which is
    accurate; we can't tell on-brand from off-brand without a reference host).
    """
    if not competitor or not getattr(competitor, "sources", None):
        return ""
    for s in competitor.sources:
        if s.type == "website" and s.url:
            parsed = parse_url(s.url)
            return parsed.get("host") or ""
    return ""


def detect_brand_host(link_urls: list[str | None]) -> str:
    """Heuristically derive the brand's canonical host from its ad corpus when
    the YAML competitor config isn't available (e.g., when the strategy report
    is rendered outside the right INTEL_COMPETITORS_FILE context). The most-
    common registrable domain across non-tracker, non-template URLs wins. The
    `*dealers.com` / `*retailers.com` family is excluded — those subdomains
    are themselves the off-brand-but-intentional bucket we want to classify
    separately, so they shouldn't pin the brand host."""
    counter: Counter = Counter()
    for raw in link_urls:
        parsed = parse_url(raw)
        host = parsed.get("host") or ""
        if not host or parsed.get("has_template"):
            continue
        if host in _TRACKER_HOSTS or host in _SHORTLINK_HOSTS:
            continue
        # Strip the dealer-network subdomain family so it doesn't win.
        if any(host.endswith(suffix) for suffix in (
            "dealers.com", "retailers.com", "stores.com",
            "locations.com", "showrooms.com",
        )):
            continue
        parts = host.split(".")
        if len(parts) >= 2:
            # Last 2 labels = the registrable domain in the common case
            # (good enough for .com, .net, .co — not bulletproof for .co.uk
            # but the brands in this corpus don't use multi-part TLDs).
            domain = ".".join(parts[-2:])
            counter[domain] += 1
    if not counter:
        return ""
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# aggregate_landing_pages
# ---------------------------------------------------------------------------

# Sections rendered at the END of the bar/table regardless of their count.
# These are findings to flag, not traffic worth applauding.
_FLAGGED_SECTIONS = ("template_unfilled", "off_brand_tracker", "off_brand_short", "off_brand_other")

# Default display order for sections — funnel-narrative friendly. Sections
# not in this list fall through alphabetically at the end (minus flagged).
_SECTION_ORDER = (
    "homepage",
    "product_browse",
    "product_detail",
    "samples",
    "quote_lead",
    "where_to_buy",
    "inspiration_content",
    "brand_story",
    "unknown",
)


def _section_sort_key(section: str, ad_count: int) -> tuple:
    """Sort sections by funnel order, then by ad_count desc. Flagged sections
    are pushed to the end regardless of count."""
    if section in _FLAGGED_SECTIONS:
        return (2, _FLAGGED_SECTIONS.index(section), -ad_count)
    if section in _SECTION_ORDER:
        return (0, _SECTION_ORDER.index(section), -ad_count)
    return (1, section, -ad_count)


def aggregate_landing_pages(rows: list[dict], brand_host: str = "") -> dict[str, Any]:
    """Group enriched ad rows (with `parsed`, `section`, `popularity_score`
    already attached) into a per-section breakdown ready for rendering.

    Each row is expected to look like the output of:
        {**dict(ad_row), **parse_url(ad_row["link_url"]), "section": classify_url(...),
         "popularity_score": float}

    Returns the dict described in the plan: total_ads, with_link, on_brand_share,
    by_section (ordered list).
    """
    total_ads = len(rows)
    rows_with_link = [r for r in rows if r.get("clean_url")]
    with_link = len(rows_with_link)

    # On-brand share — count rows where the host matches brand_host.
    if brand_host and with_link:
        brand_host_norm = brand_host.lower().lstrip(".")
        if brand_host_norm.startswith("www."):
            brand_host_norm = brand_host_norm[4:]
        on_brand = sum(
            1 for r in rows_with_link
            if (r.get("host") or "") == brand_host_norm
            or (r.get("host") or "").endswith("." + brand_host_norm)
        )
        on_brand_share = on_brand / with_link
    else:
        on_brand_share = 0.0

    # Group by section.
    by_section_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows_with_link:
        section = r.get("section") or "unknown"
        by_section_buckets[section].append(r)

    total_pop = sum(r.get("popularity_score") or 0 for r in rows_with_link) or 1.0
    out_sections = []
    for section, rs in by_section_buckets.items():
        ad_count = len(rs)
        ad_share = ad_count / with_link if with_link else 0.0
        pop_weight = sum(r.get("popularity_score") or 0 for r in rs)
        pop_share = pop_weight / total_pop if total_pop else 0.0
        # Top destinations within this section, by ad-count.
        url_counter: Counter = Counter()
        url_examples: dict[str, list[str]] = defaultdict(list)
        for r in rs:
            cu = r.get("clean_url") or ""
            url_counter[cu] += 1
            if r.get("ad_archive_id"):
                if len(url_examples[cu]) < 3:
                    url_examples[cu].append(r["ad_archive_id"])
        top_urls = []
        for url, cnt in url_counter.most_common(5):
            top_urls.append({
                "clean_url": url,
                "ad_count": cnt,
                "ad_archive_ids": url_examples[url],
            })
        # Example raw URLs (verbatim, for investigations).
        example_raw_urls = []
        seen_raw = set()
        for r in rs:
            raw = r.get("raw") or ""
            if raw and raw not in seen_raw:
                example_raw_urls.append(raw)
                seen_raw.add(raw)
            if len(example_raw_urls) >= 3:
                break
        out_sections.append({
            "section": section,
            "ad_count": ad_count,
            "ad_share": ad_share,
            "popularity_weight": pop_weight,
            "popularity_share": pop_share,
            "top_urls": top_urls,
            "example_raw_urls": example_raw_urls,
        })

    # Final sort: funnel order then ad-count desc.
    out_sections.sort(key=lambda s: _section_sort_key(s["section"], s["ad_count"]))

    return {
        "total_ads": total_ads,
        "with_link": with_link,
        "on_brand_share": on_brand_share,
        "by_section": out_sections,
        "brand_host": brand_host,
    }


# Public palette — kept here so the dashboard and strategy report import a
# single source of truth.
SECTION_PALETTE: dict[str, str] = {
    "homepage":             "#6c757d",  # grey
    "product_browse":       "#2a6c3a",  # green family
    "product_detail":       "#1e7e34",  # darker green
    "samples":              "#7a4a1c",  # amber
    "quote_lead":           "#a06030",  # darker amber
    "where_to_buy":         "#2a5fb0",  # blue
    "inspiration_content":  "#5e3a8a",  # purple
    "brand_story":          "#854a99",  # lighter purple
    "template_unfilled":    "#e08e00",  # orange (warning)
    "off_brand_tracker":    "#d9534f",  # red
    "off_brand_short":      "#c0392b",  # darker red
    "off_brand_other":      "#a93226",  # darkest red
    "unknown":              "#9a9a9a",  # light grey
}

# Human-readable labels for the display layer.
SECTION_LABELS: dict[str, str] = {
    "homepage":             "Homepage",
    "product_browse":       "Product browse",
    "product_detail":       "Product detail",
    "samples":              "Samples",
    "quote_lead":           "Quote / lead capture",
    "where_to_buy":         "Where to buy",
    "inspiration_content":  "Inspiration / content",
    "brand_story":          "Brand story",
    "template_unfilled":    "⚠ Template placeholders",
    "off_brand_tracker":    "⚠ Off-brand tracker",
    "off_brand_short":      "⚠ Off-brand short-link",
    "off_brand_other":      "⚠ Off-brand other",
    "unknown":              "Unknown",
}

# Short marketer-takeaway one-liners shown as flagged-section callouts.
SECTION_CALLOUTS: dict[str, str] = {
    "template_unfilled": (
        "URLs with un-substituted Dynamic Creative placeholders "
        "(`{{site_source_name}}`, `{{ad.id}}`) — a campaign-ops bug worth flagging."
    ),
    "off_brand_tracker": (
        "Ads route through DoubleClick / measurement redirects — the true "
        "landing destination is obscured without following the hop."
    ),
    "off_brand_short": (
        "Short-link / opaque redirect host — can't classify the real "
        "landing page without an HTTP follow."
    ),
    "off_brand_other": (
        "Off-brand destination (dealer subdomain, partner site, "
        "marketplace listing) — not on the brand's own www host."
    ),
}
