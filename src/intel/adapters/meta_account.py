"""Owned Meta ad-account ingest — first-party performance joined to creative.

This is the owned-account counterpart to `meta_ads_scrape.py`. That module scrapes
the *public* Ad Library for competitors; this one reads ad accounts the user
actually owns, via the Graph Marketing API, and brings back what the Ad Library
can never expose: spend, impressions, CTR, ROAS and conversions at the ad level.

Why a separate lane rather than an extension of the Ad Library path
------------------------------------------------------------------
The obvious idea — "join performance onto the ads we already scraped, on ad id" —
does not work. The Ad Library's `ad_archive_id` and the ad account's `ad.id` are
different namespaces for the same underlying ad; nothing in either API maps one
to the other. So instead of bridging, this module re-ingests the owned ads from
the account side, where `ad.id` IS the native key, and every metric joins to it
exactly. Ads land in the normal `ads`/`creatives` tables under
`source='meta_owned'`, so the existing vision-analysis and dashboard machinery
works on them unchanged — and, because every report path filters on an explicit
source set that does not include `meta_owned`, none of the existing competitor
reports move a byte.

The one real bridge that does exist is `creative.effective_object_story_id`,
which is prefixed with the page id (e.g. `2140630076185020_...`) — the same page
id the competitor config resolved for JD Sports US. That is what lets an owned
account be tied back to a brand in the competitive set.

Creative classification (the load-bearing caveat)
-------------------------------------------------
Not every ad has a creative that can be looked at. Ads fall into three classes:

  * `analyzable` — a real fixed creative. Either a direct `image_url`/`video_id`,
    or a dynamic-creative ad whose assets live in `asset_feed_spec`. These can be
    rendered and vision-analyzed, so they get the full attribute treatment.
  * `dpa` — catalog / dynamic product ads. The creative is assembled per-product
    from the feed at serve time, so there is no fixed image to analyze. Their
    `/previews` render returns "Story Unavailable". They are still ingested and
    still carry performance, but they can only be reported on *metadata*
    attributes (body copy, CTA, format, campaign), never vision attributes.
  * `no_asset` — neither of the above; ingested for completeness.

This split is not cosmetic. Across the JD/Finish Line accounts, `dpa` is the
majority of spend, so any "performance by creative attribute" rollup MUST be
read as covering the analyzable subset only. `spend_coverage()` exists to report
that fraction honestly rather than letting a dashboard imply full coverage.

Asset acquisition
-----------------
Preference order, best fidelity first:
  1. `/{creative_id}/previews` rendered to PNG with Playwright — the ad exactly as
     a user sees it, including copy and social proof. Requires a browser.
  2. `image_url` (direct, full resolution).
  3. `thumbnail_url` — a real video frame, but only ~160x160; Meta rejects
     requests for a larger crop (stripping the `stp=` sizing param 403s), and the
     video's own `source` field is permission-gated, so this is the ceiling for
     video assets when previews are unavailable.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx


log = logging.getLogger("intel.meta_account")

GRAPH = "https://graph.facebook.com/v21.0"

# Ad-level insight fields. `spend` comes back as a plain numeric string here
# (unlike the MCP surface, which pre-formats it as "$47,880.33 USD"), so the raw
# Graph API is deliberately used rather than the MCP tool.
INSIGHT_FIELDS = [
    "ad_id", "ad_name", "campaign_id", "campaign_name", "adset_id", "adset_name",
    "impressions", "spend", "clicks", "ctr", "cpc", "cpm", "reach", "frequency",
    "actions", "action_values", "purchase_roas",
    # Meta's own quality assessment. These are ordinal buckets, not scores, and
    # they are only defined over recent delivery — a 90-day window returns
    # UNKNOWN for every ad (measured: 25/25 UNKNOWN at 90d, 144/253 ranked at
    # 30d). Ingest them anyway; the dashboard scopes them to the window where
    # they exist and says so.
    "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking",
    "video_play_actions",
    "video_thruplay_watched_actions", "video_p25_watched_actions",
    "video_p50_watched_actions", "video_p75_watched_actions",
    "video_p100_watched_actions",
]

CREATIVE_FIELDS = (
    "id,name,status,created_time,"
    "creative{id,name,object_type,image_url,image_hash,video_id,thumbnail_url,"
    "title,body,link_url,call_to_action_type,object_story_id,"
    "effective_object_story_id,product_set_id,asset_feed_spec,object_story_spec}"
)

# Graph caps a batched `?ids=` lookup at 50.
ID_CHUNK = 50

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class MetaAccountError(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("META_AD_LIBRARY_ACCESS_TOKEN")
    if not tok:
        raise MetaAccountError(
            "META_AD_LIBRARY_ACCESS_TOKEN not set — owned-account ingest needs a "
            "token with the `ads_read` permission."
        )
    return tok


def _get(client: httpx.Client, url: str, params: dict | None, *, tries: int = 3) -> dict:
    """GET with a couple of retries. Meta returns transient 500s under load often
    enough that a single failure should not kill a multi-account run."""
    last = None
    for attempt in range(tries):
        try:
            r = client.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            # Meta returns a 400 with "Service temporarily unavailable"
            # (subcode 1504044) when an insights query is momentarily too heavy —
            # and marks it `is_transient: false`, which is wrong. The identical
            # request succeeds on retry. Treat it as retryable despite the flag.
            body = r.text[:400]
            transient_400 = (
                r.status_code == 400
                and ("1504044" in body or "temporarily unavailable" in body.lower())
            )
            # Other 4xx really are terminal; don't burn retries on them.
            if r.status_code < 500 and r.status_code != 429 and not transient_400:
                raise MetaAccountError(f"{r.status_code} {body[:300]}")
            last = f"{r.status_code} {body[:200]}"
        except httpx.HTTPError as exc:  # network flake
            last = str(exc)
        time.sleep(1.5 * (attempt + 1))
    raise MetaAccountError(f"giving up after {tries} tries: {last}")


# ---------------------------------------------------------------- insights ---

def fetch_insights(
    account_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    date_preset: str = "last_90d",
    attribution_windows: list[str] | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Ad-level insights for one account. Returns one row per ad in the window.

    Ads with no delivery in the window simply do not appear — that is Meta's
    behaviour, not a bug, and it means `len()` here is "ads that ran", not "ads
    that exist".

    `attribution_windows` (e.g. ATTR_WINDOWS, or ['dda']) adds
    `action_attribution_windows` to the request, so each conversion action comes
    back broken out by window. Omitted, the account's default attribution is all
    that's returned — which is the historical behaviour.
    """
    owns = client is None
    c = client or httpx.Client(timeout=120)
    params: dict[str, Any] = {
        "access_token": _token(),
        "level": "ad",
        "fields": ",".join(INSIGHT_FIELDS),
        # Per-window breakout multiplies the conversion payload per row, and a
        # 500-row page then trips Meta's synchronous-query size guard ("please
        # reduce the amount of data") on the larger accounts. A 100-row page
        # stays under it — measured — and just paginates a few more times.
        "limit": "100" if attribution_windows else "500",
    }
    if attribution_windows:
        params["action_attribution_windows"] = json.dumps(attribution_windows)
    if since and until:
        params["time_range"] = json.dumps({"since": since, "until": until})
    else:
        params["date_preset"] = date_preset

    rows: list[dict[str, Any]] = []
    url = f"{GRAPH}/act_{account_id}/insights"
    try:
        page_params: dict | None = params
        while url:
            data = _get(c, url, page_params)
            rows.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next") or ""
            page_params = None  # the `next` cursor already carries every param
    finally:
        if owns:
            c.close()
    log.info("account %s: %d ads with delivery", account_id, len(rows))
    return rows


def _action_value(actions: Any, wanted: Iterable[str]) -> float:
    """Pull the first matching action type out of Meta's list-of-dicts shape."""
    return _action_window(actions, wanted, "value")


def _action_window(actions: Any, wanted: Iterable[str], window: str) -> float:
    """Like `_action_value`, but read a specific attribution-window key.

    When `action_attribution_windows` is requested, each action dict carries the
    default under `value` plus one key per window (`1d_view`, `7d_click`, …). A
    window absent from an action means zero credit in that window, not missing
    data. `window='value'` reproduces `_action_value` exactly, so the columns
    built from it stay byte-identical to a pre-attribution ingest.
    """
    if not isinstance(actions, list):
        return 0.0
    want = set(wanted)
    for a in actions:
        if a.get("action_type") in want:
            try:
                return float(a.get(window) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


# Attribution windows these accounts actually return. 7d/28d view-through were
# removed by Meta after iOS 14 and never come back in the response — only 1-day
# view survives — so requesting them would just add empty keys. Click windows
# are cumulative (1d ⊆ 7d ⊆ 28d), verified across the live sample. DDA
# (data-driven) is fetched separately: it does not break out alongside these.
ATTR_WINDOWS = ["1d_view", "1d_click", "7d_click", "28d_click"]

# The conversion metrics that attribution re-weights. Spend, impressions and
# clicks are NOT attributed and never vary by window. Purchases/revenue read the
# same omni_* first-match the default columns use, so `default` == the columns.
_ATTR_METRICS = {
    "pu": ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"),
    "atc": ("omni_add_to_cart", "add_to_cart"),
    "ic": ("omni_initiated_checkout", "initiate_checkout"),
    "lpv": ("omni_landing_page_view", "landing_page_view"),
    "vc": ("omni_view_content", "view_content"),
}
_ATTR_REVENUE = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")


def build_attribution(actions: Any, values: Any) -> dict[str, dict[str, float]]:
    """Per-window conversion components for one insights row.

    Returns {window_label: {pu, rv, atc, ic, lpv, vc}} for the account default
    plus every window in ATTR_WINDOWS. `default` is keyed off Meta's `value`
    field, so it equals the flat columns exactly. DDA is merged in later by the
    ingest, since it needs its own request.
    """
    out: dict[str, dict[str, float]] = {}
    for win_key, label in [("value", "default"), *[(w, w) for w in ATTR_WINDOWS]]:
        rec = {m: _action_window(actions, names, win_key)
               for m, names in _ATTR_METRICS.items()}
        rec["rv"] = _action_window(values, _ATTR_REVENUE, win_key)
        out[label] = rec
    return out


def normalize_insight(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten a raw insights row into scalar metric columns.

    Meta nests conversions inside `actions`/`action_values` as lists keyed by
    `action_type`, and reports ROAS as a single-element list. Everything arrives
    as strings. This turns all of it into plain floats so SQL can aggregate.
    """
    def f(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    roas = row.get("purchase_roas")
    if isinstance(roas, list) and roas:
        try:
            roas_v = float(roas[0].get("value") or 0)
        except (TypeError, ValueError, AttributeError):
            roas_v = 0.0
    else:
        roas_v = 0.0

    actions = row.get("actions")
    values = row.get("action_values")
    purchases = _action_value(actions, ("omni_purchase", "purchase",
                                        "offsite_conversion.fb_pixel_purchase"))
    revenue = _action_value(values, ("omni_purchase", "purchase",
                                     "offsite_conversion.fb_pixel_purchase"))
    return {
        "platform_ad_id": row.get("ad_id"),
        "ad_name": row.get("ad_name"),
        "campaign_id": row.get("campaign_id"),
        "campaign_name": row.get("campaign_name"),
        "adset_id": row.get("adset_id"),
        "adset_name": row.get("adset_name"),
        "date_start": row.get("date_start"),
        "date_stop": row.get("date_stop"),
        "impressions": f("impressions"),
        "spend": f("spend"),
        "clicks": f("clicks"),
        "ctr": f("ctr"),
        "cpc": f("cpc"),
        "cpm": f("cpm"),
        "reach": f("reach"),
        "frequency": f("frequency"),
        "link_clicks": _action_value(actions, ("link_click",)),
        "purchases": purchases,
        "revenue": revenue,
        "roas": roas_v,
        # --- funnel steps -----------------------------------------------------
        # Meta reports the same conversion under several attribution surfaces
        # (`add_to_cart`, `omni_add_to_cart`, `onsite_web_add_to_cart`,
        # `offsite_conversion.fb_pixel_add_to_cart`). `_action_value` takes the
        # FIRST match, not the sum, so these are deduplicated totals — summing
        # the aliases would count one cart add up to four times.
        #
        # The omni_* surface is listed first deliberately: `purchases`/`revenue`
        # above resolve to `omni_purchase` on these accounts (measured: it wins
        # 584/584 rows), so a funnel built on the web-only surface would end on a
        # smaller number than the ROAS tiles report and read as a discrepancy.
        # Measured on the current window, omni and web-only are identical for
        # cart-add and checkout and diverge only at purchase (256,339 vs
        # 222,731) — that gap is in-store/offline conversions, which genuinely
        # never passed an on-site checkout. The dashboard footnotes it rather
        # than hiding it by silently switching surfaces mid-funnel.
        "landing_page_views": _action_value(actions, ("omni_landing_page_view",
                                                      "landing_page_view")),
        "view_content": _action_value(actions, ("omni_view_content", "view_content")),
        "add_to_cart": _action_value(actions, ("omni_add_to_cart", "add_to_cart")),
        "initiate_checkout": _action_value(actions, ("omni_initiated_checkout",
                                                     "initiate_checkout")),
        # Ordinal, and only populated for recent delivery — see INSIGHT_FIELDS.
        "quality_ranking": row.get("quality_ranking"),
        "engagement_rate_ranking": row.get("engagement_rate_ranking"),
        "conversion_rate_ranking": row.get("conversion_rate_ranking"),
        # Per-window conversion breakout. Empty {} when the request didn't ask
        # for windows, so a default ingest carries no attribution and the
        # dashboard falls back to the flat columns. DDA is merged by the ingest.
        "attribution": build_attribution(actions, values),
        # Scroll-stop / hook rate inputs.
        #
        # The numerator is 3-second video views, which Meta reports as the
        # `video_view` action. Two nearby fields are wrong for this and were
        # measured before choosing:
        #   * `video_continuous_2_sec_watched_actions` returns 0 across these
        #     accounts — it simply isn't populated, so a 2-second definition
        #     would silently read as "no one stopped".
        #   * `video_play_actions` counts autoplay initiations and runs at ~92%
        #     of impressions, which would render a meaningless ~90% "hook rate".
        # It is kept anyway, as the flag for "this ad served video at all" —
        # that's what makes the denominator honest (see video_impressions).
        "video_3s": _action_value(actions, ("video_view",)),
        "video_plays": _action_value(row.get("video_play_actions"), ("video_view",)),
        "thruplays": _action_value(row.get("video_thruplay_watched_actions"), ("video_view",)),
        "video_p25": _action_value(row.get("video_p25_watched_actions"), ("video_view",)),
        "video_p50": _action_value(row.get("video_p50_watched_actions"), ("video_view",)),
        "video_p75": _action_value(row.get("video_p75_watched_actions"), ("video_view",)),
        "video_p100": _action_value(row.get("video_p100_watched_actions"), ("video_view",)),
        "extra_json": json.dumps({k: row.get(k) for k in ("actions", "action_values") if row.get(k)}),
    }


# --------------------------------------------------------------- creatives ---

def fetch_ad_meta(
    account_id: str,
    ad_ids: list[str],
    *,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch ad + nested creative metadata for `ad_ids`, chunked to Graph's cap."""
    owns = client is None
    c = client or httpx.Client(timeout=120)
    out: dict[str, dict[str, Any]] = {}
    try:
        for i in range(0, len(ad_ids), ID_CHUNK):
            chunk = ad_ids[i:i + ID_CHUNK]
            try:
                data = _get(c, f"{GRAPH}/", {
                    "access_token": _token(),
                    "ids": ",".join(chunk),
                    "fields": CREATIVE_FIELDS,
                })
            except MetaAccountError as exc:
                # One bad chunk shouldn't lose the rest of the account.
                log.warning("creative chunk %d-%d failed: %s", i, i + len(chunk), exc)
                continue
            out.update(data)
    finally:
        if owns:
            c.close()
    return out


def classify_creative(creative: dict[str, Any]) -> str:
    """Return 'analyzable' | 'dpa' | 'no_asset'.

    Order matters: a dynamic-creative ad can carry a `product_set_id` *and* real
    assets in `asset_feed_spec`, and in that case the assets win — we can look at
    it, so it is analyzable.
    """
    afs = creative.get("asset_feed_spec") or {}
    if creative.get("video_id") or creative.get("image_url"):
        return "analyzable"
    if (afs.get("videos") or afs.get("images")):
        return "analyzable"
    oss = creative.get("object_story_spec") or {}
    template_name = str(((oss.get("template_data") or {}).get("name")) or "")
    if creative.get("product_set_id") or "{{product." in template_name:
        return "dpa"
    return "no_asset"


def extract_assets(creative: dict[str, Any]) -> list[dict[str, Any]]:
    """Every distinct visual asset on a creative, as {kind, video_id, url}.

    Dynamic-creative ads carry several videos/images in `asset_feed_spec`; each is
    a separately-served variant, so each becomes its own creative row rather than
    being collapsed to one.
    """
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, url: str | None, video_id: str | None = None) -> None:
        key = video_id or url or ""
        if not key or key in seen:
            return
        seen.add(key)
        assets.append({"kind": kind, "url": url, "video_id": video_id})

    if creative.get("image_url"):
        add("image", creative["image_url"])
    if creative.get("video_id"):
        # Video bytes are permission-gated; the thumbnail is a real frame from it.
        add("video_thumb", creative.get("thumbnail_url"), creative["video_id"])

    afs = creative.get("asset_feed_spec") or {}
    for v in (afs.get("videos") or []):
        add("video_thumb", v.get("thumbnail_url"), v.get("video_id"))
    for im in (afs.get("images") or []):
        add("image", im.get("url") or im.get("permalink_url"))
    return assets


def creative_copy(creative: dict[str, Any]) -> dict[str, Any]:
    """Copy fields, preferring dynamic-creative variants when present.

    For dynamic creative the top-level `title`/`body` are often empty while the
    real copy sits in `asset_feed_spec.titles/bodies`; join those so metadata
    attributes are populated even for ads with no fixed creative.
    """
    afs = creative.get("asset_feed_spec") or {}
    titles = [t.get("text") for t in (afs.get("titles") or []) if t.get("text")]
    bodies = [b.get("text") for b in (afs.get("bodies") or []) if b.get("text")]
    ctas = [c.get("type") for c in (afs.get("call_to_action_types") or [])
            if isinstance(c, dict) and c.get("type")]
    if not ctas:
        ctas = [c for c in (afs.get("call_to_action_types") or []) if isinstance(c, str)]
    links = [l.get("website_url") for l in (afs.get("link_urls") or []) if l.get("website_url")]
    oss = creative.get("object_story_spec") or {}
    td = oss.get("template_data") or {}
    return {
        "title": creative.get("title") or (titles[0] if titles else None) or td.get("name"),
        "body": creative.get("body") or (bodies[0] if bodies else None) or td.get("message"),
        "cta_type": (creative.get("call_to_action_type")
                     or (ctas[0] if ctas else None)
                     or ((td.get("call_to_action") or {}).get("type"))),
        "link_url": (creative.get("link_url") or (links[0] if links else None)
                     or td.get("link")),
        "title_variants": titles,
        "body_variants": bodies,
    }


# ------------------------------------------------------------ asset fetch ----

def download_asset(url: str, dest: Path, *, client: httpx.Client | None = None) -> bool:
    """Download one asset. Returns True on success.

    Note: Meta's CDN sizing is baked into the signed URL — stripping the `stp=`
    crop parameter to get a larger image returns 403, so whatever size the API
    handed us is the size we get.
    """
    owns = client is None
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = c.get(url)
        if r.status_code != 200 or not r.content:
            log.debug("asset %s -> HTTP %s", url[:80], r.status_code)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return True
    except httpx.HTTPError as exc:
        log.debug("asset download failed (%s): %s", url[:80], exc)
        return False
    finally:
        if owns:
            c.close()


def preview_iframe_url(creative_id: str, *, ad_format: str = "MOBILE_FEED_STANDARD",
                       client: httpx.Client | None = None) -> str | None:
    """Resolve a creative's preview iframe URL, or None if Meta won't render it.

    DPA creatives legitimately return a body containing "Story Unavailable" —
    there is no fixed creative to render — so callers should treat None as
    "expected for catalog ads", not as an error.
    """
    owns = client is None
    c = client or httpx.Client(timeout=60)
    try:
        data = _get(c, f"{GRAPH}/{creative_id}/previews",
                    {"access_token": _token(), "ad_format": ad_format})
    except MetaAccountError as exc:
        log.debug("preview lookup failed for %s: %s", creative_id, exc)
        return None
    finally:
        if owns:
            c.close()
    items = data.get("data") or []
    if not items:
        return None
    m = re.search(r'src="([^"]+)"', items[0].get("body") or "")
    return html.unescape(m.group(1)) if m else None


def render_previews(
    jobs: list[tuple[str, Path]],
    *,
    headless: bool = True,
    viewport: tuple[int, int] = (540, 1200),
) -> dict[str, bool]:
    """Screenshot preview iframes to PNG. `jobs` is [(iframe_url, dest_path)].

    Batched through a single browser because launching Chromium per ad dominates
    the cost. Failures are per-job, never fatal.
    """
    from playwright.sync_api import sync_playwright  # local: browser extra is optional

    results: dict[str, bool] = {}
    if not jobs:
        return results
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": viewport[0], "height": viewport[1]},
        )
        page = ctx.new_page()
        for url, dest in jobs:
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
                page.wait_for_timeout(2_500)
                text = (page.inner_text("body") or "")
                if "Story Unavailable" in text or "unavailable for preview" in text:
                    # Expected for catalog/DPA ads — no fixed creative exists.
                    results[str(dest)] = False
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(dest), full_page=True)
                results[str(dest)] = dest.exists() and dest.stat().st_size > 2_000
            except Exception as exc:  # noqa: BLE001 — never let one ad kill the run
                log.debug("preview render failed (%s): %s", dest.name, exc)
                results[str(dest)] = False
        browser.close()
    return results


# ------------------------------------------------------------- reporting ----

def spend_coverage(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """How much spend sits on creative we can actually look at.

    Exists so dashboards can state coverage explicitly. A "performance by creative
    attribute" view built on the analyzable subset while the majority of spend is
    DPA would otherwise read as if it covered the whole account.
    """
    by_class: dict[str, dict[str, float]] = {}
    for r in rows:
        cls = r.get("creative_class") or "unknown"
        b = by_class.setdefault(cls, {"ads": 0.0, "spend": 0.0, "impressions": 0.0})
        b["ads"] += 1
        b["spend"] += float(r.get("spend") or 0)
        b["impressions"] += float(r.get("impressions") or 0)
    total = sum(b["spend"] for b in by_class.values())
    analyzable = by_class.get("analyzable", {}).get("spend", 0.0)
    return {
        "by_class": by_class,
        "total_spend": total,
        "analyzable_spend": analyzable,
        "analyzable_pct": (100.0 * analyzable / total) if total else 0.0,
    }


# ---------------------------------------------------------------- audience ---
# "Audience" isn't one field in Meta — it's the adset's targeting spec. These
# accounts encode strategy in adset names too ("Advantage+ | PRO | DMAs",
# "90d_Site Visitors", "US - W - 13-65"), so classification uses both: the
# structured targeting spec first, falling back to name patterns.

ADSET_FIELDS = (
    "id,name,optimization_goal,"
    "targeting{age_min,age_max,genders,geo_locations,custom_audiences,"
    "excluded_custom_audiences,flexible_spec}"
)

# Custom-audience name fragments that imply the user already engaged with the
# brand. Site-visitor / cart / purchaser lists are retargeting; lookalikes are
# modelled off them but still point at strangers, so they rank as prospecting.
_RETARGET_HINTS = ("site visitor", "visitors", "cart", "purchase", "purchaser",
                   "engaged", "engagers", "viewers", "add to cart", "atc",
                   "retarget", "rtg", "existing", "customer", "email", "crm",
                   "app users", "ig engagement", "fb engagement", "dpa_")
_LOOKALIKE_HINTS = ("lookalike", "lal_", "lal ", "(us,", "%)")
_INTEREST_HINTS = ("interest", "wv_", "affinity", "behaviou", "behavior")


def _names(audiences: Any) -> list[str]:
    out = []
    for a in (audiences or []):
        if isinstance(a, dict):
            out.append(str(a.get("name") or a.get("id") or ""))
        else:
            out.append(str(a))
    return out


def classify_audience(adset: dict[str, Any]) -> dict[str, Any]:
    """Derive filterable audience facets from one adset.

    Returns funnel stage, gender, age band, geo scope and the raw adset name.
    Every facet is a plain string so the dashboard can filter on it directly.

    Funnel stage is the judgement call: Meta has no "is this retargeting" flag,
    so it is inferred from whether the attached custom audiences describe people
    who already touched the brand. Anything unclassifiable stays 'unknown' rather
    than being defaulted into prospecting, which would silently inflate it.
    """
    t = adset.get("targeting") or {}
    name = (adset.get("name") or "")
    lname = name.lower()
    ca = _names(t.get("custom_audiences"))
    ca_l = " ".join(ca).lower()

    # --- funnel stage ---
    if ca and any(h in ca_l for h in _RETARGET_HINTS):
        stage = "retargeting"
    elif ca and any(h in ca_l for h in _LOOKALIKE_HINTS):
        stage = "lookalike"
    elif any(h in lname for h in _RETARGET_HINTS):
        stage = "retargeting"
    elif any(h in lname for h in _LOOKALIKE_HINTS):
        stage = "lookalike"
    elif any(h in lname for h in _INTEREST_HINTS) or t.get("flexible_spec"):
        stage = "interest"
    elif "advantage" in lname or "broad" in lname or "pro" in lname:
        stage = "prospecting_broad"
    elif ca:
        stage = "custom_audience_other"
    elif t.get("age_min") or t.get("geo_locations"):
        stage = "prospecting_broad"
    else:
        stage = "unknown"

    # --- gender: Meta encodes 1=male, 2=female; empty/None = all ---
    g = t.get("genders")
    if g == [1] or " - m - " in lname or lname.endswith(" - m"):
        gender = "men"
    elif g == [2] or " - w - " in lname or lname.endswith(" - w"):
        gender = "women"
    elif not g:
        gender = "all"
    else:
        gender = "mixed"

    amin, amax = t.get("age_min"), t.get("age_max")
    age = f"{amin}-{amax}" if amin and amax else "unspecified"

    # --- geo scope ---
    geo = t.get("geo_locations") or {}
    keys = set(geo.keys())
    if "geo_markets" in keys or "dma" in lname:
        geo_scope = "dma"
    elif {"custom_locations", "places"} & keys or "miles" in lname or "radius" in lname:
        geo_scope = "local_radius"
    elif "regions" in keys or "cities" in keys or "zips" in keys:
        geo_scope = "regional"
    elif "countries" in keys:
        geo_scope = "national"
    else:
        geo_scope = "unspecified"

    return {
        "audience_stage": stage,
        "audience_gender": gender,
        "audience_age": age,
        "audience_geo": geo_scope,
        "audience_name": name,
        "audience_custom": "; ".join(ca[:4]),
        "optimization_goal": adset.get("optimization_goal"),
    }


def fetch_adsets(
    account_id: str,
    adset_ids: list[str],
    *,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch targeting for specific adsets, chunked.

    Deliberately fetches only the adsets referenced by ingested ads — these
    accounts hold thousands of adsets (2,225 in one) while only a few hundred
    delivered in a given window, and listing the whole edge with targeting
    expanded trips Meta's "reduce the amount of data" 500.
    """
    owns = client is None
    c = client or httpx.Client(timeout=120)
    out: dict[str, dict[str, Any]] = {}
    ids = [i for i in dict.fromkeys(adset_ids) if i]
    try:
        for i in range(0, len(ids), ID_CHUNK):
            chunk = ids[i:i + ID_CHUNK]
            try:
                data = _get(c, f"{GRAPH}/", {
                    "access_token": _token(),
                    "ids": ",".join(chunk),
                    "fields": ADSET_FIELDS,
                })
            except MetaAccountError as exc:
                log.warning("adset chunk %d failed: %s", i, exc)
                continue
            out.update(data)
    finally:
        if owns:
            c.close()
    return out


# ------------------------------------------------------------ time series ---

def fetch_series(
    account_id: str,
    *,
    since: str,
    until: str,
    increment: int = 7,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Ad-level insights broken into fixed-width buckets across the window.

    `increment=7` gives ~13 points across a 90-day window, which is the sparkline
    resolution the stat tiles want — daily would ship 7x the numbers to the
    browser for a line that renders at ~120px wide.

    `increment=1` is the input to the scale/kill timeline, which animates
    cumulative spend and CPA day by day and therefore genuinely needs the
    resolution. Daily rows are stored in `ad_daily`, NOT in
    `ad_performance_series`: that table is keyed (platform_ad_id, bucket_start)
    with no width column, so writing daily rows into it would collide with the
    weekly rows that share a start date and silently corrupt the sparklines.
    """
    owns = client is None
    c = client or httpx.Client(timeout=180)
    params: dict[str, Any] = {
        "access_token": _token(),
        "level": "ad",
        "fields": "ad_id,impressions,spend,clicks,actions,action_values,video_play_actions",
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": str(increment),
        "limit": "500",
    }
    rows: list[dict[str, Any]] = []
    url = f"{GRAPH}/act_{account_id}/insights"
    try:
        page_params: dict | None = params
        while url:
            data = _get(c, url, page_params)
            rows.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next") or ""
            page_params = None
    finally:
        if owns:
            c.close()
    return rows


def normalize_series_row(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten one time-bucketed insights row.

    Deliberately narrower than `normalize_insight`: a bucketed row exists once
    per ad per period, so the row count is ~90x the window's ad count and every
    stored column is paid for 90 times over. Only what the timeline and the
    sparklines actually plot is kept.
    """
    def f(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    actions = row.get("actions")
    values = row.get("action_values")
    return {
        "platform_ad_id": row.get("ad_id"),
        # For a bucketed row `date_start` IS the bucket start.
        "date_start": row.get("date_start"),
        "date_stop": row.get("date_stop"),
        "impressions": f("impressions"),
        "spend": f("spend"),
        "clicks": f("clicks"),
        "purchases": _action_value(actions, ("omni_purchase", "purchase",
                                             "offsite_conversion.fb_pixel_purchase")),
        "revenue": _action_value(values, ("omni_purchase", "purchase",
                                          "offsite_conversion.fb_pixel_purchase")),
        "video_3s": _action_value(actions, ("video_view",)),
        "video_plays": _action_value(row.get("video_play_actions"), ("video_view",)),
    }
