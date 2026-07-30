"""Creative-performance dashboard for owned Meta ad accounts.

Answers the question the competitor dashboards structurally cannot: *which
creative attributes actually correlate with performance*. It joins first-party
metrics (spend, CTR, ROAS, conversions) onto the vision-derived attributes of the
same ads, then ranks each attribute value by weighted performance.

Two honesty constraints shape the whole module:

1. **Coverage is partial by construction.** Catalog/DPA ads have no fixed
   creative to analyze, and they are typically the majority of spend. Every view
   therefore states what share of spend it actually covers, and the attribute
   tables are explicitly scoped to the analyzable subset. A number that silently
   implied whole-account coverage would be worse than no number.

2. **Correlation, not causation, and thin buckets lie.** Attribute buckets below
   `min_impressions` are dropped rather than shown with a noisy rate, and every
   bucket carries its own n so a 3-ad bucket is never read like a 300-ad one.

Metrics are impression-weighted, not row-averaged: averaging per-ad CTRs would
let a 200-impression ad swing the mean as hard as a 2M-impression one.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger("intel.performance_dashboard")

# Attributes worth cross-tabbing. Scalar (string/bool) attributes become one
# bucket per value; list attributes fan out so an ad contributes to each value.
SCALAR_ATTRS = [
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
LIST_ATTRS = [
    ("value_props", "Value props"),
    ("key_features", "Key features"),
    ("products_visible", "Products shown"),
    ("seasonal_tags", "Seasonal hooks"),
]
NESTED_ATTRS = [
    ("text_overlay.density", "Text-overlay density"),
    ("text_overlay.copy_lean", "Copy lean"),
    ("urgency_cues.present", "Urgency cues present"),
    ("casting.people_visible", "People visible"),
]


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
               oa.title, oa.body,
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
        if not d["asset_path"]:
            # No analyzed creative (typically a DPA ad) — still try to show
            # whatever asset exists so the drill-down isn't blank.
            any_asset = conn.execute("""
                SELECT c.asset_path, c.asset_type FROM creatives c
                JOIN owned_ads oa ON oa.ad_db_id = c.ad_id
                WHERE oa.platform_ad_id = ?
                ORDER BY CASE c.asset_type WHEN 'ad_preview' THEN 0 ELSE 1 END, c.id
                LIMIT 1
            """, (d["platform_ad_id"],)).fetchone()
            if any_asset:
                d["asset_path"] = any_asset["asset_path"]
                d["asset_type"] = any_asset["asset_type"]
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
    conf = a.get("confidence")
    if conf is None:
        return True  # older analyses predate the confidence field
    try:
        return float(conf) >= MIN_ANALYSIS_CONFIDENCE
    except (TypeError, ValueError):
        return True


def _attribute_table(rows: list[dict], min_impressions: int) -> list[dict]:
    """Cross-tab every tracked attribute value against performance."""
    analyzed = [r for r in rows if r.get("analysis") and _is_readable(r)]
    tables: list[dict] = []

    def emit(label: str, buckets: dict[str, list[dict]]) -> None:
        entries = []
        for value, brows in buckets.items():
            st = _bucket_stats(brows)
            if st["impressions"] < min_impressions:
                continue
            st["value"] = value
            # Member ad ids, highest spend first — the drill-down renders these.
            st["ad_ids"] = [
                b["platform_ad_id"]
                for b in sorted(brows, key=lambda r: -(r.get("spend") or 0))
            ]
            entries.append(st)
        if len(entries) < 2:
            # A single surviving bucket has nothing to compare against.
            return
        entries.sort(key=lambda e: -e["impressions"])
        base = _bucket_stats(analyzed)
        for e in entries:
            e["ctr_index"] = (100.0 * e["ctr"] / base["ctr"]) if base["ctr"] else 0.0
            e["roas_index"] = (100.0 * e["roas"] / base["roas"]) if base["roas"] else 0.0
        tables.append({"label": label, "entries": entries, "baseline": base})

    for key, label in SCALAR_ATTRS:
        b: dict[str, list[dict]] = defaultdict(list)
        for r in analyzed:
            v = _norm((r["analysis"] or {}).get(key))
            if v:
                b[v].append(r)
        emit(label, b)

    for key, label in NESTED_ATTRS:
        b = defaultdict(list)
        for r in analyzed:
            v = _norm(_dig(r["analysis"] or {}, key))
            if v:
                b[v].append(r)
        emit(label, b)

    for key, label in LIST_ATTRS:
        b = defaultdict(list)
        for r in analyzed:
            vals = (r["analysis"] or {}).get(key)
            if isinstance(vals, list):
                for v in vals:
                    nv = _norm(v)
                    if nv:
                        b[nv].append(r)
        emit(label, b)

    return tables


def _metadata_table(rows: list[dict], min_impressions: int) -> list[dict]:
    """Attributes available for EVERY ad, including catalog/DPA ones.

    This is what makes the dashboard useful despite DPA dominating spend: CTA
    type, object type and campaign are known without ever looking at an image, so
    the majority of spend can still be compared on something.
    """
    tables: list[dict] = []
    for key, label in (("cta_type", "Call to action"),
                       ("object_type", "Ad object type"),
                       ("creative_class", "Creative class"),
                       ("campaign_name", "Campaign"),
                       ("account_name", "Ad account")):
        b: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            v = _norm(r.get(key))
            if v:
                b[v].append(r)
        entries = []
        for value, brows in b.items():
            st = _bucket_stats(brows)
            if st["impressions"] < min_impressions:
                continue
            st["value"] = value
            st["ad_ids"] = [
                x["platform_ad_id"]
                for x in sorted(brows, key=lambda r: -(r.get("spend") or 0))
            ]
            entries.append(st)
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: -e["spend"])
        base = _bucket_stats(rows)
        for e in entries:
            e["ctr_index"] = (100.0 * e["ctr"] / base["ctr"]) if base["ctr"] else 0.0
            e["roas_index"] = (100.0 * e["roas"] / base["roas"]) if base["roas"] else 0.0
        tables.append({"label": label, "entries": entries[:25], "baseline": base})
    return tables

# ------------------------------------------------------------------ render ---
# Everything below ships DATA to the browser and computes the tables there.
#
# Why client-side: the audience filter has to recompute every bucket, baseline
# and index. Pre-rendering a table per filter combination is combinatorial
# (stage x gender x geo x age x account), and recomputing server-side would mean
# a round trip on a static host. One pass of per-ad records — 623 rows here — is
# small enough to aggregate instantly in JS, and it keeps a single definition of
# the aggregation instead of one in Python and one in JavaScript.

# Cap on cards rendered per expanded bucket. Announced in the drill-down header
# whenever it bites — a silently truncated list reads as "these are all the ads".
MAX_CARDS_PER_BUCKET = 60

# Facets offered in the filter bar: (field, label).
FILTERS = [
    ("b", "Brand"),
    ("ac", "Ad account"),
    ("stage", "Funnel stage"),
    ("gen", "Gender targeting"),
    ("geo", "Geo scope"),
    ("age", "Age band"),
    ("cl", "Creative class"),
]

# Metadata attributes are known for every ad; vision attributes only for ads
# with a readable creative analysis.
META_SPECS = [
    ("cta", "Call to action", "scalar"),
    ("ot", "Ad object type", "scalar"),
    ("cl", "Creative class", "scalar"),
    ("stage", "Audience — funnel stage", "scalar"),
    ("geo", "Audience — geo scope", "scalar"),
    ("age", "Audience — age band", "scalar"),
    ("gen", "Audience — gender targeting", "scalar"),
    ("opt", "Delivery optimisation goal", "scalar"),
    ("ac", "Ad account", "scalar"),
]

# Rendered last, after the creative-attribute tables. Campaign and ad-set names
# are account *structure*, not a property of the creative: they have a long tail
# of near-unique values (one bucket per campaign), so they read as a reference
# listing rather than a comparison, and they push the attribute tables down the
# page if left among them. They still cover 100% of filtered spend.
TAIL_SPECS = [
    ("cp", "Campaign", "scalar"),
    ("an", "Ad set (audience)", "scalar"),
]

CSS = """
/* Dark is the DEFAULT here, not a prefers-color-scheme branch: this dashboard is
   read in ad-ops sessions alongside Ads Manager, and it previously flipped to
   light on a light-themed OS with no way to override. The toggle writes
   data-theme on <html> and persists it, so the viewer's choice always wins. */
:root,:root[data-theme="dark"]{
--bg:#0d1017;--panel:#161a22;--panel2:#1b202a;--line:#252b37;--fg:#e9ecf3;
--dim:#98a2b3;--good:#3fb950;--bad:#f85149;--accent:#58a6ff;--warn:#d29922;
--shadow:0 1px 3px rgba(0,0,0,.4)}
:root[data-theme="light"]{
--bg:#fbfcfd;--panel:#fff;--panel2:#f6f8fa;--line:#e3e7ec;--fg:#1b1f27;
--dim:#5c6672;--good:#1a7f37;--bad:#cf222e;--accent:#0969da;--warn:#9a6700;
--shadow:0 1px 3px rgba(0,0,0,.08)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1320px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:34px 0 12px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h3.grp{font-size:15px;margin:44px 0 4px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);font-weight:600}
.sub{color:var(--dim);margin-bottom:20px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.toggle{background:var(--panel);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:7px 13px;cursor:pointer;font-size:13px;white-space:nowrap}
.toggle:hover{border-color:var(--accent);color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:18px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;
box-shadow:var(--shadow)}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
/* --- filter bar --- */
.filters{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px;margin:18px 0;box-shadow:var(--shadow)}
.frow{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.fld{display:flex;flex-direction:column;gap:4px;min-width:150px}
.fld label{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.fld select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
border-radius:7px;padding:7px 9px;font-size:13px;min-width:150px}
.fld select:focus{outline:none;border-color:var(--accent)}
.fbtn{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
border-radius:7px;padding:7px 12px;cursor:pointer;font-size:12.5px}
.fbtn:hover{border-color:var(--accent);color:var(--accent)}
.fstat{color:var(--dim);font-size:12.5px;margin-top:10px}
.fstat b{color:var(--fg)}
.note{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:8px;padding:12px 14px;margin:16px 0;font-size:13px;color:var(--fg)}
.note b{color:var(--warn)}
.tblwrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
border-radius:10px;margin-bottom:18px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:820px;font-size:13px}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:200px}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tbody tr.sum{cursor:pointer}
tbody tr.sum:hover,tbody tr.sum.open{background:var(--panel2)}
.caret{display:inline-block;width:11px;color:var(--dim);transition:transform .15s ease;
margin-right:6px;font-size:10px}
tr.sum.open .caret{transform:rotate(90deg);color:var(--accent)}
.idx{font-weight:600}
.up{color:var(--good)} .down{color:var(--bad)} .flat{color:var(--dim)}
.n{color:var(--dim);font-size:12px;font-weight:400}
.vn{color:var(--dim);font-size:10.5px;font-weight:400}
.bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:4px;max-width:280px}
.bar>i{display:block;height:100%;background:var(--accent)}
tr.detail>td{padding:0;border-bottom:1px solid var(--line);background:var(--bg)}
.drill{padding:14px}
.drillhead{color:var(--dim);font-size:12px;margin-bottom:10px;display:flex;
justify-content:space-between;flex-wrap:wrap;gap:8px}
.assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.asset{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden;
display:flex;flex-direction:column}
.asset .thumb{width:100%;aspect-ratio:9/16;max-height:280px;object-fit:cover;object-position:top;
background:var(--panel2);display:block}
.asset .noimg{width:100%;aspect-ratio:9/16;max-height:280px;background:var(--panel2);
display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:11px;
text-align:center;padding:10px}
.asset .body{padding:9px 10px 10px}
.asset .nm{font-size:11.5px;line-height:1.35;margin-bottom:7px;
display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.asset .mrow{display:flex;justify-content:space-between;font-size:11.5px;
padding:1.5px 0;color:var(--dim)}
.asset .mrow b{color:var(--fg);font-weight:600}
.pill{display:inline-block;font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;
padding:1.5px 6px;border-radius:99px;border:1px solid var(--line);color:var(--dim);
margin:0 4px 6px 0}
.pill.dpa{border-color:var(--warn);color:var(--warn)}
.pill.aud{border-color:var(--accent);color:var(--accent)}

/* ---- KPI tiles (reference layout: label / big value / vs-prior + delta / sparkline) ---- */
/* 10 tiles, so the track is sized to fit 5 per row on a full-width wrap
   (2 clean rows) rather than 4 + a ragged remainder. */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:14px;margin:18px 0 6px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
box-shadow:var(--shadow)}
.kpi .lbl{font-size:12.5px;color:var(--fg);opacity:.85;margin-bottom:8px}
.kpi{padding:15px 16px}
.kpi .val{font-size:26px;font-weight:600;letter-spacing:-.015em;line-height:1.1;
font-variant-numeric:tabular-nums;white-space:nowrap}
.kpi .foot{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-top:10px}
.kpi .cmp{font-size:11.5px;color:var(--dim);line-height:1.45}
.kpi .cmpv{color:var(--fg);opacity:.75}
.kpi .delta{display:flex;align-items:center;gap:4px;font-size:12.5px;font-weight:600;margin-top:3px}
.kpi .delta.pos{color:var(--good)} .kpi .delta.neg{color:var(--bad)}
.kpi .delta.flat{color:var(--dim)}
.kpi .spark{flex:0 0 auto}
/* ---- filter bar ---- */
.fbar{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;margin:18px 0;box-shadow:var(--shadow)}
.fbar .r1{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
padding-bottom:12px;border-bottom:1px solid var(--line)}
.fbar .r2{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding-top:12px}
.flabel{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
white-space:nowrap}
.win{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
padding:6px 10px;font-size:12.5px;color:var(--fg);white-space:nowrap}
.segs{display:flex;gap:4px;background:var(--panel2);border:1px solid var(--line);
border-radius:8px;padding:3px}
.seg{background:none;border:none;color:var(--dim);border-radius:6px;padding:5px 11px;
font-size:12px;cursor:pointer;white-space:nowrap}
.seg:hover{color:var(--fg)}
.seg.on{background:var(--accent);color:#fff;font-weight:600}
.spacer{flex:1 1 auto}
/* pill-shaped dimension chips */
.chip{position:relative;display:inline-flex;align-items:center}
.chip select{appearance:none;-webkit-appearance:none;background:var(--panel2);
border:1px solid var(--line);border-radius:999px;color:var(--fg);
padding:7px 30px 7px 13px;font-size:12.5px;cursor:pointer;max-width:230px;
text-overflow:ellipsis}
.chip select:hover{border-color:var(--accent)}
.chip select:focus{outline:none;border-color:var(--accent)}
.chip.set select{border-color:var(--accent);color:var(--accent)}
.chip:after{content:"⌄";position:absolute;right:12px;top:47%;transform:translateY(-50%);
pointer-events:none;color:var(--dim);font-size:13px}
.chip.locked{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
padding:7px 13px;font-size:12.5px;color:var(--dim)}
.chip.locked b{color:var(--fg);font-weight:600}
.empty{color:var(--dim);padding:24px;text-align:center;background:var(--panel);
border:1px solid var(--line);border-radius:10px}
footer{color:var(--dim);font-size:12px;margin-top:48px;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.wrap{padding:20px 12px 60px}
.assets{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.fld,.fld select{min-width:130px}}

/* ---------------------------------------------------------------- viz ---
   Series A/B use a validated categorical pair (#2f81f7 / #c2830b: passes the
   lightness band, chroma floor, CVD separation and 3:1 contrast against BOTH
   the dark and light surfaces). Scale/wait/kill reuse the status palette —
   status colors are reserved for state and never recycled as series hues, and
   every zone additionally carries a text label and a distinct axis region, so
   identity is never colour-alone. */
:root,:root[data-theme="dark"]{--sA:#2f81f7;--sB:#c2830b;
--zGood:#3fb950;--zWait:#58a6ff;--zBad:#f85149;--grid:#222835}
:root[data-theme="light"]{--sA:#2f81f7;--sB:#c2830b;
--zGood:#1a7f37;--zWait:#0969da;--zBad:#cf222e;--grid:#eaeef2}
.viz{margin:38px 0 0}
.h2sub{font-size:13px;font-weight:400;color:var(--dim)}
.vizbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 14px}
.vizbar .vs{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.vizbar .spacer{flex:1}
.tin{background:var(--panel);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:6px 9px;width:92px;font-size:13px}
.tgt{margin-left:4px}
svg.chart{display:block;width:100%;height:auto;overflow:visible}
svg.chart text{fill:var(--dim);font-size:11px}
svg.chart .axline{stroke:var(--line)}
svg.chart .gridln{stroke:var(--grid);stroke-dasharray:2 4}

/* funnel */
.funnels{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
.fn{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px 18px 18px;box-shadow:var(--shadow)}
.fn .fnhd{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
.fn .dot{width:9px;height:9px;border-radius:50%;flex:none}
.fn .fnname{font-weight:650;font-size:14px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.fn .fnmeta{color:var(--dim);font-size:12px;margin-bottom:12px}
.stg{margin:0 0 3px}
.stg .lab{display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px}
.stg .lab b{font-variant-numeric:tabular-nums}
.stg .bar{height:22px;border-radius:0 4px 4px 0;position:relative}
.stg.leak .bar{outline:2px solid var(--zBad);outline-offset:1px}
.stg.leak .lab{color:var(--zBad)}
.leakmsg{margin-top:12px;padding:9px 11px;border-radius:8px;font-size:12.5px;
background:color-mix(in srgb,var(--zBad) 12%,transparent);
border:1px solid color-mix(in srgb,var(--zBad) 35%,transparent);color:var(--fg)}
.fnfoot{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);
display:flex;gap:18px;font-size:12px;color:var(--dim);flex-wrap:wrap}
.fnfoot b{color:var(--fg);font-size:14px;display:block;font-variant-numeric:tabular-nums}

/* rating strip */
.rlane{fill:var(--panel2)}
.rdot{fill-opacity:.55;stroke:var(--panel);stroke-width:1}
.rmed{stroke:var(--fg);stroke-width:2}

/* scale/kill */
.skzones{display:flex;gap:2px;margin:0 0 10px;border-radius:8px;overflow:hidden}
.skz{padding:9px 13px;font-size:12.5px;font-weight:650;color:#fff;
display:flex;gap:9px;align-items:center;white-space:nowrap}
.skz .n{font-size:15px;color:#fff;font-weight:700}
.skz .sp{opacity:.85;font-weight:400;margin-left:auto}
.skz.g{background:var(--zGood)}.skz.w{background:var(--zWait)}.skz.k{background:var(--zBad)}
:root[data-theme="light"] .skz{color:#fff}
/* The segments double as the filter for the list below, so they read as
   controls: pointer cursor, and unselected ones recede when one is active. */
.skz{cursor:pointer;transition:opacity .12s ease,box-shadow .12s ease;position:relative}
.skz:hover{opacity:.92}
.skzs.picked .skz{opacity:.42}
.skzs.picked .skz.on{opacity:1;box-shadow:inset 0 -3px 0 rgba(255,255,255,.85)}
.skz .pick{font-size:10px;opacity:.75;font-weight:400;letter-spacing:.04em}
.sklist{margin-top:14px}
.sklist .lhd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.sklist .lhd b{font-size:14px}
.sklist .lhd .hint{color:var(--dim);font-size:12px}
/* Kept deliberately compact: this sits between the chart and the ad drill-down,
   and an unbounded 60-row table pushed the section past 15,000px, burying
   everything below it. Scrolls internally with a pinned header instead. */
.sklist .tblwrap{max-height:430px;overflow-y:auto}
.sklist table{min-width:760px}
.sklist thead th{position:sticky;top:0;background:var(--panel);z-index:1;
box-shadow:inset 0 -1px 0 var(--line)}
.sklist .zdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;
vertical-align:middle;flex:none}
.sklist .adnm{display:block;max-width:430px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-size:12.5px}
.sklist .sub{color:var(--dim);font-size:11px;margin-top:2px}
.sklist tbody tr{cursor:pointer}
.sklist tbody tr:hover{background:var(--panel2)}
.skdot{stroke:var(--panel);stroke-width:1;cursor:pointer}
.skdot:hover{stroke:var(--fg);stroke-width:2}
.tl{margin:14px 0 0;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:14px 16px}
.tlhd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.tlhd .rng{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
.tlbars{display:flex;align-items:flex-end;gap:1px;height:44px}
.tlbars i{flex:1;background:var(--line);border-radius:1px 1px 0 0;min-height:1px}
.tlbars i.on{background:var(--zGood)}
.tlctl{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap}
.tlctl input[type=range]{flex:1;min-width:180px;accent-color:var(--zGood)}
.pbtn{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:5px 11px;cursor:pointer;font-size:13px}
.pbtn.on{background:var(--zGood);border-color:var(--zGood);color:#fff}
.pbtn:hover{border-color:var(--accent)}
.tlspeed{display:inline-flex;gap:3px;align-items:center;margin-left:2px}
.pbtn.spd{padding:5px 8px;font-size:12px;min-width:40px;text-align:center}
#adPanel:empty{display:none}
.adp{margin-top:16px;background:var(--panel);border:1px solid var(--line);
border-left:3px solid var(--accent);border-radius:12px;padding:16px 18px}
.adp .aphd{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}
.adp .apnm{font-weight:650;font-size:14px;word-break:break-word}
.adp .apmet{display:flex;gap:22px;flex-wrap:wrap;margin:12px 0 4px}
.adp .apmet div{font-size:12px;color:var(--dim)}
.adp .apmet b{display:block;font-size:17px;color:var(--fg);font-variant-numeric:tabular-nums}

/* ---- scale/kill target-reduction control ---- */
#skAdj .seg{padding:5px 8px;font-size:11.5px}

/* ---- per-zone dashboard sections (scale / wait / kill) ---- */
.zsecs{margin-top:16px}
.zseclead{color:var(--dim);font-size:12.5px;margin-bottom:12px}
.zsec{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin-bottom:16px;box-shadow:var(--shadow)}
.zsec .zhd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:12px;
padding-bottom:10px;border-bottom:1px solid var(--line)}
.zbadge{font-size:11px;font-weight:700;letter-spacing:.06em;color:#fff;
padding:3px 9px;border-radius:6px;flex:none}
.zsec .zhd b{font-size:14px}
.zsec .zhd .zsp{color:var(--dim);font-variant-numeric:tabular-nums}
.zsec .zhd .hint{color:var(--dim);font-size:12px;font-weight:400;margin-left:auto;
max-width:520px;text-align:right}
.ztrends{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;
margin-bottom:14px}
.tchart{margin:0;background:var(--panel2);border:1px solid var(--line);border-radius:10px;
padding:10px 12px 6px}
.tchart .tcttl{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin-bottom:2px}
.tchart .tcnow{font-size:14px;font-weight:700;letter-spacing:0;text-transform:none;
font-variant-numeric:tabular-nums}
.tchart .tcempty{color:var(--dim);font-size:12px;padding:24px 0;text-align:center}
.zsec .tblwrap{max-height:360px;overflow-y:auto;margin-bottom:0}
.zsec table{min-width:640px}
.zsec thead th{position:sticky;top:0;background:var(--panel);z-index:1;
box-shadow:inset 0 -1px 0 var(--line)}
.zsec .zdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;
vertical-align:middle;flex:none}
.zsec .adnm{display:block;max-width:420px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-size:12.5px}
.zsec .sub{color:var(--dim);font-size:11px;margin-top:2px}
.zsec tbody tr{cursor:pointer}
.zsec tbody tr:hover{background:var(--panel2)}

/* ---- permanent left-hand section nav ---- */
html{scroll-behavior:smooth}
section[id]{scroll-margin-top:18px}
.attrsec{scroll-margin-top:18px}
.attrsec h2{margin-top:30px}
.attrtrends{margin:4px 0 14px}
.attrbar .tcttl{margin-bottom:2px}
.tcnote{font-size:9px;text-transform:none;letter-spacing:0;color:var(--dim);opacity:.7;font-weight:400}
/* Fixed, always-visible sidebar. Vertically centred but capped at 88vh with its
   own scroll, so a long attribute list never runs off-screen. Desktops reserve
   left padding on <body> (below) so it never overlaps content; on small screens
   there isn't room for a 224px rail, so it is hidden there. */
.secnav{position:fixed;left:0;top:50%;transform:translateY(-50%);z-index:50;
display:flex;align-items:stretch}
.secnav-inner{width:224px;max-height:88vh;overflow-y:auto;
background:color-mix(in srgb,var(--panel) 92%,transparent);
backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
border:1px solid var(--line);border-left:none;border-radius:0 12px 12px 0;box-shadow:var(--shadow);
padding:12px 10px}
.secnav-hd{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
padding:2px 8px 8px}
.secnav-inner a{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:8px;
color:var(--dim);text-decoration:none;font-size:12.5px;line-height:1.2}
.secnav-inner a i{width:7px;height:7px;border-radius:50%;background:var(--line);flex:none;
transition:background .15s ease,transform .15s ease}
.secnav-inner a:hover{background:var(--panel2);color:var(--fg)}
.secnav-inner a.active{color:var(--fg);font-weight:600;background:var(--panel2)}
.secnav-inner a.active i{background:var(--accent);transform:scale(1.35)}
/* dynamic attribute sub-links, grouped under their section headings */
.secnav-sub{margin:2px 0 2px 6px;border-left:1px solid var(--line);padding-left:4px}
.secnav-grp{font-size:9.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);
opacity:.8;padding:8px 8px 3px}
.secnav-inner a.sub{padding:5px 8px;font-size:11.5px}
.secnav-inner a.sub i{width:5px;height:5px}
/* Permanently hold the page clear of the fixed rail (content stays centred in
   the remaining width); hide the rail where the viewport is too narrow for it. */
@media(min-width:861px){body{padding-left:238px}}
@media(max-width:860px){.secnav{display:none}}

/* ---- early read vs final scale ---- */
.erhd2{margin:2px 0 14px}
.erslider{flex:1;min-width:180px;max-width:400px;accent-color:var(--accent);cursor:pointer}
.eropts{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin:0 0 14px;display:flex;flex-direction:column;gap:10px}
.erorow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.erin{background:var(--panel);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:6px 10px;font-size:12.5px;min-width:220px;flex:1;max-width:340px}
.erin:focus{outline:none;border-color:var(--accent)}
.ercard{display:flex;gap:22px;align-items:center;flex-wrap:wrap;background:var(--panel);
border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:12px;
padding:16px 20px;margin:0 0 16px;box-shadow:var(--shadow)}
.ercard .erstat{flex:none}
.ercard .erstat .k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.ercard .erstat .v{font-size:40px;font-weight:700;line-height:1;letter-spacing:-.02em;
font-variant-numeric:tabular-nums;margin-top:4px}
.ercard .ertext{min-width:240px}
.ercard .ertext .erinterp{font-size:15px;font-weight:600}
.ercard .ertext .ersub2{color:var(--dim);font-size:12.5px;margin-top:4px}
.erlegend{display:flex;gap:18px;margin:0 0 8px;font-size:12.5px;color:var(--dim)}
.erlegend i{display:inline-block;width:20px;height:3px;border-radius:2px;margin-right:6px;
vertical-align:middle}
"""


JS = r"""
(function(){
  var root=document.documentElement;
  try{root.setAttribute('data-theme', localStorage.getItem('perfdash-theme')||'dark');}catch(e){}
  var tbtn=document.getElementById('themeToggle');
  function tlabel(){tbtn.textContent=root.getAttribute('data-theme')==='dark'?'☀ Light':'☾ Dark';}
  tlabel();
  tbtn.addEventListener('click',function(){
    var nx=root.getAttribute('data-theme')==='dark'?'light':'dark';
    root.setAttribute('data-theme',nx);
    try{localStorage.setItem('perfdash-theme',nx);}catch(e){}
    tlabel();
  });

  var money=function(n){return '$'+(n||0).toLocaleString(undefined,{maximumFractionDigits:0});};
  var num=function(n){return (n||0).toLocaleString(undefined,{maximumFractionDigits:0});};
  // CPA lives in the $10–$50 range on these accounts, where rounding to whole
  // dollars loses real precision — a target of $19.63 rendered as "$20" reads
  // like a round number someone chose rather than the measured blend.
  var money2=function(n){return '$'+(n||0).toLocaleString(undefined,
    {minimumFractionDigits:2,maximumFractionDigits:2});};
  var pct2=function(n){return (+n).toFixed(2)+'%';};
  var mult=function(n){return (+n).toFixed(2)+'x';};
  var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});};

  // --- attribution -----------------------------------------------------------
  // Which window credits conversions. Only conversions move: spend, impressions,
  // clicks, CTR, CPM and scroll-stop are not attributed and never change. cv()
  // is the one read-point — every purchase/revenue/cart/checkout number on the
  // reporting surfaces goes through it, so switching the window re-weights them
  // all at once. `default` == the flat payload fields (the account's own
  // attribution setting), so it is byte-identical to the pre-feature dashboard.
  var ATTR='default';
  function cv(a,m){
    if(ATTR==='default'||!a.attr) return a[m]||0;
    var w=a.attr[ATTR];
    return (w && w[m]!=null) ? w[m] : (a[m]||0);
  }
  var ATTR_LABEL={'default':'account default','1d_view':'1-day view',
    '1d_click':'1-day click','7d_click':'7-day click','28d_click':'28-day click'};
  var HAS_ATTR=ADS.some(function(a){return a.attr;});

  function renderAttrNote(all){
    var el=document.getElementById('attrNote'); if(!el) return;
    if(!HAS_ATTR){ el.style.display='none'; return; }
    el.style.display='';
    var base='<b>Attribution:</b> conversions credited on the <b>'+ATTR_LABEL[ATTR]+
      '</b> window. Only conversions move with this — purchases, revenue, ROAS, CVR '+
      'and the funnel’s cart/checkout/purchase steps. Spend, impressions, clicks, CTR, '+
      'CPM and scroll-stop are not attributed and never change.';
    // Meta stopped reporting 7- and 28-day VIEW-through after iOS 14, so only
    // 1-day view is offered; the account default is typically 7-day click plus
    // 1-day view. State it, since a missing option otherwise reads as an omission.
    var caveat=' Meta only returns 1-day view-through (7- and 28-day view were '+
      'retired after iOS 14); the click windows are cumulative. The '+
      '<b>scale/kill</b> chart and the tile <b>sparklines</b> read the account default '+
      'attribution regardless of this setting — they are built on the daily/weekly series, '+
      'which isn’t stored per window.';
    var extra='';
    if(ATTR==='1d_click'||ATTR==='1d_view'){
      extra=' At <b>'+ATTR_LABEL[ATTR]+'</b>, credit is at its strictest, so ROAS and '+
        'purchases read lower than the account default — that gap is the share of '+
        'conversions the default window claims from '+(ATTR==='1d_click'?'view-through and longer click paths':'clicks and longer paths')+'.';
    }else if(ATTR==='28d_click'){
      extra=' At <b>28-day click</b>, the longest window, credit is at its most generous — '+
        'ROAS reads highest here because slow-converting click paths are all counted.';
    }
    el.innerHTML=base+extra+caveat;
  }

  // --- aggregation (single definition, used for buckets and baselines) ---
  function agg(list){
    var im=0,sp=0,ck=0,pu=0,rv=0,v3=0,vim=0,vads=0;
    for(var i=0;i<list.length;i++){var a=list[i];
      im+=a.im||0; sp+=a.sp||0; ck+=a.ck||0; pu+=cv(a,'pu'); rv+=cv(a,'rv');
      v3+=a.v3||0;
      // Scroll-stop rate is a VIDEO metric. Static and catalog-image ads can
      // never register a 3-second view, so counting their impressions in the
      // denominator drags the rate toward zero for reasons that have nothing to
      // do with the creative. Only ads that actually served video (vp>0) are
      // counted, and vads is surfaced so a thin video sample is visible.
      if((a.vp||0)>0){ vim+=a.im||0; vads++; }
    }
    return {ads:list.length,im:im,sp:sp,ck:ck,pu:pu,rv:rv,
      ctr:im?100*ck/im:0, cpm:im?1000*sp/im:0, roas:sp?rv/sp:0,
      // Conversion rate is purchases per CLICK, matching the CTR tile's
      // denominator so impressions → clicks → purchases reads as one chain.
      cvr:ck?100*pu/ck:0,
      v3:v3, vim:vim, vads:vads, ssr:vim?100*v3/vim:null};
  }
  function ssrCell(st){
    if(st.ssr===null) return '<span class="flat" title="no video ads in this bucket">&mdash;</span>';
    return st.ssr.toFixed(2)+'%<span class="vn" title="'+st.vads+
      ' video ad'+(st.vads===1?'':'s')+' of '+st.ads+'">&nbsp;('+st.vads+')</span>';
  }
  function idxCell(v){
    if(!v||!isFinite(v)) return '<span class="flat">&mdash;</span>';
    var c=v>=105?'up':(v<=95?'down':'flat');
    return '<span class="idx '+c+'">'+v.toFixed(0)+'</span>';
  }

  // ================================================================ funnel ===
  // Stage order is the real user journey. `vid:true` marks the two stages whose
  // denominator is video impressions rather than all impressions — a static ad
  // physically cannot register a 3-second view, so charting it against total
  // impressions would invent a drop-off that is really just media mix.
  var STAGES=[
    {k:'im', lab:'Impressions'},
    {k:'v3', lab:'3s hook', vid:true},
    {k:'tp', lab:'Thruplay', vid:true},
    {k:'lc', lab:'Link click'},
    {k:'atc',lab:'Add to cart'},
    {k:'ic', lab:'Checkout'},
    {k:'pu', lab:'Purchase'}
  ];

  function funnelOf(list){
    var t={im:0,v3:0,tp:0,lc:0,atc:0,ic:0,pu:0,sp:0,rv:0,vim:0,vads:0};
    for(var i=0;i<list.length;i++){var a=list[i];
      // Impressions → 3s → thruplay → link-click are NOT attributed and stay
      // fixed across windows; cart, checkout, purchase and revenue re-weight.
      t.im+=a.im||0; t.v3+=a.v3||0; t.tp+=a.tp||0; t.lc+=a.lc||0;
      t.atc+=cv(a,'atc'); t.ic+=cv(a,'ic'); t.pu+=cv(a,'pu');
      t.sp+=a.sp||0; t.rv+=cv(a,'rv');
      if((a.vp||0)>0){ t.vim+=a.im||0; t.vads++; }
    }
    t.ads=list.length;
    t.roas=t.sp?t.rv/t.sp:0;
    t.cpa=t.pu?t.sp/t.pu:0;
    // Each stage as a share of ITS OWN denominator, and of the previous stage.
    t.rates={}; t.steps={};
    var prev=null;
    STAGES.forEach(function(s){
      var den=s.vid?t.vim:t.im;
      var v=s.k==='im'?t.im:t[s.k];
      t.rates[s.k]= den? v/den : null;
      // Step conversion compares to the previous stage's raw count. Crossing the
      // video/non-video boundary makes that ratio meaningless (different
      // denominators), so it is suppressed rather than shown as a fake number.
      if(prev && !(prev.vid!==s.vid)) t.steps[s.k]= t[prev.k]? v/t[prev.k] : null;
      else if(prev) t.steps[s.k]=null;
      prev=s;
    });
    return t;
  }

  // Segment options for the two funnel sides. Built from the CURRENT filtered
  // pool so the A/B choices always describe ads that are actually on screen.
  //
  // The axes are BRAND plus CREATIVE ATTRIBUTES (format/class/CTA and every
  // vision attribute). Brand is kept because JD-vs-Finish-Line is the default
  // comparison. Individual ads, campaigns, ad sets and audience targeting are
  // intentionally excluded — the funnel is meant to answer "which brand / which
  // creative treatment converts better", not "which line item spent more", so
  // account structure and targeting would only muddy that read.
  var SEG_CAP=8;   // top values per attribute offered as a funnel side
  function segOptions(pool){
    var out=[{id:'__all__',lab:'All ads in filter',list:pool}];
    function group(key,label,vision,isList){
      var m={};
      pool.forEach(function(a){
        var v = vision ? (a.A?a.A[key]:undefined) : a[key];
        if(v===undefined||v===null||v==='') return;
        if(isList){ if(!Array.isArray(v)) return;
          v.forEach(function(x){ if(x!==''&&x!=null) (m[x]=m[x]||[]).push(a); }); }
        else { (m[v]=m[v]||[]).push(a); }
      });
      Object.keys(m).sort(function(x,y){
        return m[y].reduce(function(s,a){return s+(a.sp||0);},0)
             - m[x].reduce(function(s,a){return s+(a.sp||0);},0);
      }).filter(function(v){
        // Drop thin values, exactly as the attribute tables do — a funnel built
        // on a few hundred impressions is noise.
        return m[v].reduce(function(s,a){return s+(a.im||0);},0)>=MINIMP;
      // …then keep only each attribute's top values by spend. The list
      // attributes (value props, key features) otherwise fan out into hundreds
      // of high-impression tags and bury the dropdown; the scalar attributes sit
      // well under this cap and are unaffected.
      }).slice(0,SEG_CAP).forEach(function(v){
        out.push({id:key+'::'+v,lab:label+': '+v,list:m[v]});
      });
    }
    // Brand is the top-level axis and the default comparison…
    group('b','Brand');
    // …creative-descriptive metadata (known for every ad)…
    group('cl','Creative class'); group('ot','Object type'); group('cta','CTA');
    // …then every vision-derived creative attribute (analyzable ads only).
    VISION_SPECS.forEach(function(s){ group(s[0], s[1], true, s[2]==='list'); });
    return out;
  }

  // Default the funnel to a brand-vs-brand read (JD Sports vs Finish Line). If
  // either brand is filtered out of the current pool, fillSegSelects falls back
  // to the first available option, so this is a preference, not a hard wire.
  var FN_A='b::jdsports', FN_B='b::finishline', FN_MODE='imp';

  function fillSegSelects(pool){
    var opts=segOptions(pool);
    SEGMAP={}; opts.forEach(function(o){SEGMAP[o.id]=o;});
    function fill(el,cur,allowNone,noneLab){
      var html=allowNone?'<option value="">'+esc(noneLab)+'</option>':'';
      html+=opts.map(function(o){
        return '<option value="'+esc(o.id)+'"'+(o.id===cur?' selected':'')+'>'+
          esc(o.lab)+'</option>';}).join('');
      el.innerHTML=html;
      if(cur && !SEGMAP[cur]) el.value='';
    }
    fill(document.getElementById('fnA'),FN_A,false);
    fill(document.getElementById('fnB'),FN_B,true,'(no comparison)');
    if(!SEGMAP[FN_A]) FN_A=opts.length?opts[0].id:'';
    if(FN_B && !SEGMAP[FN_B]) FN_B='';
  }
  var SEGMAP={};

  function fmtRate(v){
    if(v===null||v===undefined||!isFinite(v)) return '—';
    var p=100*v;
    return p>=10?p.toFixed(0)+'%':(p>=1?p.toFixed(1)+'%':p.toFixed(2)+'%');
  }

  function funnelHTML(seg,colorVar,other){
    var t=funnelOf(seg.list);
    if(!t.im) return '';
    // Leak = the stage where this side converts worst RELATIVE to the other
    // side. With no comparison, fall back to the worst absolute step drop.
    // Either way it is flagged on step conversion, not on share-of-impressions,
    // because a stage deep in the funnel always has a small share and would
    // otherwise always look like the problem.
    var leak=null, worst=0;
    STAGES.forEach(function(s,i){
      if(!i) return;
      var mine=t.steps[s.k];
      if(mine===null||mine===undefined||!isFinite(mine)) return;
      if(other){
        var theirs=other.steps[s.k];
        if(theirs===null||theirs===undefined||!isFinite(theirs)||!theirs) return;
        var gap=1-(mine/theirs);           // how far behind the other side
        if(gap>worst+1e-9){worst=gap;leak=s;}
      }else{
        var drop=1-mine;
        if(drop>worst+1e-9){worst=drop;leak=s;}
      }
    });
    // A relative gap under 15% is noise, not a leak worth pointing at.
    if(other && worst<0.15) leak=null;

    var maxW=100;
    var html='<div class="fn"><div class="fnhd">'+
      '<span class="dot" style="background:'+colorVar+'"></span>'+
      '<span class="fnname" title="'+esc(seg.lab)+'">'+esc(seg.lab)+'</span></div>'+
      '<div class="fnmeta">'+num(t.ads)+' ad'+(t.ads===1?'':'s')+' · '+
      money(t.sp)+' spend · '+num(t.im)+' impressions'+
      (t.vads?' · '+num(t.vads)+' video':'')+'</div>';

    STAGES.forEach(function(s,i){
      var den=s.vid?t.vim:t.im;
      if(s.vid && !t.vim) return;           // no video in this segment
      var v=(s.k==='im')?t.im:t[s.k];
      var r=t.rates[s.k];
      var shown=(FN_MODE==='step'&&i)?t.steps[s.k]:r;
      // Bar width always tracks share of the stage's own denominator, so the
      // shape stays a funnel even when the labels show step conversion.
      var w=Math.max(r?Math.pow(r,0.42)*maxW:0, v>0?2:0);
      var isLeak=leak===s;
      html+='<div class="stg'+(isLeak?' leak':'')+'">'+
        '<div class="lab"><span>'+esc(s.lab)+
          (s.vid?'<span class="vn" title="denominator is video-ad impressions only"> · of video</span>':'')+
          '</span><b>'+fmtRate(shown)+'<span class="vn"> · '+num(v)+'</span></b></div>'+
        '<div class="bar" style="width:'+w.toFixed(1)+'%;background:'+colorVar+
          ';opacity:'+(0.35+0.65*(1-i/STAGES.length)).toFixed(2)+'"></div></div>';
    });

    if(leak){
      var mine=t.steps[leak.k];
      var msg;
      if(other){
        msg='<b>Biggest gap — '+esc(leak.lab)+'.</b> '+fmtRate(mine)+
          ' of the previous step converts here, vs '+fmtRate(other.steps[leak.k])+
          ' for the other side — '+(100*worst).toFixed(0)+'% behind.';
      }else{
        msg='<b>Largest drop — '+esc(leak.lab)+'.</b> '+
          fmtRate(1-mine)+' of the previous step is lost here.';
      }
      html+='<div class="leakmsg">'+msg+'</div>';
    }
    html+='<div class="fnfoot">'+
      '<div>ROAS<b>'+t.roas.toFixed(2)+'</b></div>'+
      '<div>CPA<b>'+(t.cpa?money2(t.cpa):'—')+'</b></div>'+
      '<div>Purchases<b>'+num(t.pu)+'</b></div></div></div>';
    return html;
  }

  function renderFunnel(pool){
    fillSegSelects(pool);
    var A=SEGMAP[FN_A], B=FN_B?SEGMAP[FN_B]:null;
    var host=document.getElementById('funnels');
    if(!A){host.innerHTML='<div class="empty">No ads match this filter.</div>';return;}
    var tA=funnelOf(A.list), tB=B?funnelOf(B.list):null;
    host.innerHTML=funnelHTML(A,'var(--sA)',tB)+(B?funnelHTML(B,'var(--sB)',tA):'');
    document.getElementById('fnNote').innerHTML=
      '<b>Reading this:</b> bar length is each stage’s share of its own denominator. '+
      '<b>3s hook</b> and <b>thruplay</b> are divided by the impressions of video ads only — '+
      'a static or catalog image can never register a 3-second view, so pooling it into the '+
      'denominator would show a drop-off that is really just media mix. The remaining stages '+
      'are over all impressions. <b>Purchase</b> counts Meta’s <em>omni</em> conversions '+
      '(the same basis as the ROAS tiles above), which includes in-store and offline orders — '+
      'those never passed through an on-site checkout, so the last step is not strictly a subset '+
      'of the one above it. Leaks are flagged on step-to-step conversion, not share of '+
      'impressions, and only when a side is at least 15% behind its comparison.';
  }

  // ======================================================== rating vs ROAS ===
  // Meta grades ads on three ordinal scales. This is deliberately NOT a scatter:
  // the grade has at most five possible values (and only ever two non-UNKNOWN
  // ones on these accounts), so a continuous x-axis would spread points along a
  // dimension that carries no information. A categorical strip shows the real
  // shape — the spread of ROAS *within* each grade — which is the thing that
  // decides whether the grade is worth acting on.
  var RANK_ORDER=['BELOW_AVERAGE_10','BELOW_AVERAGE_20','BELOW_AVERAGE_35',
                  'AVERAGE','ABOVE_AVERAGE','UNKNOWN'];
  var RANK_LAB={BELOW_AVERAGE_10:'Bottom 10%',BELOW_AVERAGE_20:'Bottom 20%',
    BELOW_AVERAGE_35:'Bottom 35%',AVERAGE:'Average',ABOVE_AVERAGE:'Above average',
    UNKNOWN:'Not rated'};
  var RANK_FIELD='q';
  var RANK_NAME={q:'Quality ranking',e:'Engagement rate ranking',c:'Conversion rate ranking'};

  function renderRank(pool){
    var host=document.getElementById('rankPlot');
    var note=document.getElementById('rankNote');
    // Only ads carrying a rating record, and only those with spend in the
    // rating window — ROAS on the y-axis must come from the same period.
    var pts=[];
    pool.forEach(function(a){
      if(!a.R || !(a.R.sp>0)) return;
      pts.push({g:a.R[RANK_FIELD]||'UNKNOWN', roas:a.R.rv/a.R.sp,
                sp:a.R.sp, nm:a.nm, id:a.id});
    });
    if(!pts.length){
      host.innerHTML='<div class="empty">No rated ads in this filter.</div>';
      note.innerHTML=''; return;
    }
    var groups=RANK_ORDER.filter(function(g){
      return pts.some(function(p){return p.g===g;});});

    var W=980,H=330,PL=64,PR=18,PT=16,PB=52;
    var iw=W-PL-PR, ih=H-PT-PB;
    // Clip the y-axis at p98 so one 60x-ROAS outlier doesn't flatten everything
    // else onto the baseline; clipped points are drawn at the ceiling and the
    // count is disclosed.
    var sorted=pts.map(function(p){return p.roas;}).sort(function(a,b){return a-b;});
    var yMax=Math.max(1,sorted[Math.floor(0.98*(sorted.length-1))]*1.08);
    var clipped=pts.filter(function(p){return p.roas>yMax;}).length;
    var lane=iw/groups.length;
    var spMax=Math.max.apply(null,pts.map(function(p){return p.sp;}));
    var r=function(sp){return 3+9*Math.sqrt(sp/(spMax||1));};
    var yOf=function(v){return PT+ih-Math.min(v,yMax)/yMax*ih;};

    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" '+
      'aria-label="ROAS distribution by '+esc(RANK_NAME[RANK_FIELD])+'">';
    // y gridlines + labels
    for(var i=0;i<=4;i++){
      var v=yMax*i/4, y=yOf(v);
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+y+'" y2="'+y+'"/>'+
         '<text x="'+(PL-9)+'" y="'+(y+4)+'" text-anchor="end">'+v.toFixed(1)+'x</text>';
    }
    s+='<text x="14" y="'+(PT+ih/2)+'" text-anchor="middle" '+
       'transform="rotate(-90 14 '+(PT+ih/2)+')">ROAS</text>';

    groups.forEach(function(g,gi){
      var x0=PL+gi*lane, cx=x0+lane/2;
      var gp=pts.filter(function(p){return p.g===g;});
      s+='<rect class="rlane" x="'+(x0+6)+'" y="'+PT+'" width="'+(lane-12)+
         '" height="'+ih+'" rx="8"/>';
      // Deterministic horizontal jitter: index-derived, not random, so the
      // layout is stable across re-renders and the same ad keeps its spot.
      gp.forEach(function(p,pi){
        var jitter=((pi*2654435761)%1000/1000-0.5)*(lane-34);
        var col=p.g==='UNKNOWN'?'var(--dim)':'var(--sA)';
        s+='<circle class="rdot" cx="'+(cx+jitter).toFixed(1)+'" cy="'+yOf(p.roas).toFixed(1)+
           '" r="'+r(p.sp).toFixed(1)+'" fill="'+col+'"><title>'+
           esc((p.nm||'').slice(0,80))+'\nROAS '+p.roas.toFixed(2)+'x · '+money(p.sp)+
           ' spend</title></circle>';
      });
      // Spend-weighted ROAS for the lane — the number that matters, since an
      // unweighted median lets a $40 ad count as much as a $400k one.
      var sp=gp.reduce(function(t,p){return t+p.sp;},0);
      var rv=gp.reduce(function(t,p){return t+p.sp*p.roas;},0);
      var wr=sp?rv/sp:0;
      s+='<line class="rmed" x1="'+(x0+14)+'" x2="'+(x0+lane-14)+'" y1="'+yOf(wr)+
         '" y2="'+yOf(wr)+'"/>'+
         '<text x="'+cx+'" y="'+(H-PB+20)+'" text-anchor="middle" '+
         'style="fill:var(--fg);font-weight:600">'+esc(RANK_LAB[g]||g)+'</text>'+
         '<text x="'+cx+'" y="'+(H-PB+36)+'" text-anchor="middle">'+gp.length+
         ' ads · '+money(sp)+' · '+wr.toFixed(2)+'x</text>';
    });
    s+='</svg>';
    host.innerHTML=s;

    var rated=pts.filter(function(p){return p.g!=='UNKNOWN';});
    note.innerHTML='<b>Reading this:</b> each dot is one ad, sized by spend; the heavy line '+
      'is the lane’s <em>spend-weighted</em> ROAS. Meta’s rankings are ordinal — there are only '+
      'ever a handful of possible grades — so this is a strip plot rather than a scatter; a '+
      'continuous x-axis would imply precision the grade does not have. '+
      '<b>'+rated.length+'</b> of '+pts.length+' ads in this filter carry a '+
      esc(RANK_NAME[RANK_FIELD].toLowerCase())+'. Ratings are only defined over recent '+
      'delivery, so both axes here use the <b>'+(RANKWIN?esc(RANKWIN[0]+' – '+RANKWIN[1]):'ratings')+
      '</b> window, not the 90-day window driving the rest of the page — an ad needs roughly '+
      '500 recent impressions before Meta grades it at all, which is why unrated ads skew small. '+
      (clipped?'<b>'+clipped+'</b> ad'+(clipped===1?'':'s')+' above '+yMax.toFixed(1)+
        'x ROAS '+(clipped===1?'is':'are')+' drawn at the ceiling so the rest stays readable. ':'')+
      'Grade and return are correlated across ads that already survived budget decisions — '+
      'this is not evidence that improving a grade would raise ROAS.';
  }

  // =========================================================== scale / kill ===
  // Zone boundaries are a confidence band, not a flat threshold. An ad with $40
  // of spend and one conversion has a CPA estimate with enormous error bars;
  // an ad with $40,000 and 900 conversions does not. Both boundaries therefore
  // converge on the target as spend grows:
  //     kill  = target * (1 + K/sqrt(n))
  //     scale = target / (1 + K/sqrt(n))
  // where n is expected conversions at target (spend/target) and K=2 is roughly
  // two standard errors. That is what produces the reference's decaying kill
  // curve and rising scale curve — an ad is only called when its spend is
  // enough to distinguish it from the target.
  var SK_K=2.0, SK_METRIC='cpa', SK_TARGET=null, SK_FRAME=null, SK_PLAYING=false, SK_TIMER=null;
  var SK_SPEED=1;   // timeline playback speed multiplier (1× / 0.5× / 0.25×)
  var SK_LAUNCH='', SK_ZONE='k';   // default to the kill list; '' = every zone
  // Fractional reduction applied to the auto (account-average) target: 0 =
  // account average, 0.10 = a target 10% tougher than average, etc. This is the
  // "reduce by 5/10/15/20%" control. For the cost metrics (CPA, CPM, cost/3s
  // view) reducing the number is a stricter goal; the READ note states the
  // effective target so the direction is never ambiguous.
  var SK_ADJ=0;
  // Metric registry. `lower` marks a cost metric (smaller is better); ROAS is
  // the one return metric. `video` restricts the population to ads that served
  // video (a 3-second view is a video-only event). `num`/`den` compute the
  // ratio from an accumulated totals object. CPM and cost/3s view are the
  // upper-funnel / prospecting metrics the account team asked for.
  var SK_MET={
    cpa:{lab:'CPA',   lower:true,  video:false, val:function(t){return t.pu?t.sp/t.pu:null;}},
    roas:{lab:'ROAS', lower:false, video:false, val:function(t){return t.sp?t.rv/t.sp:0;}},
    cpm:{lab:'CPM',   lower:true,  video:false, val:function(t){return t.im?1000*t.sp/t.im:null;}},
    cpv:{lab:'Cost / 3s view', lower:true, video:true, val:function(t){return t.v3?t.sp/t.v3:null;}}
  };
  function skDef(){ return SK_MET[SK_METRIC]; }
  function skLower(){ return skDef().lower; }
  function skValue(t){ return skDef().val(t); }
  // Value formatter for the active metric. Cost-per-3s-view lives in cents, so
  // it needs more precision than the whole-dollar-ish CPA/CPM.
  function skFmt(v){
    if(v===null||v===undefined||!isFinite(v)) return '—';
    if(SK_METRIC==='roas') return v.toFixed(2)+'x';
    if(SK_METRIC==='cpv')  return '$'+v.toFixed(3);
    return money2(v);
  }


  // Launch date is the ad's CREATION date, not the first day it delivered in
  // this window. The distinction matters: 166 of these ads were built before
  // the window opened, so an ad first seen spending on day 1 may be a
  // long-running asset rather than a new one. Filtering on first-delivery would
  // label every one of those a fresh launch.
  function skLaunchPass(a){
    if(!SK_LAUNCH) return true;
    var cr=a.cr||'';
    if(!cr) return false;                       // unknown creation date
    if(SK_LAUNCH==='__new__')  return DAYS.length ? cr>=DAYS[0] : true;
    if(SK_LAUNCH==='__prior__')return DAYS.length ? cr< DAYS[0] : false;
    return cr.slice(0,7)===SK_LAUNCH;           // a month cohort
  }

  function fillLaunchSelect(pool){
    var el=document.getElementById('skLaunch');
    var months={}, nNew=0, nPrior=0, nUnknown=0;
    var w0=DAYS.length?DAYS[0]:null;
    pool.forEach(function(a){
      var cr=a.cr||'';
      if(!cr){nUnknown++;return;}
      months[cr.slice(0,7)]=(months[cr.slice(0,7)]||0)+1;
      if(w0){ if(cr>=w0) nNew++; else nPrior++; }
    });
    var keys=Object.keys(months).sort().reverse();
    var html='<option value="">Launched: any date</option>';
    if(nNew)   html+='<option value="__new__">Launched: new in this window · '+nNew+'</option>';
    if(nPrior) html+='<option value="__prior__">Launched: before this window · '+nPrior+'</option>';
    keys.forEach(function(k){
      html+='<option value="'+esc(k)+'">Launched: '+esc(k)+' · '+months[k]+'</option>';
    });
    el.innerHTML=html;
    // A cohort can vanish when the main filter bar narrows the pool; fall back
    // to "any" rather than silently showing an empty chart.
    var valid=(SK_LAUNCH==='')||(SK_LAUNCH==='__new__'&&nNew)||
              (SK_LAUNCH==='__prior__'&&nPrior)||months[SK_LAUNCH];
    if(!valid) SK_LAUNCH='';
    el.value=SK_LAUNCH;
    el.closest('.chip').classList.toggle('set', !!SK_LAUNCH);
    return {unknown:nUnknown};
  }

  function skBand(sp,target){
    var n=target>0?sp/target:0;
    var f=1+SK_K/Math.sqrt(Math.max(n,0.25));
    return {kill:target*f, scale:target/f, f:f};
  }
  // For ROAS the good direction flips: scale above the band, kill below.
  function skBandR(sp,target,cpaProxy){
    var n=cpaProxy>0?sp/cpaProxy:0;
    var f=1+SK_K/Math.sqrt(Math.max(n,0.25));
    return {kill:target/f, scale:target*f, f:f};
  }

  // Cumulative totals for each ad up to and including day index `upto`.
  // Daily row layout: [dayIdx, spend, purchases, revenue, impressions,
  // video_3s, video_plays].
  function skTotals(pool,upto){
    var out=[];
    for(var i=0;i<pool.length;i++){
      var a=pool[i], d=a.D;
      if(!d||!d.length) continue;
      var sp=0,pu=0,rv=0,im=0,v3=0,vp=0,first=null,last=null;
      for(var j=0;j<d.length;j++){
        if(d[j][0]>upto) break;
        sp+=d[j][1]; pu+=d[j][2]; rv+=d[j][3];
        im+=d[j][4]||0; v3+=d[j][5]||0; vp+=d[j][6]||0;
        if(first===null) first=d[j][0];
        last=d[j][0];
      }
      if(sp<=0) continue;
      out.push({a:a,sp:sp,pu:pu,rv:rv,im:im,v3:v3,vp:vp,first:first,last:last,
                cpa:pu?sp/pu:null, roas:sp?rv/sp:0});
    }
    return out;
  }

  // An ad with no conversions has no CPA, but "no conversions" is not the same
  // as "no information". Once an ad has spent enough that it should have
  // produced several conversions at target and produced none, that IS the
  // finding — treating it as WAIT would quietly protect the worst ads on the
  // account from ever being called. NOCONV_N is how many expected-at-target
  // conversions must have been missed before the call is made.
  var NOCONV_N=3;
  function skClassify(t,target){
    var lower=skLower();
    var v=skValue(t);
    // A cost metric with a zero denominator (no purchases / no 3s views) has no
    // ratio. For every cost metric, n = spend/target is the count of events
    // expected at target, so the same "spent enough to have produced NOCONV_N
    // and produced none → kill" rule applies uniformly.
    if(lower && v===null){
      var expected=target>0?t.sp/target:0;
      return expected>=NOCONV_N?'k':'w';
    }
    var b=lower?skBand(t.sp,target):skBandR(t.sp,target,SK_CPAPROXY);
    if(lower) return v<=b.scale?'g':(v>=b.kill?'k':'w');
    return v>=b.scale?'g':(v<=b.kill?'k':'w');
  }
  var SK_CPAPROXY=1;

  // `light` is set on timeline animation frames: the scatter, zone bars and
  // timeline still update, but the per-zone dashboard sections (three charts + a
  // table each) are skipped, since rebuilding them ~9×/second is what made the
  // animation stutter. They refresh in full the moment playback pauses or ends.
  function renderSK(pool, light){
    var host=document.getElementById('skPlot');
    var hasDaily=pool.filter(function(a){return a.D&&a.D.length;});
    var launchInfo=fillLaunchSelect(hasDaily);
    var withDaily=hasDaily.filter(skLaunchPass);
    var launchHidden=hasDaily.length-withDaily.length;
    // Cost-per-3s-view is a video-only event: an ad that never served video can
    // never register one, so it is dropped from this metric's population rather
    // than plotted at an infinite cost. The count is disclosed in the note.
    var nonVideo=0;
    if(skDef().video){
      var vids=withDaily.filter(function(a){
        var d=a.D; for(var j=0;j<d.length;j++){ if((d[j][6]||0)>0) return true; } return false;
      });
      nonVideo=withDaily.length-vids.length;
      withDaily=vids;
    }
    if(!DAYS.length||!withDaily.length){
      host.innerHTML='<div class="empty">'+
        (skDef().video&&nonVideo
          ? 'No video ads in this filter — cost per 3-second view only applies to ads that '+
            'served video.'
          : 'No daily data for this filter — run '+
            '<code>intel perf-series --increment 1</code> to populate it.')+'</div>';
      document.getElementById('skZones').innerHTML='';
      document.getElementById('skTl').style.display='none';
      document.getElementById('skNote').innerHTML='';
      document.getElementById('skTable').innerHTML='';
      return;
    }
    document.getElementById('skTl').style.display='';
    if(SK_FRAME===null) SK_FRAME=DAYS.length-1;

    var tot=skTotals(withDaily,SK_FRAME);
    // Default target = the filtered population's own blended value for the active
    // metric at the full window, so "target" means "the account's current
    // average" until the user types a real business target or dials in a
    // reduction. CPM = cost per 1000 impressions; cost/3s view = spend per view.
    var full=skTotals(withDaily,DAYS.length-1);
    var fsp=full.reduce(function(s,t){return s+t.sp;},0);
    var fpu=full.reduce(function(s,t){return s+t.pu;},0);
    var frv=full.reduce(function(s,t){return s+t.rv;},0);
    var fim=full.reduce(function(s,t){return s+t.im;},0);
    var fv3=full.reduce(function(s,t){return s+t.v3;},0);
    SK_CPAPROXY=fpu?fsp/fpu:1;
    var autoTarget=({cpa:fpu?fsp/fpu:0, roas:fsp?frv/fsp:0,
                     cpm:fim?1000*fsp/fim:0, cpv:fv3?fsp/fv3:0})[SK_METRIC];
    // Base target is a typed value if present, else the account average; the
    // reduction control then tightens it. Typing and the −% buttons are mutually
    // exclusive (each clears the other), so this never compounds.
    var base=(SK_TARGET!==null&&SK_TARGET>0)?SK_TARGET:autoTarget;
    var target=base*(1-SK_ADJ);
    var tin=document.getElementById('skTarget');
    tin.step=(SK_METRIC==='cpv')?'0.001':'0.01';
    if(document.activeElement!==tin) tin.value=(SK_METRIC==='cpv')?target.toFixed(3):target.toFixed(2);
    // Reflect the active reduction on the −% control.
    document.querySelectorAll('#skAdj .seg').forEach(function(b){
      b.classList.toggle('on', (+b.dataset.adj)===SK_ADJ);
    });

    var buckets={g:[],w:[],k:[]};
    tot.forEach(function(t){ t.z=skClassify(t,target); buckets[t.z].push(t); });

    // --- header segments -----------------------------------------------------
    var zl=[['g','SCALE'],['w','WAIT'],['k','KILL']];
    var totSp=tot.reduce(function(s,t){return s+t.sp;},0)||1;
    // A zone with no ads can't be selected; if the active one empties out (a
    // filter change), fall back to showing all rather than an empty list.
    if(SK_ZONE && !buckets[SK_ZONE].length) SK_ZONE='';
    var zhost=document.getElementById('skZones');
    zhost.className='skzones skzs'+(SK_ZONE?' picked':'');
    zhost.innerHTML=zl.map(function(z){
      var b=buckets[z[0]], sp=b.reduce(function(s,t){return s+t.sp;},0);
      if(!b.length) return '';
      return '<div class="skz '+z[0]+(SK_ZONE===z[0]?' on':'')+'" data-z="'+z[0]+'" '+
        'role="button" tabindex="0" aria-pressed="'+(SK_ZONE===z[0])+'" '+
        'title="Click to list the '+z[1].toLowerCase()+' ads below" '+
        'style="flex:'+Math.max(sp/totSp,0.08)+'">'+
        '<span class="n">'+b.length+'</span><span>'+z[1]+'</span>'+
        '<span class="sp">'+money(sp)+'</span></div>';
    }).join('');
    zhost.querySelectorAll('.skz').forEach(function(el){
      var pick=function(){
        SK_ZONE=(SK_ZONE===el.dataset.z)?'':el.dataset.z;   // click again to clear
        renderSK(ADS.filter(passes));
      };
      el.addEventListener('click',pick);
      el.addEventListener('keydown',function(e){
        if(e.key==='Enter'||e.key===' '){e.preventDefault();pick();}
      });
    });

    // --- scatter -------------------------------------------------------------
    var W=980,H=420,PL=66,PR=110,PT=14,PB=54;
    var iw=W-PL-PR, ih=H-PT-PB;
    var lower=skLower();
    var spMax=Math.max(100,Math.max.apply(null,full.map(function(t){return t.sp;})));
    var lx=function(sp){return PL+(Math.log10(Math.max(sp,10))-1)/(Math.log10(spMax)-1)*iw;};
    var vals=tot.map(skValue)
                .filter(function(v){return v!==null&&isFinite(v);}).sort(function(a,b){return a-b;});
    var vMax=vals.length?Math.max(target*2, vals[Math.floor(0.95*(vals.length-1))]*1.1):target*2;
    // A few pathological ads — most visibly a "video" ad with almost no 3-second
    // views, whose cost per 3s view runs to thousands of dollars — otherwise drag
    // a p95-based axis so high that the target line and the whole decision zone
    // collapse onto the baseline. Cap the axis at a fixed multiple of target for
    // the cost metrics; ads past the cap pin to the ceiling and are counted in
    // the note. Normal CPA/CPM sit well under 10× target, so this only bites the
    // pathological tail and leaves those charts unchanged.
    if(lower) vMax=Math.min(vMax, target*10);
    var yOf=function(v){return PT+ih-Math.min(v,vMax)/vMax*ih;};

    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+
      'Ads by cumulative spend and '+esc(skDef().lab)+'">';
    for(var i=0;i<=4;i++){
      var v=vMax*i/4,y=yOf(v);
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+y+'" y2="'+y+'"/>'+
         '<text x="'+(PL-9)+'" y="'+(y+4)+'" text-anchor="end">'+skFmt(v)+'</text>';
    }
    [10,100,1000,10000,100000].forEach(function(t){
      if(t>spMax*1.4) return;
      var x=lx(t);
      if(x<PL-1||x>W-PR+1) return;
      s+='<line class="gridln" x1="'+x+'" x2="'+x+'" y1="'+PT+'" y2="'+(PT+ih)+'"/>'+
         '<text x="'+x+'" y="'+(PT+ih+20)+'" text-anchor="middle">'+
         (t>=1000?'$'+(t/1000)+'k':'$'+t)+'</text>';
    });
    s+='<text x="'+(PL+iw/2)+'" y="'+(H-12)+'" text-anchor="middle">CUMULATIVE SPEND (LOG)</text>'+
       '<text x="14" y="'+(PT+ih/2)+'" text-anchor="middle" transform="rotate(-90 14 '+
       (PT+ih/2)+')">'+esc(skDef().lab.toUpperCase())+'</text>';

    // zone bands, sampled across the log axis
    var killPts=[],scalePts=[];
    for(var px=0;px<=60;px++){
      var sp=Math.pow(10,1+(px/60)*(Math.log10(spMax)-1));
      var b=lower?skBand(sp,target):skBandR(sp,target,SK_CPAPROXY);
      killPts.push([lx(sp),yOf(b.kill)]);
      scalePts.push([lx(sp),yOf(b.scale)]);
    }
    var path=function(p){return p.map(function(q,i){
      return (i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1);}).join('');};
    var topY=PT, botY=PT+ih;
    // Kill region fills away from target: upward for a cost metric (high cost is
    // bad), downward for ROAS (low return is bad).
    var killFill=lower
      ? path(killPts)+'L'+lx(spMax)+' '+topY+'L'+PL+' '+topY+'Z'
      : path(killPts)+'L'+lx(spMax)+' '+botY+'L'+PL+' '+botY+'Z';
    var scaleFill=lower
      ? path(scalePts)+'L'+lx(spMax)+' '+botY+'L'+PL+' '+botY+'Z'
      : path(scalePts)+'L'+lx(spMax)+' '+topY+'L'+PL+' '+topY+'Z';
    s+='<path d="'+killFill+'" fill="var(--zBad)" fill-opacity="0.10"/>'+
       '<path d="'+scaleFill+'" fill="var(--zGood)" fill-opacity="0.10"/>'+
       '<path d="'+path(killPts)+'" fill="none" stroke="var(--zBad)" stroke-width="1.5" stroke-dasharray="5 4"/>'+
       '<path d="'+path(scalePts)+'" fill="none" stroke="var(--zGood)" stroke-width="1.5" stroke-dasharray="5 4"/>'+
       '<line x1="'+PL+'" x2="'+(W-PR)+'" y1="'+yOf(target)+'" y2="'+yOf(target)+
       '" stroke="var(--fg)" stroke-width="1.5" stroke-dasharray="7 5" opacity=".55"/>'+
       '<text x="'+(W-PR+8)+'" y="'+(yOf(target)+4)+'" style="fill:var(--fg)">TARGET</text>'+
       '<text x="'+(W-PR+8)+'" y="'+(yOf(target)-16)+'" style="fill:var(--zBad)">'+
       (lower?'KILL':'SCALE')+'</text>'+
       '<text x="'+(W-PR+8)+'" y="'+(yOf(target)+24)+'" style="fill:var(--zGood)">'+
       (lower?'SCALE':'KILL')+'</text>';

    var COL={g:'var(--zGood)',w:'var(--zWait)',k:'var(--zBad)'};
    var skClipped=0, skNoConv=0;
    tot.slice().sort(function(a,b){return a.sp-b.sp;}).forEach(function(t){
      var v=skValue(t);
      if(v===null||!isFinite(v)){ v=lower?vMax:0; skNoConv++; }
      else if(v>vMax) skClipped++;
      s+='<circle class="skdot" data-id="'+esc(t.a.id)+'" cx="'+lx(t.sp).toFixed(1)+
         '" cy="'+yOf(v).toFixed(1)+'" r="'+(4+4*Math.sqrt(t.sp/spMax)).toFixed(1)+
         '" fill="'+COL[t.z]+'" fill-opacity=".72"><title>'+
         esc((t.a.nm||'').slice(0,80))+'\n'+money(t.sp)+' · '+esc(skDef().lab)+' '+
         (skValue(t)===null?'—':skFmt(skValue(t)))+'</title></circle>';
    });
    s+='</svg>';
    host.innerHTML=s;
    host.querySelectorAll('.skdot').forEach(function(c){
      c.addEventListener('click',function(){showAd(c.dataset.id,target);});
    });

    // --- timeline + per-zone dashboard sections -------------------------------
    renderTimeline(withDaily);
    if(!light) renderZoneSections(buckets,target);

    var wrong=buckets.k.reduce(function(s,t){return s+t.sp;},0);
    var isAuto=!(SK_TARGET!==null&&SK_TARGET>0) && SK_ADJ===0;
    var lab=skDef().lab, mfmt=skFmt;
    // The countable event behind each metric's confidence band, for the note.
    var noun={cpa:'conversions', roas:'conversions',
              cpm:'thousand impressions', cpv:'3-second views'}[SK_METRIC];
    var lbl={'__new__':'launched during this window',
             '__prior__':'launched before this window'}[SK_LAUNCH] ||
             (SK_LAUNCH?('launched in '+SK_LAUNCH):'');
    // A cohort filter changes the population every number below describes, so
    // it is stated up front rather than left to be inferred from the chip.
    var launchLine = SK_LAUNCH
      ? '<b>Cohort:</b> showing only assets <b>'+esc(lbl)+'</b> — '+withDaily.length+
        ' of '+hasDaily.length+' ads ('+launchHidden+' hidden). Spend, zones and the '+
        'timeline below all describe this cohort only.<br><br>'
      : '';
    // When the reduction control is engaged, the target is no longer the account
    // average — say so and by how much, because otherwise "wrong zone" would read
    // against a number the user can't see.
    var adjLine = SK_ADJ>0
      ? '<b>Target:</b> '+mfmt(target)+' — <b>'+(100*SK_ADJ).toFixed(0)+'% tighter</b> than '+
        'this filter’s account average of '+mfmt(autoTarget)+'. '+
        (lower
          ? 'A lower '+lab+' is a stricter goal, so more ads fall into KILL than at the average.'
          : 'Note a lower ROAS target is an <em>easier</em> goal, so more ads clear SCALE.')+
        '<br><br>'
      : '';
    // The default target is the population's OWN blended rate, so roughly half
    // the spend sits on the wrong side of it by construction. Calling that
    // "underperforming" would be a sleight of hand — against a self-referential
    // target the only honest claim is "worse than your own average".
    var readLine = isAuto
      ? '<b>READ:</b> against this filter’s own blended '+esc(lab)+
        ' ('+mfmt(target)+'), '+
        buckets.g.length+' ad'+(buckets.g.length===1?'':'s')+' beat it with enough spend to '+
        'be confident and '+buckets.k.length+' trail it — <b>'+money(wrong)+'</b> ('+
        (100*wrong/totSp).toFixed(0)+'% of spend in view). Note this target is the average '+
        'of the ads being measured, so about half the spend sits below it by construction; '+
        'type a real business target above, or tighten it with the −% control, to judge against '+
        'something external.'
      : '<b>READ:</b> '+buckets.g.length+' ad'+(buckets.g.length===1?'':'s')+
        ' clearing the scale line · '+buckets.k.length+' in kill · <b>'+money(wrong)+
        '</b> ('+(100*wrong/totSp).toFixed(0)+'% of spend in view) sitting in the wrong zone '+
        'against a target of '+mfmt(target)+'.';
    document.getElementById('skNote').innerHTML= launchLine + adjLine + readLine +
      (skDef().video&&nonVideo
        ? '<br><br><b>'+nonVideo+'</b> non-video ad'+(nonVideo===1?'':'s')+' '+
          (nonVideo===1?'is':'are')+' excluded — a 3-second view is a video-only event.'
        : '')+
      '<br><br><b>Launch date</b> is when the asset was created in the ad account, not when '+
      'it first delivered inside this window — '+
      'an ad can be built months before it starts spending, so first-delivery would label '+
      'long-running assets as new launches. '+
      (launchInfo.unknown?'<b>'+launchInfo.unknown+'</b> ad'+(launchInfo.unknown===1?'':'s')+
        ' carry no creation date and drop out of every cohort. ':'')+
      '<br><br><b>How the zones are drawn:</b> they are a confidence band, not a fixed '+
      'threshold. A low-spend ad has a '+esc(lab)+' you cannot trust; a high-spend one you '+
      'can. Both boundaries are target × (1 ± '+SK_K.toFixed(0)+'/√n), with n the '+esc(noun)+
      ' expected at target, so they converge on the target as spend accumulates and an ad is '+
      'only called once it has earned the call. That is why low-spend ads sit in WAIT almost '+
      'regardless of their measured rate. '+
      (lower
        ? 'An ad producing none of the '+esc(noun)+' has no '+esc(lab)+', so it stays in WAIT '+
          'until it has spent enough to have expected at least '+NOCONV_N+' at target — past '+
          'that, producing none is itself the verdict, and it is called a kill. '
        : '')+
      (skClipped?'<b>'+skClipped+'</b> ad'+(skClipped===1?'':'s')+' beyond '+
        mfmt(vMax)+' '+(skClipped===1?'is':'are')+
        ' pinned to the '+(lower?'top':'bottom')+' edge so the bulk stays readable. ':'')+
      (skNoConv?'<b>'+skNoConv+'</b> ad'+(skNoConv===1?'':'s')+' with none of the '+esc(noun)+
        ' at all '+(skNoConv===1?'is':'are')+' drawn at the '+(lower?'ceiling':'floor')+'. ':'')+
      'Dot position uses spend accumulated up to the selected day, so dragging the timeline '+
      'replays how each ad actually got where it is.';
  }

  // Money at stake against the target, in dollars — the quantity that makes one
  // ad more urgent than another.
  //
  //   Cost metric (CPA/CPM/cost-per-3s-view): what these EVENTS should have cost
  //     at target, minus what they did cost.
  //   ROAS: what this spend SHOULD have returned, minus what it did.
  //
  // Negative means the ad is behind target and the figure is how much it burned;
  // positive means it beat target and the figure is the surplus it produced.
  // Sorting on this rather than on raw spend is the difference between "biggest
  // ad" and "biggest problem".
  function skEvents(t){
    return SK_METRIC==='cpa'?t.pu:(SK_METRIC==='cpm'?t.im/1000:(SK_METRIC==='cpv'?t.v3:t.pu));
  }
  function skImpact(t,target){
    return (SK_METRIC==='roas') ? (t.rv - t.sp*target) : (skEvents(t)*target - t.sp);
  }

  // A compact single-metric trend line over the weekly buckets. Follows the
  // mark spec: 2px line, 10% area wash, recessive dashed grid, an emphasised
  // end dot with a surface ring, and the latest value called out in the title —
  // one series, so it is named rather than given a legend.
  function trendChart(vals, label, fmt, hue){
    var W=320,H=132,PL=44,PR=12,PT=30,PB=22;
    var wrapOpen='<figure class="tchart"><figcaption class="tcttl">'+esc(label);
    if(!vals || !vals.length){
      return wrapOpen+'</figcaption><div class="tcempty">no weekly trend</div></figure>';
    }
    var n=vals.length;
    var mx=Math.max.apply(null,vals), mn=Math.min.apply(null,vals);
    if(!isFinite(mx)) mx=0; if(!isFinite(mn)) mn=0;
    var top=(mx>0?mx*1.18:1), bot=Math.min(0,mn);
    var iw=W-PL-PR, ih=H-PT-PB;
    var xo=function(i){return PL+(n<2?iw/2:i*iw/(n-1));};
    var yo=function(v){return PT+ih-((v-bot)/((top-bot)||1))*ih;};
    var last=vals[n-1];
    var s='<svg viewBox="0 0 '+W+' '+H+'" class="chart" role="img" aria-label="'+
      esc(label)+' trend">';
    for(var g=0;g<=2;g++){
      var gv=bot+(top-bot)*g/2, gy=yo(gv);
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+gy.toFixed(1)+'" y2="'+gy.toFixed(1)+'"/>'+
         '<text x="'+(PL-6)+'" y="'+(gy+3).toFixed(1)+'" text-anchor="end" style="font-size:9px">'+
         fmt(gv)+'</text>';
    }
    var d='',area='';
    for(var i=0;i<n;i++){ d+=(i?' L':'M')+xo(i).toFixed(1)+' '+yo(vals[i]).toFixed(1); }
    area='M'+xo(0).toFixed(1)+' '+(PT+ih)+' L'+d.slice(1)+' L'+xo(n-1).toFixed(1)+' '+(PT+ih)+' Z';
    s+='<path d="'+area+'" fill="'+hue+'" fill-opacity="0.10"/>'+
       '<path d="'+d+'" fill="none" stroke="'+hue+'" stroke-width="2" '+
       'stroke-linejoin="round" stroke-linecap="round"/>'+
       '<circle cx="'+xo(n-1).toFixed(1)+'" cy="'+yo(last).toFixed(1)+'" r="3" fill="'+hue+
       '" stroke="var(--panel)" stroke-width="2"/>';
    if(BUCKETS.length){
      s+='<text x="'+PL+'" y="'+(H-6)+'" style="font-size:9px">'+esc((BUCKETS[0]||'').slice(5))+'</text>'+
         '<text x="'+(W-PR)+'" y="'+(H-6)+'" text-anchor="end" style="font-size:9px">'+
         esc((BUCKETS[n-1]||'').slice(5))+'</text>';
    }
    s+='</svg>';
    return wrapOpen+'<span class="tcnow" style="color:'+hue+'">'+fmt(last)+'</span>'+
      '</figcaption>'+s+'</figure>';
  }

  // Per-zone dashboard sections: for each classification in scope, a header, a
  // row of key-metric trend charts (scroll-stop, CTR, ROAS) for the ads in that
  // zone, then the ranked action table. Default is the KILL zone only (set by
  // SK_ZONE); clearing the zone selection stacks all three.
  var SK_ROWS_ZONE=40;
  function renderZoneSections(buckets,target){
    var ZL={g:['SCALE','var(--zGood)','g'],w:['WAIT','var(--zWait)','w'],k:['KILL','var(--zBad)','k']};
    var order=SK_ZONE?[SK_ZONE]:['k','g','w'];   // worst first when unfiltered
    var host=document.getElementById('skTable');
    var metLab=skDef().lab, secLab=(SK_METRIC==='roas')?'CPA':'ROAS';
    var secFmt=function(t){return (SK_METRIC==='roas')?(t.cpa?money2(t.cpa):'—'):t.roas.toFixed(2)+'x';};
    var rankHint={
      k:'Ranked by money burned against target — act on the top row first.',
      g:'Ranked by surplus produced against target — the strongest case for more budget first.',
      w:'Ranked by spend at risk — these have not earned a verdict yet.'
    };

    var out=order.map(function(z){
      var b=buckets[z].map(function(t){ t.imp=skImpact(t,target); return t; });
      if(!b.length) return '';
      if(z==='k')      b.sort(function(x,y){return x.imp-y.imp;});   // worst burn first
      else if(z==='g') b.sort(function(x,y){return y.imp-x.imp;});   // biggest surplus first
      else             b.sort(function(x,y){return y.sp-x.sp;});     // undecided: most at risk
      var spSum=b.reduce(function(s,t){return s+t.sp;},0);

      // Trend charts aggregate the weekly series of THIS zone's ads only.
      var S=poolSeries(b.map(function(t){return t.a;}));
      var trends='<div class="ztrends">'+
        trendChart(S&&S.ssr,'Scroll-stop rate',function(v){return v.toFixed(1)+'%';},'var(--sA)')+
        trendChart(S&&S.ctr,'Click-through rate',function(v){return v.toFixed(2)+'%';},'var(--sA)')+
        trendChart(S&&S.roas,'ROAS',function(v){return v.toFixed(2)+'x';},'var(--sA)')+
        '</div>';

      var shown=b.slice(0,SK_ROWS_ZONE);
      var rows=shown.map(function(t){
        var a=t.a, days=(t.last-t.first+1);
        var vNow=skValue(t);
        return '<tr data-id="'+esc(a.id)+'">'+
          '<td><span class="zdot" style="background:'+ZL[z][1]+'"></span>'+
            '<span class="adnm" title="'+esc(a.nm||a.id)+'">'+esc(a.nm||a.id)+'</span>'+
            '<div class="sub">'+(a.cr?'launched '+esc(a.cr)+' &middot; ':'')+days+
            ' day'+(days===1?'':'s')+' delivering'+(a.stage?' &middot; '+esc(a.stage):'')+'</div></td>'+
          '<td>'+money(t.sp)+'</td>'+
          '<td title="'+esc(metLab)+'">'+(vNow===null?'<span class="flat">&mdash;</span>':skFmt(vNow))+'</td>'+
          '<td>'+secFmt(t)+'</td>'+
          '<td style="color:'+(t.imp<0?'var(--zBad)':'var(--zGood)')+';font-weight:600" '+
            'title="'+(t.imp<0
              ? 'Spent '+money(-t.imp)+' more than these events should have cost at target'
              : 'Produced '+money(t.imp)+' more value than target required')+'">'+
            (t.imp<0?'-':'+')+money(Math.abs(t.imp))+'</td>'+
          '<td style="color:'+ZL[z][1]+';font-weight:600">'+ZL[z][0]+'</td></tr>';
      }).join('');

      return '<section class="zsec" data-z="'+z+'">'+
        '<div class="zhd"><span class="zbadge" style="background:'+ZL[z][1]+'">'+ZL[z][0]+'</span>'+
        '<b>'+b.length+' ad'+(b.length===1?'':'s')+'</b><span class="zsp">'+money(spSum)+'</span>'+
        '<span class="hint">'+rankHint[z]+'</span></div>'+
        trends+
        '<div class="tblwrap"><table><thead><tr><th>Ad</th><th>Spend</th>'+
        '<th title="active scale/kill metric">'+esc(metLab)+'</th><th>'+esc(secLab)+'</th>'+
        '<th title="Dollars gained or lost against the target — the sort key">vs target</th>'+
        '<th>Verdict</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
        (b.length>shown.length
          ? '<div class="hint" style="margin-top:6px">Showing the highest-priority '+
            shown.length+' of '+b.length+' '+ZL[z][0].toLowerCase()+' ads.</div>' : '')+
        '</section>';
    }).join('');

    if(!out){ host.innerHTML='<div class="empty">No ads in this selection.</div>'; return; }
    host.innerHTML='<div class="zsecs">'+
      '<div class="hint zseclead">'+(SK_ZONE
        ? 'Showing the <b style="color:'+ZL[SK_ZONE][1]+'">'+ZL[SK_ZONE][0]+
          '</b> section. Click the highlighted bar above again to see all three. Click a row for that ad’s history.'
        : 'One section per verdict — kill, then scale, then wait. Click a bar above to isolate one. '+
          'Trend charts are the weekly scroll-stop, CTR and ROAS for that zone’s ads; click a row for an ad’s history.')+
      '</div>'+out+'</div>';
    host.querySelectorAll('tbody tr').forEach(function(tr){
      tr.addEventListener('click',function(){ showAd(tr.dataset.id,target); });
    });
  }

  // The timeline controls are built ONCE and then only updated in place. The old
  // version rebuilt this whole block's innerHTML on every animation frame, which
  // destroyed and recreated the play button ~9×/second — so a click's mousedown
  // and mouseup landed on different button instances and no click event ever
  // fired, i.e. Pause looked broken. Persisting the controls fixes that; only the
  // bars, range value, label and button/speed state are mutated per frame.
  var TL_BUILT=false;
  var SK_SPEEDS=[[1,'1×'],[0.5,'0.5×'],[0.25,'0.25×']];
  function renderTimeline(pool){
    var host=document.getElementById('skTl');
    var per=new Array(DAYS.length).fill(0);
    pool.forEach(function(a){ (a.D||[]).forEach(function(d){ if(d[0]<per.length) per[d[0]]+=d[1]; }); });
    var mx=Math.max.apply(null,per)||1;
    var bars=per.map(function(v,i){
      return '<i class="'+(i<=SK_FRAME?'on':'')+'" style="height:'+
        Math.max(2,100*v/mx).toFixed(1)+'%"></i>';}).join('');

    if(!TL_BUILT){
      host.innerHTML=
        '<div class="tlhd"><span class="flabel">Timeline</span>'+
        '<span style="color:var(--dim);font-size:12px">Drag to replay how spend accumulated. '+
        'Each ad enters when it first delivered.</span>'+
        '<span class="rng" id="skRng"></span></div>'+
        '<div class="tlbars" id="skBars"></div>'+
        '<div class="tlctl">'+
        '<button class="pbtn" id="skPlay" type="button">▶ Play</button>'+
        '<input type="range" id="skRange" min="0" max="'+(DAYS.length-1)+'" value="'+SK_FRAME+'">'+
        '<span class="tlspeed" id="skSpeed" title="Playback speed">'+
        SK_SPEEDS.map(function(s){
          return '<button class="pbtn spd" data-s="'+s[0]+'" type="button">'+s[1]+'</button>';
        }).join('')+'</span>'+
        '<button class="pbtn" id="skEnd" type="button">Full window</button></div>';
      // Listeners bound ONCE to the persistent controls.
      document.getElementById('skRange').addEventListener('input',function(){
        SK_FRAME=+this.value; stopPlay(); renderSK(ADS.filter(passes));
      });
      document.getElementById('skPlay').addEventListener('click',skPlayToggle);
      document.getElementById('skEnd').addEventListener('click',function(){
        stopPlay(); SK_FRAME=DAYS.length-1; renderSK(ADS.filter(passes));
      });
      document.querySelectorAll('#skSpeed .spd').forEach(function(btn){
        btn.addEventListener('click',function(){ setSpeed(+btn.dataset.s); });
      });
      TL_BUILT=true;
    }

    // Per-frame, in-place updates only — nothing here rebinds a listener.
    document.getElementById('skBars').innerHTML=bars;
    var rng=document.getElementById('skRange');
    rng.max=DAYS.length-1;
    if(document.activeElement!==rng) rng.value=SK_FRAME;
    document.getElementById('skRng').textContent=DAYS[0]+' → '+DAYS[SK_FRAME];
    updateTlControls();
  }

  function updateTlControls(){
    var pb=document.getElementById('skPlay');
    if(pb){ pb.textContent=SK_PLAYING?'❚❚ Pause':'▶ Play'; pb.classList.toggle('on',SK_PLAYING); }
    document.querySelectorAll('#skSpeed .spd').forEach(function(b){
      b.classList.toggle('on',(+b.dataset.s)===SK_SPEED);
    });
  }

  function skStartTimer(){
    if(SK_TIMER) clearInterval(SK_TIMER);
    // Base 130ms at 1×; slower speeds lengthen the interval, which also gives the
    // (heavier, post-zone-sections) render more breathing room per frame.
    SK_TIMER=setInterval(function(){
      SK_FRAME++;
      if(SK_FRAME>=DAYS.length-1){ SK_FRAME=DAYS.length-1; stopPlay(); renderSK(ADS.filter(passes)); return; }
      renderSK(ADS.filter(passes), true);   // light frame: skip the zone sections
    }, Math.round(130/SK_SPEED));
  }

  function skPlayToggle(){
    if(SK_PLAYING){ stopPlay(); renderSK(ADS.filter(passes)); return; }  // full render on pause
    SK_PLAYING=true;
    if(SK_FRAME>=DAYS.length-1) SK_FRAME=0;
    skStartTimer();
    updateTlControls();
    renderSK(ADS.filter(passes), true);
  }

  function setSpeed(s){
    SK_SPEED=s; updateTlControls();
    if(SK_PLAYING) skStartTimer();   // apply the new cadence immediately
  }

  function stopPlay(){
    SK_PLAYING=false;
    if(SK_TIMER){clearInterval(SK_TIMER);SK_TIMER=null;}
    updateTlControls();
  }

  // --- per-ad drill-down: performance since launch --------------------------
  function showAd(id,target){
    var a=null;
    for(var i=0;i<ADS.length;i++){ if(ADS[i].id===id){a=ADS[i];break;} }
    var host=document.getElementById('adPanel');
    if(!a||!a.D){host.innerHTML='';return;}
    var d=a.D;
    var firstIdx=d[0][0], lastIdx=d[d.length-1][0];
    var sp=0,pu=0,rv=0,im=0,v3=0;
    d.forEach(function(x){sp+=x[1];pu+=x[2];rv+=x[3];im+=x[4]||0;v3+=x[5]||0;});
    var cpa=pu?sp/pu:null, roas=sp?rv/sp:0, cpm=im?1000*sp/im:null, cpv=v3?sp/v3:null;

    // The lower panel tracks the SAME metric the scale/kill chart is judging on
    // (CPA / ROAS / CPM / cost per 3s view), so the drill-down explains the
    // verdict the row carried. It shares the spend panel's x-axis but has its
    // own y-scale — stacked, never overlaid: on one pair of axes the crossings
    // between a spend bar and the metric line would be an artefact of two
    // unrelated scales.
    var metLab=skDef().lab;
    var W=940,PL=58,PR=58;
    var H1=140, H2=104, GAP=22, H=H1+GAP+H2+26;
    var iw=W-PL-PR, ih=H1-24, ih2=H2-22;
    var PT=12, PT2=H1+GAP;
    var n=lastIdx-firstIdx+1;
    // byDay row: [spend, purchases, revenue, impressions, video_3s].
    var byDay=new Array(n).fill(null).map(function(){return [0,0,0,0,0];});
    d.forEach(function(x){var k=x[0]-firstIdx;
      byDay[k][0]+=x[1];byDay[k][1]+=x[2];byDay[k][2]+=x[3];byDay[k][3]+=x[4]||0;byDay[k][4]+=x[5]||0;});
    var spMaxD=Math.max.apply(null,byDay.map(function(x){return x[0];}))||1;
    // Cumulative value of the active metric, day by day.
    var cum=[],cs=0,cp=0,crv=0,cim=0,cv3=0;
    byDay.forEach(function(x){cs+=x[0];cp+=x[1];crv+=x[2];cim+=x[3];cv3+=x[4];
      var val;
      if(SK_METRIC==='cpa')       val=cp?cs/cp:null;
      else if(SK_METRIC==='roas') val=cs?crv/cs:null;
      else if(SK_METRIC==='cpm')  val=cim?1000*cs/cim:null;
      else                        val=cv3?cs/cv3:null;
      cum.push(val);
    });
    var cumVals=cum.filter(function(v){return v!==null&&isFinite(v);});
    var lowerM=skLower();
    var cMax=Math.max(target*1.6, cumVals.length?Math.max.apply(null,cumVals)*1.1:target*1.6);
    var bw=iw/n;

    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" '+
      'aria-label="Daily spend and cumulative '+esc(metLab)+' since launch, as two stacked panels">';
    // --- panel 1: daily spend ---
    s+='<text x="'+PL+'" y="'+(PT-1)+'" style="fill:var(--fg);font-weight:600">Daily spend</text>';
    byDay.forEach(function(x,i){
      var h=x[0]/spMaxD*ih;
      s+='<rect x="'+(PL+i*bw+bw*0.15).toFixed(1)+'" y="'+(PT+ih-h).toFixed(1)+
         '" width="'+Math.max(bw*0.7,1).toFixed(1)+'" height="'+Math.max(h,0).toFixed(1)+
         '" fill="var(--sA)" fill-opacity=".65" rx="1"><title>'+esc(DAYS[firstIdx+i])+': '+
         money(x[0])+'</title></rect>';
    });
    s+='<line class="axline" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+(PT+ih)+'" y2="'+(PT+ih)+'"/>'+
       '<text x="'+(PL-8)+'" y="'+(PT+10)+'" text-anchor="end">'+money(spMaxD)+'</text>'+
       '<text x="'+(PL-8)+'" y="'+(PT+ih)+'" text-anchor="end">$0</text>';

    // --- panel 2: cumulative active metric, own scale, same x ---
    s+='<text x="'+PL+'" y="'+(PT2-1)+'" style="fill:var(--fg);font-weight:600">Cumulative '+esc(metLab)+'</text>';
    var ty=PT2+ih2-Math.min(target,cMax)/cMax*ih2;
    s+='<line x1="'+PL+'" x2="'+(W-PR)+'" y1="'+ty+'" y2="'+ty+
       '" stroke="var(--fg)" stroke-width="1.2" stroke-dasharray="6 4" opacity=".5"/>'+
       '<text x="'+(W-PR+6)+'" y="'+(ty+4)+'">target</text>';
    var lp=[];
    cum.forEach(function(v,i){
      if(v===null||!isFinite(v)) return;
      lp.push([PL+i*bw+bw/2, PT2+ih2-Math.min(v,cMax)/cMax*ih2]);
    });
    if(lp.length>1){
      s+='<path d="'+lp.map(function(q,i){return (i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1);}).join('')+
         '" fill="none" stroke="var(--sB)" stroke-width="2"/>'+
         '<circle cx="'+lp[lp.length-1][0].toFixed(1)+'" cy="'+lp[lp.length-1][1].toFixed(1)+
         '" r="3.5" fill="var(--sB)" stroke="var(--panel)" stroke-width="2"/>';
    }
    s+='<line class="axline" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+(PT2+ih2)+'" y2="'+(PT2+ih2)+'"/>'+
       '<text x="'+(PL-8)+'" y="'+(PT2+10)+'" text-anchor="end">'+skFmt(cMax)+'</text>'+
       '<text x="'+(PL-8)+'" y="'+(PT2+ih2)+'" text-anchor="end">'+(lowerM?skFmt(0):'0x')+'</text>'+
       '<text x="'+PL+'" y="'+(H-6)+'">'+esc(DAYS[firstIdx])+'</text>'+
       '<text x="'+(W-PR)+'" y="'+(H-6)+'" text-anchor="end">'+esc(DAYS[lastIdx])+'</text>'+
       '</svg>';

    host.innerHTML='<div class="adp"><div class="aphd"><div>'+
      '<div class="apnm">'+esc(a.nm||a.id)+'</div>'+
      '<div style="color:var(--dim);font-size:12px;margin-top:3px">'+
      (a.cr?'Created '+esc(a.cr)+' · ':'')+'first delivered '+esc(DAYS[firstIdx])+
      ' · last '+esc(DAYS[lastIdx])+' · '+(lastIdx-firstIdx+1)+' days in market'+
      (a.stage?' · '+esc(a.stage):'')+'</div></div>'+
      '<button class="pbtn" onclick="document.getElementById(\'adPanel\').innerHTML=\'\'">Close</button>'+
      '</div><div class="apmet">'+
      '<div>Spend<b>'+money(sp)+'</b></div>'+
      '<div>CPA<b>'+(cpa?money2(cpa):'—')+'</b></div>'+
      '<div>ROAS<b>'+roas.toFixed(2)+'x</b></div>'+
      '<div>CPM<b>'+(cpm?money2(cpm):'—')+'</b></div>'+
      '<div>Cost/3s<b>'+(cpv?'$'+cpv.toFixed(3):'—')+'</b></div>'+
      '<div>Purchases<b>'+num(pu)+'</b></div>'+
      '<div>Revenue<b>'+money(rv)+'</b></div></div>'+
      '<div style="font-size:12px;color:var(--dim);margin:6px 0 4px">'+
      'Two panels on a shared date axis: daily spend above, '+
      '<b style="color:var(--sB)">cumulative '+esc(metLab)+'</b> below — the latter is the '+
      'metric the scale/kill chart is judging on, and it steadies as delivery accumulates. '+
      'They are stacked rather than overlaid on purpose: on one pair of axes the crossings '+
      'between a spend bar and the metric line would be an artefact of two unrelated scales.</div>'+
      s+'</div>';
    host.scrollIntoView({behavior:'smooth',block:'nearest'});
  }

  // --- filtering ---
  var state={};
  FILTERS.forEach(function(f){state[f[0]]='';});
  function passes(a){
    for(var k in state){ if(state[k] && String(a[k]||'')!==state[k]) return false; }
    return true;
  }

  function buildFilterBar(){
    var host=document.getElementById('filterFields');
    host.innerHTML=FILTERS.map(function(f){
      var key=f[0];
      var vals={};
      ADS.forEach(function(a){var v=a[key]; if(v!==undefined&&v!==null&&v!=='') vals[v]=(vals[v]||0)+1;});
      var opts=Object.keys(vals).sort(function(x,y){return vals[y]-vals[x];});
      if(opts.length<2) return '';
      return '<span class="chip" data-for="'+key+'"><select data-k="'+key+'" '+
        'aria-label="'+esc(f[1])+'">'+
        '<option value="">'+esc(f[1])+': All</option>'+
        opts.map(function(o){return '<option value="'+esc(o)+'">'+esc(f[1])+': '+esc(o)+' \u00b7 '+vals[o]+'</option>';}).join('')+
        '</select></span>';
    }).join('');
    host.querySelectorAll('select').forEach(function(sel){
      sel.addEventListener('change',function(){
        state[sel.dataset.k]=sel.value;
        sel.closest('.chip').classList.toggle('set', !!sel.value);
        render();
      });
    });
  }

  // --- card rendering for the drill-down ---
  function card(a){
    var img=a.img?'<img class="thumb" loading="lazy" src="'+esc(a.img)+'" alt="">'
      :'<div class="noimg">no fixed creative<br>(catalog / dynamic ad)</div>';
    var pills='<span class="pill'+(a.cl==='analyzable'?'':' dpa')+'">'+esc(a.cl==='analyzable'?(a.at||'creative'):a.cl)+'</span>';
    if(a.stage) pills+='<span class="pill aud">'+esc(a.stage)+'</span>';
    return '<div class="asset">'+img+'<div class="body">'+pills+
      '<div class="nm" title="'+esc(a.nm)+'">'+esc(a.nm||'(unnamed)')+'</div>'+
      '<div class="mrow"><span>spend</span><b>'+money(a.sp)+'</b></div>'+
      '<div class="mrow"><span>impr.</span><b>'+num(a.im)+'</b></div>'+
      '<div class="mrow"><span>scroll-stop</span><b>'+
        ((a.vp||0)>0&&a.im?(100*(a.v3||0)/a.im).toFixed(2)+'%':'\u2014')+'</b></div>'+
      '<div class="mrow"><span>CTR</span><b>'+(a.im?100*a.ck/a.im:0).toFixed(2)+'%</b></div>'+
      '<div class="mrow"><span>CPM</span><b>$'+(a.im?1000*a.sp/a.im:0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>ROAS</span><b>'+(a.sp?a.rv/a.sp:0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>purch.</span><b>'+num(a.pu)+'</b></div>'+
      '</div></div>';
  }

  // --- one attribute table ---

  // ---- sparkline: 2px line, series hue, 10% wash, end-dot with surface ring ----
  function sparkline(vals, hue){
    var W=104,H=34,pad=3;
    if(!vals || vals.length<2) return '<svg width="'+W+'" height="'+H+'"></svg>';
    var mn=Math.min.apply(null,vals), mx=Math.max.apply(null,vals);
    var span=(mx-mn)||1;
    var n=vals.length;
    var x=function(i){return pad+i*(W-2*pad)/(n-1);};
    var y=function(v){return H-pad-((v-mn)/span)*(H-2*pad);};
    var d='',area='';
    for(var i=0;i<n;i++){ d+=(i?' L':'M')+x(i).toFixed(1)+' '+y(vals[i]).toFixed(1); }
    area='M'+x(0).toFixed(1)+' '+(H-pad)+' L'+d.slice(1)+' L'+x(n-1).toFixed(1)+' '+(H-pad)+' Z';
    var lx=x(n-1).toFixed(1), ly=y(vals[n-1]).toFixed(1);
    return '<svg class="spark" width="'+W+'" height="'+H+'" aria-hidden="true">'+
      '<path d="'+area+'" fill="'+hue+'" fill-opacity="0.10"/>'+
      '<path d="'+d+'" fill="none" stroke="'+hue+'" stroke-width="2" '+
      'stroke-linejoin="round" stroke-linecap="round"/>'+
      '<circle cx="'+lx+'" cy="'+ly+'" r="2.6" fill="'+hue+'" '+
      'stroke="var(--panel)" stroke-width="2"/></svg>';
  }

  // Per-bucket series for the filtered pool. Derived rates are recomputed from
  // summed components per bucket — averaging per-ad rates would let a tiny ad
  // swing the line as hard as a multi-million-impression one.
  function poolSeries(pool){
    var n=BUCKETS.length; if(!n) return null;
    var im=new Array(n).fill(0), sp=new Array(n).fill(0), ck=new Array(n).fill(0),
        pu=new Array(n).fill(0), rv=new Array(n).fill(0),
        v3=new Array(n).fill(0), vim=new Array(n).fill(0);
    var any=false;
    pool.forEach(function(a){
      if(!a.s) return; any=true;
      for(var i=0;i<n;i++){
        im[i]+=a.s.im[i]||0; sp[i]+=a.s.sp[i]||0; ck[i]+=a.s.ck[i]||0;
        pu[i]+=(a.s.pu&&a.s.pu[i])||0;
        rv[i]+=a.s.rv[i]||0; v3[i]+=a.s.v3[i]||0;
        if((a.s.vp[i]||0)>0) vim[i]+=a.s.im[i]||0;
      }
    });
    if(!any) return null;
    // Derived rates are rebuilt from summed components per bucket, never
    // averaged from per-ad rates — a 200-impression ad must not swing a week as
    // hard as a 2M-impression one.
    return {im:im, sp:sp, ck:ck, pu:pu, rv:rv,
      ctr:im.map(function(v,i){return v?100*ck[i]/v:0;}),
      cpm:im.map(function(v,i){return v?1000*sp[i]/v:0;}),
      cvr:ck.map(function(v,i){return v?100*pu[i]/v:0;}),
      roas:sp.map(function(v,i){return v?rv[i]/v:0;}),
      ssr:vim.map(function(v,i){return v?100*v3[i]/v:0;})};
  }

  // Prior period aggregated over the ads that actually ran THEN, matched on the
  // same filter facets. Creative-attribute filters have no counterpart in the
  // prior population (those ads were never vision-analyzed), so only the shared
  // facets apply — which is why the comparison is honest for audience/brand
  // slices and simply broad for the rest.
  var PFIELDS=['b','ac','stage','gen','geo','age','cl'];
  function priorAgg(){
    var im=0,sp=0,ck=0,pu=0,rv=0,v3=0,vim=0,n=0;
    PADS.forEach(function(a){
      for(var i=0;i<PFIELDS.length;i++){
        var k=PFIELDS[i];
        if(state[k] && String(a[k]||'')!==state[k]) return;
      }
      n++; im+=a.im||0; sp+=a.sp||0; ck+=a.ck||0; pu+=cv(a,'pu');
      rv+=cv(a,'rv'); v3+=a.v3||0;
      if((a.vp||0)>0) vim+=a.im||0;
    });
    if(!n) return null;
    return {ads:n,im:im,sp:sp,ck:ck,pu:pu,rv:rv,
      ctr:im?100*ck/im:0, cpm:im?1000*sp/im:0, cvr:ck?100*pu/ck:0,
      roas:sp?rv/sp:0, ssr:vim?100*v3/vim:null};
  }

  function renderKpis(pool, all){
    var S=poolSeries(pool), P=priorAgg();
    // higherIsBetter drives the delta colour — a fall in CPM is good news.
    // Ordered as a funnel: money in, reach, cost of reach, attention, clicks,
    // click rate, conversions, conversion rate, money out, return. `f` is the
    // tile's own formatter so the delta's "prior" value is rendered the same way
    // as its headline \u2014 a shared formatter guessing from the label breaks as
    // soon as two tiles are both percentages with different precision.
    // hib = higher-is-better, which drives the delta COLOUR: a fall in CPM is
    // good news. null = neutral, where up is neither good nor bad.
    var tiles=[
      {k:'Total spend',   cur:all.sp,   prev:P&&P.sp,   s:S&&S.sp,   hib:null, f:money},
      {k:'Impressions',   cur:all.im,   prev:P&&P.im,   s:S&&S.im,   hib:true, f:num},
      {k:'CPM',           cur:all.cpm,  prev:P&&P.cpm,  s:S&&S.cpm,  hib:false,f:money2},
      {k:'Scroll-stop rate', cur:all.ssr, prev:P&&P.ssr, s:S&&S.ssr, hib:true, f:pct2},
      {k:'Clicks',        cur:all.ck,   prev:P&&P.ck,   s:S&&S.ck,   hib:true, f:num},
      {k:'Click through rate', cur:all.ctr, prev:P&&P.ctr, s:S&&S.ctr, hib:true, f:pct2},
      {k:'Purchases',     cur:all.pu,   prev:P&&P.pu,   s:S&&S.pu,   hib:true, f:num},
      {k:'Conversion rate',cur:all.cvr, prev:P&&P.cvr,  s:S&&S.cvr,  hib:true, f:pct2},
      {k:'Revenue',       cur:all.rv,   prev:P&&P.rv,   s:S&&S.rv,   hib:true, f:money},
      {k:'ROAS',          cur:all.roas, prev:P&&P.roas, s:S&&S.roas, hib:true, f:mult}
    ];
    document.getElementById('kpis').innerHTML=tiles.map(function(t){
      var pct=null;
      if(t.prev!==null&&t.prev!==undefined&&t.prev!==0&&t.cur!==null) pct=100*(t.cur-t.prev)/t.prev;
      var dir = pct===null?'flat':(pct>0.05?'pos':(pct<-0.05?'neg':'flat'));
      // Colour by GOODNESS, not direction. hib:null = neutral measure (spend),
      // where up/down is neither good nor bad, so it stays muted.
      var cls='flat';
      if(pct!==null&&t.hib!==null) cls=((pct>0)===t.hib)?'pos':'neg';
      var hue = cls==='pos'?'var(--good)':(cls==='neg'?'var(--bad)':'var(--accent)');
      var arrow = dir==='pos'?'\u2197':(dir==='neg'?'\u2198':'\u2192');
      var fmt=function(x){
        return (x===null||x===undefined||!isFinite(x))?'\u2014':t.f(x);
      };
      return '<div class="kpi"><div class="lbl">'+esc(t.k)+'</div>'+
        '<div class="val">'+fmt(t.cur)+'</div><div class="foot"><div>'+
        '<div class="cmp">vs prior period<br><span class="cmpv">'+fmt(t.prev)+'</span></div>'+
        '<div class="delta '+cls+'">'+arrow+' '+(pct===null?'n/a':(pct>0?'+':'')+pct.toFixed(1)+'%')+'</div>'+
        '</div>'+(t.s?sparkline(t.s,hue):'')+'</div></div>';
    }).join('');
  }

  // One compact diverging bar chart for an attribute, indexing each value's
  // metric against the current filter baseline (100 = the filter average). Same
  // three metrics the scale/kill zones trend on — scroll-stop, CTR, ROAS — so
  // the whole dashboard reads in one visual language. Single series each, so no
  // legend; a value above 100 beat the baseline (green), below it lagged (red).
  function attrBarChart(ents, mk){
    var bv=mk.bv;
    if(!bv) return '';
    var rows=ents.map(function(e){
      var val=mk.get(e);
      return (val===null||val===undefined)?null:{v:e.value, idx:100*val/bv, sp:e.sp};
    }).filter(function(r){return r&&isFinite(r.idx);});
    if(rows.length<2) return '';
    rows.sort(function(a,b){return b.idx-a.idx;});
    rows=rows.slice(0,12);
    var maxDev=Math.max(12,Math.max.apply(null,rows.map(function(r){return Math.abs(r.idx-100);})));
    var lo=100-maxDev*1.2, hi=100+maxDev*1.2;
    var W=360, RH=22, PT=16, PB=6, PL=120, PR=38;
    var H=PT+PB+rows.length*RH;
    var iw=W-PL-PR;
    var xo=function(x){return PL+(Math.max(lo,Math.min(hi,x))-lo)/(hi-lo)*iw;};
    var zx=xo(100);
    var s='<figure class="tchart attrbar"><figcaption class="tcttl">'+esc(mk.lab)+
      '<span class="tcnote">100 = filter avg</span></figcaption>'+
      '<svg viewBox="0 0 '+W+' '+H+'" class="chart" role="img" aria-label="'+esc(mk.lab)+' by value">';
    s+='<line x1="'+zx.toFixed(1)+'" x2="'+zx.toFixed(1)+'" y1="'+(PT-4)+'" y2="'+(H-PB)+
       '" stroke="var(--fg)" stroke-dasharray="3 3" opacity=".45"/>';
    rows.forEach(function(r,i){
      var cy=PT+i*RH+RH/2;
      var col=r.idx>=105?'var(--good)':(r.idx<=95?'var(--bad)':'var(--dim)');
      var x1=Math.min(zx,xo(r.idx)), x2=Math.max(zx,xo(r.idx)), good=r.idx>=100;
      s+='<text x="'+(PL-8)+'" y="'+(cy+3).toFixed(1)+'" text-anchor="end" '+
         'style="font-size:10px;fill:var(--fg)">'+esc(String(r.v).slice(0,18))+'</text>'+
         '<rect x="'+x1.toFixed(1)+'" y="'+(cy-6).toFixed(1)+'" width="'+Math.max(x2-x1,1).toFixed(1)+
         '" height="12" rx="2.5" fill="'+col+'" fill-opacity=".82"><title>'+esc(String(r.v))+': '+
         r.idx.toFixed(0)+' index · '+money(r.sp)+'</title></rect>'+
         '<text x="'+(xo(r.idx)+(good?5:-5)).toFixed(1)+'" y="'+(cy+3).toFixed(1)+
         '" text-anchor="'+(good?'start':'end')+'" style="font-size:9.5px;fill:var(--dim)">'+
         r.idx.toFixed(0)+'</text>';
    });
    return s+'</svg></figure>';
  }

  function attrCharts(ents, base){
    var c=attrBarChart(ents,{lab:'Scroll-stop index',bv:base.ssr,get:function(e){return e.ssr;}})+
          attrBarChart(ents,{lab:'CTR index',bv:base.ctr,get:function(e){return e.ctr;}})+
          attrBarChart(ents,{lab:'ROAS index',bv:base.roas,get:function(e){return e.roas;}});
    return c?('<div class="ztrends attrtrends">'+c+'</div>'):'';
  }

  // Populated by tableHTML as it emits each section, consumed by updateAttrNav
  // to build the left-nav sub-links for whichever tables actually rendered.
  var NAVSECS=[];

  function tableHTML(spec, pool, base, tid, group){
    var key=spec[0], label=spec[1], kind=spec[2], vision=spec[3];
    var buckets={};
    pool.forEach(function(a){
      var v = vision ? (a.A?a.A[key]:undefined) : a[key];
      if(v===undefined||v===null||v==='') return;
      if(kind==='list'){ if(!Array.isArray(v)) return;
        v.forEach(function(x){ if(x===''||x==null) return; (buckets[x]=buckets[x]||[]).push(a); }); }
      else { (buckets[v]=buckets[v]||[]).push(a); }
    });
    var ents=[];
    for(var v in buckets){
      var st=agg(buckets[v]);
      if(st.im<MINIMP) continue;
      st.value=v; st.ads_list=buckets[v];
      ents.push(st);
    }
    if(ents.length<2) return '';
    ents.sort(function(x,y){return y.im-x.im;});
    var maxi=ents[0].im||1;
    var rows=ents.map(function(e,i){
      var rid=tid+'-'+i;
      var ctrIdx=base.ctr?100*e.ctr/base.ctr:0, roasIdx=base.roas?100*e.roas/base.roas:0;
      var ssrIdx=(base.ssr&&e.ssr!==null)?100*e.ssr/base.ssr:0;
      DRILL[rid]=e.ads_list;
      return '<tr class="sum" data-target="'+rid+'">'+
        '<td><span class="caret">&#9654;</span>'+esc(e.value)+
        '<div class="bar"><i style="width:'+(100*e.im/maxi).toFixed(0)+'%"></i></div></td>'+
        '<td class="n">'+e.ads+'</td><td>'+num(e.im)+'</td><td>'+money(e.sp)+'</td>'+
        '<td>'+ssrCell(e)+'</td><td>'+idxCell(ssrIdx)+'</td>'+
        '<td>'+e.ctr.toFixed(2)+'%</td><td>'+idxCell(ctrIdx)+'</td>'+
        '<td>$'+e.cpm.toFixed(2)+'</td><td>'+e.roas.toFixed(2)+'</td><td>'+idxCell(roasIdx)+'</td></tr>'+
        '<tr class="detail" id="'+rid+'" hidden><td colspan="11"><div class="drill"></div></td></tr>';
    }).join('');
    var secId='sec-'+tid;
    NAVSECS.push({id:secId, label:label, group:group||''});
    return '<section class="attrsec" id="'+secId+'">'+
      '<h2>'+esc(label)+'<span class="n">click a row to see its ads</span></h2>'+
      attrCharts(ents, base)+
      '<div class="tblwrap"><table><thead><tr><th>'+esc(label)+'</th><th>ads</th>'+
      '<th>impressions</th><th>spend</th>'+
      '<th title="3-second video views / impressions of video ads only">scroll-stop</th>'+
      '<th>SSR idx</th><th>CTR</th><th>CTR idx</th><th>CPM</th>'+
      '<th>ROAS</th><th>ROAS idx</th></tr></thead><tbody>'+rows+'</tbody>'+
      '<tfoot><tr><td class="n">baseline (current filter)</td><td class="n">'+base.ads+'</td>'+
      '<td class="n">'+num(base.im)+'</td><td class="n">'+money(base.sp)+'</td>'+
      '<td class="n">'+(base.ssr===null?'&mdash;':base.ssr.toFixed(2)+'%')+'</td><td class="n">100</td>'+
      '<td class="n">'+base.ctr.toFixed(2)+'%</td><td class="n">100</td>'+
      '<td class="n">$'+base.cpm.toFixed(2)+'</td><td class="n">'+base.roas.toFixed(2)+'</td>'+
      '<td class="n">100</td></tr></tfoot></table></div></section>';
  }

  // ============================================== early read vs final scale ===
  // Does an ad's EARLY efficiency (its signal over the first N delivering days)
  // predict how much it ultimately SCALES? Answered with a Spearman rank
  // correlation shown three ways — a scatter, a sensitivity sweep over the
  // opening window (1→30d), and quintiles of the early signal. All computed from
  // the same daily series the scale/kill timeline uses; the daily row layout is
  // [dayIdx, spend, purchases, revenue, impressions, video_3s, video_plays, clicks].
  var ER_WINDOW=7, ER_SIGNAL='cpc', ER_OUTCOME='final', ER_MINCLK=10,
      ER_VIEW='scatter', ER_CAMP='', ER_OPTS=false;
  var ER_SIGNALS={
    cpc:{lab:'CPC', low:true,  log:true,  fmt:function(v){return money2(v);},
         get:function(e){return e.ck?e.sp/e.ck:null;}},
    cpm:{lab:'CPM', low:true,  log:true,  fmt:function(v){return money2(v);},
         get:function(e){return e.im?1000*e.sp/e.im:null;}},
    ctr:{lab:'CTR', low:false, log:false, fmt:function(v){return v.toFixed(2)+'%';},
         get:function(e){return e.im?100*e.ck/e.im:null;}},
    cvr:{lab:'CVR', low:false, log:false, fmt:function(v){return v.toFixed(2)+'%';},
         get:function(e){return e.ck?100*e.pu/e.ck:null;}},
    roas:{lab:'ROAS',low:false, log:true,  fmt:function(v){return v.toFixed(2)+'x';},
         get:function(e){return e.sp?e.rv/e.sp:null;}}
  };
  var ER_OUTLAB={final:'final spend', post:'post-window spend'};

  // Per-ad early-window sums + final/post-window spend, for the ads that clear
  // the minimum-early-clicks bar. "Early" = each ad's first N days from its first
  // delivering day within the loaded data (creation may predate the data, same
  // caveat as the scale/kill launch date).
  function erRows(pool, N){
    var camp=ER_CAMP.trim().toLowerCase(), out=[];
    for(var i=0;i<pool.length;i++){
      var a=pool[i], d=a.D; if(!d||!d.length) continue;
      if(camp && (a.cp||'').toLowerCase().indexOf(camp)<0) continue;
      var first=d[0][0], cut=first+N-1;
      var sp=0,ck=0,im=0,pu=0,rv=0,fSp=0;
      for(var j=0;j<d.length;j++){
        fSp+=d[j][1];
        if(d[j][0]<=cut){ sp+=d[j][1]; pu+=d[j][2]; rv+=d[j][3]; im+=d[j][4]||0; ck+=d[j][7]||0; }
      }
      if(ck<ER_MINCLK) continue;
      var sig=ER_SIGNALS[ER_SIGNAL].get({sp:sp,ck:ck,im:im,pu:pu,rv:rv});
      if(sig===null||!isFinite(sig)) continue;
      out.push({a:a, x:sig, yF:fSp, yP:Math.max(fSp-sp,0), eSp:sp, eCk:ck});
    }
    return out;
  }
  function erY(r){ return ER_OUTCOME==='post'? r.yP : r.yF; }

  // Average-rank (ties averaged) → Pearson on the ranks = Spearman ρ.
  function erRank(vals){
    var idx=vals.map(function(v,i){return [v,i];}).sort(function(a,b){return a[0]-b[0];});
    var r=new Array(vals.length), i=0;
    while(i<idx.length){
      var j=i; while(j+1<idx.length && idx[j+1][0]===idx[i][0]) j++;
      var avg=(i+j)/2+1;
      for(var k=i;k<=j;k++) r[idx[k][1]]=avg;
      i=j+1;
    }
    return r;
  }
  function erSpearman(xs, ys){
    var n=xs.length; if(n<4) return null;
    var rx=erRank(xs), ry=erRank(ys), mx=0,my=0, i;
    for(i=0;i<n;i++){ mx+=rx[i]; my+=ry[i]; } mx/=n; my/=n;
    var num=0,dx=0,dy=0;
    for(i=0;i<n;i++){ var a=rx[i]-mx,b=ry[i]-my; num+=a*b; dx+=a*a; dy+=b*b; }
    return (dx>0&&dy>0)? num/Math.sqrt(dx*dy) : null;
  }
  function erRho(rows, which){
    var xs=[],ys=[];
    rows.forEach(function(r){
      var y=(which==='post')?r.yP:(which==='final')?r.yF:erY(r);
      if(y>0){ xs.push(r.x); ys.push(y); }
    });
    return {rho:erSpearman(xs,ys), n:xs.length};
  }
  function erInterp(rho){
    var s=Math.abs(rho), sig=ER_SIGNALS[ER_SIGNAL].lab.toLowerCase(), out=ER_OUTLAB[ER_OUTCOME];
    if(s<0.1) return 'no reliable link — early '+sig+' did not rank-order '+out+'.';
    var strength=s<0.3?'a weak':(s<0.5?'a moderate':'a strong');
    return strength+' '+(rho<0?'negative':'positive')+' link — '+(rho<0?'lower':'higher')+
      ' early '+sig+' ads reached higher '+out+'.';
  }

  function renderER(){
    var pool=ADS.filter(passes).filter(function(a){return a.D&&a.D.length;});
    var def=ER_SIGNALS[ER_SIGNAL], sig=def.lab.toLowerCase();
    // subtitle + options panel
    var sub=(ER_VIEW==='sensitivity')
      ? 'How the early <b>'+sig+'</b> ↔ spend rank correlation shifts as the opening window '+
        'widens from 1 to 30 days, with the shared early dollars kept in (final spend) vs '+
        'stripped out (post-window).'
      : (ER_VIEW==='quintiles')
      ? 'Ads split into five equal groups by early <b>'+sig+'</b> (Q1 lowest), and the median '+
        '<b>'+ER_OUTLAB[ER_OUTCOME]+'</b> each group reached.'
      : 'Each dot is an ad: its early <b>'+sig+'</b> over the first '+ER_WINDOW+' day'+
        (ER_WINDOW===1?'':'s')+' vs how much it ultimately spent.';
    document.getElementById('erSub').innerHTML=sub;
    renderEROpts();
    var rows=erRows(pool, ER_WINDOW);
    var rr=erRho(rows);
    renderERHead(rr);
    if(ER_VIEW==='sensitivity') renderERSensitivity(pool);
    else if(ER_VIEW==='quintiles') renderERQuintiles(rows);
    else renderERScatter(rows, rr);
  }

  function renderEROpts(){
    var host=document.getElementById('erOpts');
    document.getElementById('erAdjust').textContent=ER_OPTS?'Hide options':'Adjust';
    if(!ER_OPTS){ host.style.display='none'; return; }
    host.style.display='';
    if(host.dataset.built) { // just refresh 'on' states
      host.querySelectorAll('[data-sig]').forEach(function(b){b.classList.toggle('on',b.dataset.sig===ER_SIGNAL);});
      host.querySelectorAll('[data-out]').forEach(function(b){b.classList.toggle('on',b.dataset.out===ER_OUTCOME);});
      host.querySelectorAll('[data-mc]').forEach(function(b){b.classList.toggle('on',+b.dataset.mc===ER_MINCLK);});
      return;
    }
    var sigBtns=['cpc','cpm','ctr','cvr','roas'].map(function(k){
      return '<button class="seg'+(k===ER_SIGNAL?' on':'')+'" data-sig="'+k+'" type="button">'+
        ER_SIGNALS[k].lab+'</button>';}).join('');
    var outBtns=[['final','Final spend'],['post','Post-window']].map(function(o){
      return '<button class="seg'+(o[0]===ER_OUTCOME?' on':'')+'" data-out="'+o[0]+'" type="button">'+o[1]+'</button>';}).join('');
    var mcBtns=[5,10,25,50].map(function(m){
      return '<button class="seg'+(m===ER_MINCLK?' on':'')+'" data-mc="'+m+'" type="button">'+m+'</button>';}).join('');
    host.innerHTML=
      '<div class="erorow"><span class="flabel">Early signal</span><span class="segs">'+sigBtns+'</span>'+
      '<span class="flabel" style="margin-left:14px">Outcome</span><span class="segs">'+outBtns+'</span></div>'+
      '<div class="erorow"><span class="flabel">Min early clicks</span><span class="segs">'+mcBtns+'</span>'+
      '<input class="erin" id="erCamp" type="text" placeholder="Scope to campaign (contains)…" value="'+esc(ER_CAMP)+'"></div>';
    host.querySelectorAll('[data-sig]').forEach(function(b){b.addEventListener('click',function(){
      ER_SIGNAL=b.dataset.sig; renderER();});});
    host.querySelectorAll('[data-out]').forEach(function(b){b.addEventListener('click',function(){
      ER_OUTCOME=b.dataset.out; renderER();});});
    host.querySelectorAll('[data-mc]').forEach(function(b){b.addEventListener('click',function(){
      ER_MINCLK=+b.dataset.mc; renderER();});});
    var ci=document.getElementById('erCamp');
    ci.addEventListener('input',function(){ ER_CAMP=ci.value; renderER(); });
    host.dataset.built='1';
  }

  function renderERHead(rr){
    var host=document.getElementById('erHead');
    if(rr.rho===null){
      host.innerHTML='<div class="ercard"><div class="ertext"><div class="erinterp">'+
        'Not enough ads clear the minimum early clicks for a correlation.</div>'+
        '<div class="ersub2">Lower the threshold, widen the window, or broaden the filter — '+
        'only '+rr.n+' qualifying ad'+(rr.n===1?'':'s')+' at this setting.</div></div></div>';
      return;
    }
    var rho=rr.rho;
    host.innerHTML='<div class="ercard"><div class="erstat">'+
      '<div class="k">SPEARMAN ρ (RANK)</div>'+
      '<div class="v">'+(rho<0?'−':'')+Math.abs(rho).toFixed(2)+'</div></div>'+
      '<div class="ertext"><div class="erinterp">'+erInterp(rho)+'</div>'+
      '<div class="ersub2">'+rr.n+' ads · Spearman rank correlation · opening window '+
      ER_WINDOW+' day'+(ER_WINDOW===1?'':'s')+' · min '+ER_MINCLK+' early clicks</div></div></div>';
  }

  // ---- scatter: early signal (x) vs outcome spend (y, log) -------------------
  function renderERScatter(rows, rr){
    var host=document.getElementById('erChart');
    var def=ER_SIGNALS[ER_SIGNAL];
    var pts=rows.filter(function(r){return erY(r)>0 && r.x>0;});
    if(pts.length<4){ host.innerHTML='<div class="empty">Fewer than 4 qualifying ads — nothing to correlate here.</div>'; return; }
    var W=980,H=440,PL=74,PR=24,PT=16,PB=52, iw=W-PL-PR, ih=H-PT-PB;
    var xlog=def.log;
    var tx=function(v){ return xlog?Math.log10(Math.max(v,1e-9)):v; };
    var xs=pts.map(function(p){return tx(p.x);});
    var xmin=Math.min.apply(null,xs), xmax=Math.max.apply(null,xs);
    if(xmax<=xmin){ xmax=xmin+1; } var xpad=(xmax-xmin)*0.06; xmin-=xpad; xmax+=xpad;
    var ys=pts.map(function(p){return erY(p);});
    var yTop=Math.pow(10,Math.ceil(Math.log10(Math.max.apply(null,ys)))), yBot=10;
    var ty=function(v){return Math.log10(Math.max(v,1));};
    var X=function(v){return PL+(tx(v)-xmin)/(xmax-xmin)*iw;};
    var Y=function(v){return PT+ih-(ty(v)-ty(yBot))/(ty(yTop)-ty(yBot))*ih;};
    var ckMax=Math.max.apply(null,pts.map(function(p){return p.eCk;}))||1;

    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Early '+
      esc(def.lab)+' vs '+esc(ER_OUTLAB[ER_OUTCOME])+'">';
    // y gridlines (powers of 10)
    for(var yy=yBot; yy<=yTop+1; yy*=10){
      var gy=Y(yy);
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+gy.toFixed(1)+'" y2="'+gy.toFixed(1)+'"/>'+
         '<text x="'+(PL-9)+'" y="'+(gy+4).toFixed(1)+'" text-anchor="end">'+
         (yy>=1e6?'$'+(yy/1e6)+'M':yy>=1e3?'$'+(yy/1e3)+'k':'$'+yy)+'</text>';
    }
    // x ticks
    var xticks=[];
    if(xlog){ for(var e=Math.floor(xmin);e<=Math.ceil(xmax);e++) xticks.push(Math.pow(10,e)); }
    else { for(var t=0;t<=4;t++) xticks.push(xmin+(xmax-xmin)*t/4); }
    xticks.forEach(function(v){ var X0=xlog?X(v):PL+(v-xmin)/(xmax-xmin)*iw;
      if(X0<PL-1||X0>W-PR+1) return;
      s+='<line class="gridln" x1="'+X0.toFixed(1)+'" x2="'+X0.toFixed(1)+'" y1="'+PT+'" y2="'+(PT+ih)+'"/>'+
         '<text x="'+X0.toFixed(1)+'" y="'+(PT+ih+20)+'" text-anchor="middle">'+def.fmt(v)+'</text>';
    });
    s+='<text x="'+(PL+iw/2)+'" y="'+(H-10)+'" text-anchor="middle">EARLY '+esc(def.lab.toUpperCase())+
       (xlog?' (LOG)':'')+'</text>'+
       '<text x="16" y="'+(PT+ih/2)+'" text-anchor="middle" transform="rotate(-90 16 '+(PT+ih/2)+')">'+
       esc(ER_OUTLAB[ER_OUTCOME].toUpperCase())+' (LOG)</text>';
    // regression line (OLS in transformed space)
    var n=pts.length, mtx=0,mty=0;
    pts.forEach(function(p){ mtx+=tx(p.x); mty+=ty(erY(p)); }); mtx/=n; mty/=n;
    var cov=0,vx=0;
    pts.forEach(function(p){ var ax=tx(p.x)-mtx, ay=ty(erY(p))-mty; cov+=ax*ay; vx+=ax*ax; });
    if(vx>0){ var b=cov/vx, a=mty-b*mtx;
      var lx1=xmin+xpad*0.4, lx2=xmax-xpad*0.4;
      var pv=function(txv){return Math.pow(10, a+b*txv);};
      var xv1=xlog?Math.pow(10,lx1):lx1, xv2=xlog?Math.pow(10,lx2):lx2;
      var Xa=PL+(lx1-xmin)/(xmax-xmin)*iw, Xb=PL+(lx2-xmin)/(xmax-xmin)*iw;
      s+='<line x1="'+Xa.toFixed(1)+'" y1="'+Y(pv(lx1)).toFixed(1)+'" x2="'+Xb.toFixed(1)+
         '" y2="'+Y(pv(lx2)).toFixed(1)+'" stroke="var(--fg)" stroke-width="2" opacity=".7"/>';
    }
    // dots, sized by early clicks
    pts.forEach(function(p){
      var X0=xlog?X(p.x):PL+(p.x-xmin)/(xmax-xmin)*iw;
      s+='<circle class="rdot" cx="'+X0.toFixed(1)+'" cy="'+Y(erY(p)).toFixed(1)+
         '" r="'+(3+7*Math.sqrt(p.eCk/ckMax)).toFixed(1)+'" fill="var(--sA)"><title>'+
         esc((p.a.nm||p.a.id||'').slice(0,70))+'\nearly '+esc(def.lab)+' '+def.fmt(p.x)+
         ' · '+ER_OUTLAB[ER_OUTCOME]+' '+money(erY(p))+' · '+num(p.eCk)+' early clicks</title></circle>';
    });
    s+='</svg>';
    host.innerHTML=s;
    document.getElementById('erNote').innerHTML=
      '<b>Reading this:</b> each dot is an ad; the line is the ordinary-least-squares fit in '+
      'log space, and the headline number is the <b>Spearman rank</b> correlation (robust to the '+
      'log scale and to outliers). Dots are sized by early clicks — bigger dots carry more '+
      'signal. This is correlation across ads that already survived budget decisions, not proof '+
      'that hitting a given early '+esc(def.lab)+' <em>causes</em> more scale. Early window is each '+
      'ad’s first '+ER_WINDOW+' delivering day'+(ER_WINDOW===1?'':'s')+' in the loaded data.';
  }

  // ---- sensitivity: ρ as the opening window widens 1→30d ---------------------
  function renderERSensitivity(pool){
    var host=document.getElementById('erChart');
    var days=[], sF=[], sP=[];
    for(var w=1; w<=30; w++){
      var rows=erRows(pool, w);
      var rf=erRho(rows,'final'), rp=erRho(rows,'post');
      days.push(w); sF.push(rf.rho); sP.push(rp.rho);
    }
    var all=sF.concat(sP).filter(function(v){return v!==null&&isFinite(v);});
    if(all.length<2){ host.innerHTML='<div class="empty">Not enough qualifying ads across the window to sweep.</div>'; return; }
    var lo=Math.min.apply(null,all), hi=Math.max.apply(null,all);
    lo=Math.min(lo,-0.05); hi=Math.max(hi,0.05); var pad=(hi-lo)*0.12; lo-=pad; hi+=pad;
    var W=980,H=420,PL=64,PR=96,PT=18,PB=46, iw=W-PL-PR, ih=H-PT-PB;
    var X=function(w){return PL+(w-1)/29*iw;};
    var Y=function(v){return PT+ih-(v-lo)/(hi-lo)*ih;};
    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Spearman correlation vs opening window">';
    for(var g=0;g<=4;g++){ var gv=lo+(hi-lo)*g/4, gy=Y(gv);
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+gy.toFixed(1)+'" y2="'+gy.toFixed(1)+'"/>'+
         '<text x="'+(PL-9)+'" y="'+(gy+4).toFixed(1)+'" text-anchor="end">'+gv.toFixed(2)+'</text>'; }
    if(lo<0&&hi>0){ var z=Y(0); s+='<line x1="'+PL+'" x2="'+(W-PR)+'" y1="'+z.toFixed(1)+'" y2="'+z.toFixed(1)+
      '" stroke="var(--fg)" stroke-width="1" opacity=".35"/>'; }
    [1,3,5,7,10,14,21,30].forEach(function(w){ var X0=X(w);
      s+='<text x="'+X0.toFixed(1)+'" y="'+(PT+ih+20)+'" text-anchor="middle">'+w+'</text>'; });
    s+='<text x="'+(PL+iw/2)+'" y="'+(H-6)+'" text-anchor="middle">OPENING WINDOW (DAYS)</text>'+
       '<text x="16" y="'+(PT+ih/2)+'" text-anchor="middle" transform="rotate(-90 16 '+(PT+ih/2)+')">SPEARMAN ρ</text>';
    // current-window marker
    var mx=X(ER_WINDOW);
    s+='<line x1="'+mx.toFixed(1)+'" x2="'+mx.toFixed(1)+'" y1="'+PT+'" y2="'+(PT+ih)+
       '" stroke="var(--dim)" stroke-width="1" stroke-dasharray="4 4"/>'+
       '<text x="'+mx.toFixed(1)+'" y="'+(PT-4)+'" text-anchor="middle" style="fill:var(--dim)">'+ER_WINDOW+'d</text>';
    function line(vals, col){
      var d='', started=false, lastX=null,lastY=null,lastV=null;
      vals.forEach(function(v,i){ if(v===null||!isFinite(v)){started=false;return;}
        var x=X(days[i]), y=Y(v); d+=(started?' L':'M')+x.toFixed(1)+' '+y.toFixed(1);
        started=true; lastX=x; lastY=y; lastV=v; });
      return {path:d, lx:lastX, ly:lastY, lv:lastV, col:col};
    }
    var lf=line(sF,'var(--sA)'), lp=line(sP,'var(--sB)');
    [lp,lf].forEach(function(L){ if(L.path) s+='<path d="'+L.path+'" fill="none" stroke="'+L.col+'" stroke-width="2" stroke-linejoin="round"/>'; });
    // direct end labels
    [{L:lf,t:'Final'},{L:lp,t:'Post-window'}].forEach(function(o){ if(o.L.lx!==null)
      s+='<circle cx="'+o.L.lx.toFixed(1)+'" cy="'+o.L.ly.toFixed(1)+'" r="3" fill="'+o.L.col+
         '" stroke="var(--panel)" stroke-width="2"/><text x="'+(o.L.lx+7).toFixed(1)+'" y="'+
         (o.L.ly+4).toFixed(1)+'" style="fill:'+o.L.col+';font-weight:600">'+o.t+'</text>'; });
    s+='</svg>';
    host.innerHTML=
      '<div class="erlegend"><span><i style="background:var(--sA)"></i>vs Final spend</span>'+
      '<span><i style="background:var(--sB)"></i>vs Post-window spend</span></div>'+s;
    document.getElementById('erNote').innerHTML=
      '<b>Reading this:</b> the <b style="color:var(--sA)">Final spend</b> line keeps the early '+
      'dollars in the outcome, so part of any correlation is mechanical (early spend is inside '+
      'final spend). The <b style="color:var(--sB)">Post-window</b> line strips those shared '+
      'dollars out — it is the honest test of whether the early read predicts <em>future</em> '+
      'scale. Where post-window drifts toward zero as the window widens, the early signal is '+
      'mostly re-describing spend already booked, not forecasting more.';
  }

  // ---- quintiles: median outcome spend by early-signal group -----------------
  function renderERQuintiles(rows){
    var host=document.getElementById('erChart');
    var def=ER_SIGNALS[ER_SIGNAL];
    var pts=rows.filter(function(r){return erY(r)>0;}).slice().sort(function(a,b){return a.x-b.x;});
    if(pts.length<10){ host.innerHTML='<div class="empty">Need at least 10 qualifying ads to form quintiles.</div>'; return; }
    var med=function(arr){ var a=arr.slice().sort(function(x,y){return x-y;}), m=a.length>>1;
      return a.length%2?a[m]:(a[m-1]+a[m])/2; };
    var bins=[];
    for(var q=0;q<5;q++){
      var a=Math.floor(q*pts.length/5), b=Math.floor((q+1)*pts.length/5);
      var slice=pts.slice(a,b);
      bins.push({q:q+1, n:slice.length,
        xlo:slice[0].x, xhi:slice[slice.length-1].x,
        m:med(slice.map(erY))});
    }
    var W=980,H=420,PL=74,PR=24,PT=20,PB=64, iw=W-PL-PR, ih=H-PT-PB;
    var mx=Math.max.apply(null,bins.map(function(x){return x.m;}))*1.15||1;
    var bw=iw/5;
    var s='<svg class="chart" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Median '+
      esc(ER_OUTLAB[ER_OUTCOME])+' by early '+esc(def.lab)+' quintile">';
    for(var g=0;g<=4;g++){ var gv=mx*g/4, gy=PT+ih-gv/mx*ih;
      s+='<line class="gridln" x1="'+PL+'" x2="'+(W-PR)+'" y1="'+gy.toFixed(1)+'" y2="'+gy.toFixed(1)+'"/>'+
         '<text x="'+(PL-9)+'" y="'+(gy+4).toFixed(1)+'" text-anchor="end">'+
         (gv>=1e6?'$'+(gv/1e6).toFixed(1)+'M':gv>=1e3?'$'+Math.round(gv/1e3)+'k':'$'+Math.round(gv))+'</text>'; }
    bins.forEach(function(bn,i){
      var h=bn.m/mx*ih, x=PL+i*bw+bw*0.16, w=bw*0.68;
      s+='<rect x="'+x.toFixed(1)+'" y="'+(PT+ih-h).toFixed(1)+'" width="'+w.toFixed(1)+
         '" height="'+Math.max(h,0).toFixed(1)+'" rx="4" fill="var(--sA)" fill-opacity="'+
         (0.5+0.1*i).toFixed(2)+'"><title>Q'+bn.q+' · early '+esc(def.lab)+' '+def.fmt(bn.xlo)+
         ' – '+def.fmt(bn.xhi)+'\nmedian '+ER_OUTLAB[ER_OUTCOME]+' '+money(bn.m)+' · '+bn.n+' ads</title></rect>'+
         '<text x="'+(x+w/2).toFixed(1)+'" y="'+(PT+ih-h-7).toFixed(1)+'" text-anchor="middle" '+
         'style="fill:var(--fg);font-weight:600;font-size:12px">'+money(bn.m)+'</text>'+
         '<text x="'+(x+w/2).toFixed(1)+'" y="'+(PT+ih+20)+'" text-anchor="middle" '+
         'style="fill:var(--fg);font-weight:600">Q'+bn.q+'</text>'+
         '<text x="'+(x+w/2).toFixed(1)+'" y="'+(PT+ih+37)+'" text-anchor="middle">'+
         def.fmt(bn.xlo)+'–'+def.fmt(bn.xhi)+'</text>';
    });
    s+='<text x="'+(PL+iw/2)+'" y="'+(H-6)+'" text-anchor="middle">EARLY '+esc(def.lab.toUpperCase())+
       ' QUINTILE  (Q1 = lowest)</text>'+
       '<text x="16" y="'+(PT+ih/2)+'" text-anchor="middle" transform="rotate(-90 16 '+(PT+ih/2)+')">MEDIAN '+
       esc(ER_OUTLAB[ER_OUTCOME].toUpperCase())+'</text></svg>';
    host.innerHTML=s;
    document.getElementById('erNote').innerHTML=
      '<b>Reading this:</b> ads are split into five equal-sized groups by their early '+esc(def.lab)+
      ' (Q1 the lowest, Q5 the highest), and each bar is that group’s <b>median</b> '+
      esc(ER_OUTLAB[ER_OUTCOME])+' — the median, not the mean, so one runaway ad can’t carry a '+
      'bar. A clean staircase across the quintiles is the same story the correlation tells, but '+
      'without assuming the relationship is a straight line.';
  }

  var DRILL={};

  function render(){
    var pool=ADS.filter(passes);
    var readable=pool.filter(function(a){return a.A;});
    DRILL={};
    NAVSECS=[];

    // headline KPI tiles
    var all=agg(pool), rd=agg(readable);
    renderKpis(pool, all);

    renderFunnel(pool);
    renderRank(pool);
    renderSK(pool);
    renderER();
    renderAttrNote(all);

    var pct=all.sp?100*rd.sp/all.sp:0;
    document.getElementById('cov').innerHTML='<b>Coverage:</b> vision-attribute tables cover <b>'+
      rd.ads+'</b> of '+all.ads+' ads in the current filter &mdash; <b>'+money(rd.sp)+'</b> of '+
      money(all.sp)+' spend ('+pct.toFixed(0)+'%). The rest is mostly catalog/dynamic-product ads, '+
      'whose creative is assembled per product at serve time, so there is no fixed image to analyze. '+
      'Those ads still appear in the audience &amp; metadata tables, which cover 100% of the filtered spend. '+
      'Figures are impression-weighted; buckets under '+num(MINIMP)+' impressions are dropped as too thin to read.';

    if(!pool.length){
      document.getElementById('tables').innerHTML='<div class="empty">No ads match this filter.</div>';
      document.getElementById('fstat').innerHTML='';
      updateAttrNav();
      return;
    }
    var active=FILTERS.filter(function(f){return state[f[0]];})
      .map(function(f){return f[1]+': <b>'+esc(state[f[0]])+'</b>';});
    document.getElementById('fstat').innerHTML= active.length
      ? 'Filtered to <b>'+all.ads+'</b> ads &middot; '+money(all.sp)+' &mdash; '+active.join(' &middot; ')
      : 'Showing all <b>'+all.ads+'</b> ads &middot; '+money(all.sp);

    var G_META='Audience & metadata', G_VIS='Creative attributes', G_ACCT='Account structure';
    var html='<h3 class="grp" id="grp-meta">'+esc(G_META)+' &mdash; all ads (100% of filtered spend)</h3>';
    META_SPECS.forEach(function(s,i){ html+=tableHTML(s, pool, all, 'm'+i, G_META); });
    if(readable.length){
      html+='<h3 class="grp" id="grp-vis">'+esc(G_VIS)+' &mdash; analyzable ads only ('+pct.toFixed(0)+'% of filtered spend)</h3>';
      VISION_SPECS.forEach(function(s,i){ html+=tableHTML(s, readable, rd, 'v'+i, G_VIS); });
    }else{
      html+='<div class="note">No creative-attribute tables for this filter &mdash; none of the '+
        'matching ads have a readable creative analysis (catalog ads have no fixed creative).</div>';
    }
    // Account structure last — see TAIL_SPECS for why these sit apart from the
    // attribute tables rather than among them.
    html+='<h3 class="grp" id="grp-acct">'+esc(G_ACCT)+' &mdash; all ads (100% of filtered spend)</h3>';
    TAIL_SPECS.forEach(function(s,i){ html+=tableHTML(s, pool, all, 't'+i, G_ACCT); });
    document.getElementById('tables').innerHTML=html;
    updateAttrNav();
    wireRows();
  }

  // Rebuild the left-nav sub-links under "Creative attributes" from whatever
  // tables the current filter actually rendered (thin buckets drop out, so the
  // list is filter-dependent). Grouped under their section headings; each link
  // smooth-scrolls to its table.
  function updateAttrNav(){
    var host=document.getElementById('secNavAttrs');
    if(!host) return;
    if(!NAVSECS.length){ host.innerHTML=''; return; }
    var order=[], byGroup={};
    NAVSECS.forEach(function(n){
      if(!byGroup[n.group]){ byGroup[n.group]=[]; order.push(n.group); }
      byGroup[n.group].push(n);
    });
    host.innerHTML=order.map(function(g){
      return '<div class="secnav-grp">'+esc(g)+'</div>'+
        byGroup[g].map(function(n){
          return '<a class="sub" href="#'+n.id+'" data-sec="'+n.id+'"><i></i>'+esc(n.label)+'</a>';
        }).join('');
    }).join('');
  }

  function wireRows(){
    document.querySelectorAll('tr.sum').forEach(function(tr){
      tr.addEventListener('click',function(){
        var det=document.getElementById(tr.dataset.target);
        if(!det) return;
        if(!det.hidden){det.hidden=true;tr.classList.remove('open');return;}
        var host=det.querySelector('.drill');
        if(!host.dataset.filled){
          var list=(DRILL[tr.dataset.target]||[]).slice().sort(function(a,b){return (b.sp||0)-(a.sp||0);});
          var shown=list.slice(0,MAXC);
          host.innerHTML='<div class="drillhead"><span>'+list.length+' ad'+(list.length===1?'':'s')+
            ' in this bucket, highest spend first</span>'+
            (list.length>shown.length?'<span>showing top '+shown.length+' of '+list.length+'</span>':'')+
            '</div><div class="assets">'+shown.map(card).join('')+'</div>';
          host.dataset.filled='1';
        }
        det.hidden=false;tr.classList.add('open');
      });
    });
  }

  document.getElementById('resetFilters').addEventListener('click',function(){
    FILTERS.forEach(function(f){state[f[0]]='';});
    document.querySelectorAll('#filterFields select').forEach(function(s){
      s.value=''; s.closest('.chip').classList.remove('set');});
    render();
  });

  // --- viz controls ---------------------------------------------------------
  document.getElementById('fnA').addEventListener('change',function(){
    FN_A=this.value; renderFunnel(ADS.filter(passes));});
  document.getElementById('fnB').addEventListener('change',function(){
    FN_B=this.value; renderFunnel(ADS.filter(passes));});
  document.getElementById('fnMode').addEventListener('change',function(){
    FN_MODE=this.value; renderFunnel(ADS.filter(passes));});

  document.getElementById('attrSel').addEventListener('change',function(){
    ATTR=this.value;
    this.closest('.chip').classList.toggle('set', ATTR!=='default');
    render();
  });

  document.querySelectorAll('#rankSegs .seg').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('#rankSegs .seg').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on'); RANK_FIELD=b.dataset.r; renderRank(ADS.filter(passes));
    });
  });

  document.querySelectorAll('#skMetric .seg').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('#skMetric .seg').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on');
      SK_METRIC=b.dataset.m;
      // The target is metric-specific — a CPA target is meaningless as a ROAS
      // target, so switching metrics drops back to the auto default.
      SK_TARGET=null;
      document.getElementById('adPanel').innerHTML='';
      renderSK(ADS.filter(passes));
    });
  });
  document.getElementById('skTarget').addEventListener('change',function(){
    var v=parseFloat(this.value);
    // An absolute target and a reduction-off-average are two ways of naming the
    // same number, so typing one clears the other rather than compounding.
    SK_TARGET=(isFinite(v)&&v>0)?v:null;
    SK_ADJ=0;
    renderSK(ADS.filter(passes));
  });
  document.querySelectorAll('#skAdj .seg').forEach(function(b){
    b.addEventListener('click',function(){
      SK_ADJ=+b.dataset.adj;
      SK_TARGET=null;                 // reduction is measured off the account average
      renderSK(ADS.filter(passes));
    });
  });
  document.getElementById('skReset').addEventListener('click',function(){
    SK_TARGET=null; SK_ADJ=0; renderSK(ADS.filter(passes));
  });
  document.getElementById('skLaunch').addEventListener('change',function(){
    SK_LAUNCH=this.value;
    // The open drill-down may belong to an ad this cohort excludes.
    document.getElementById('adPanel').innerHTML='';
    stopPlay();
    renderSK(ADS.filter(passes));
  });

  // --- early read vs final scale controls ----------------------------------
  document.getElementById('erWin').addEventListener('input',function(){
    ER_WINDOW=+this.value;
    document.getElementById('erWinLab').textContent=ER_WINDOW+(ER_WINDOW===1?' day':' days');
    renderER();
  });
  document.querySelectorAll('#erView .seg').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('#erView .seg').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on'); ER_VIEW=b.dataset.v; renderER();
    });
  });
  document.getElementById('erAdjust').addEventListener('click',function(){
    ER_OPTS=!ER_OPTS; renderER();
  });

  // --- permanent left section nav (scroll-spy only) ------------------------
  (function(){
    var nav=document.getElementById('secNav');
    if(!nav) return;
    var links=[].slice.call(nav.querySelectorAll('a[data-sec]'));
    var byId={}; links.forEach(function(a){ byId[a.dataset.sec]=a; });
    // Highlight the section nearest the top of the viewport as it scrolls.
    if('IntersectionObserver' in window){
      var vis={};
      var obs=new IntersectionObserver(function(ents){
        ents.forEach(function(e){ vis[e.target.id]=e.isIntersecting?e.intersectionRatio:0; });
        var best=null, bestR=0;
        Object.keys(vis).forEach(function(id){ if(vis[id]>bestR){bestR=vis[id];best=id;} });
        links.forEach(function(a){ a.classList.toggle('active', a.dataset.sec===best); });
      },{rootMargin:'-10% 0px -70% 0px',threshold:[0,0.25,0.5,1]});
      links.forEach(function(a){ var el=document.getElementById(a.dataset.sec); if(el) obs.observe(el); });
    }
  })();

  buildFilterBar();
  render();
})();
"""


def build_performance_dashboard(
    conn: sqlite3.Connection,
    *,
    out_dir: Path,
    competitor_ids: list[str] | None = None,
    min_impressions: int = 1000,
) -> dict[str, Any] | None:
    rows = _fetch(conn, competitor_ids)
    if not rows:
        return None

    # Vision attribute specs shipped to the client: (key, label, kind, is_vision)
    vision_specs = (
        [(k, lab, "scalar", True) for k, lab in SCALAR_ATTRS]
        + [(k, lab, "scalar", True) for k, lab in NESTED_ATTRS]
        + [(k, lab, "list", True) for k, lab in LIST_ATTRS]
    )
    meta_specs = [(k, lab, kind, False) for k, lab, kind in META_SPECS]
    tail_specs = [(k, lab, kind, False) for k, lab, kind in TAIL_SPECS]

    ads: list[dict[str, Any]] = []
    for r in rows:
        rec: dict[str, Any] = {
            # The account's native ad id. Needed as a stable key by anything that
            # addresses a single ad — the scale/kill dot click-through and the
            # per-ad funnel options both look an ad up by it.
            "id": r.get("platform_ad_id") or "",
            "nm": r.get("ad_name") or r.get("title") or "",
            "b": r.get("competitor_id") or "",
            "ac": r.get("account_name") or "",
            "cp": r.get("campaign_name") or "",
            "cta": r.get("cta_type") or "",
            "ot": r.get("object_type") or "",
            "cl": r.get("creative_class") or "unknown",
            "stage": r.get("audience_stage") or "",
            "gen": r.get("audience_gender") or "",
            "age": r.get("audience_age") or "",
            "geo": r.get("audience_geo") or "",
            "an": r.get("audience_name") or "",
            "opt": r.get("optimization_goal") or "",
            "at": r.get("asset_type") or "",
            "img": r.get("asset_path") or "",
            "sp": round(r.get("spend") or 0, 2),
            "im": r.get("impressions") or 0,
            "ck": r.get("clicks") or 0,
            "pu": r.get("purchases") or 0,
            "rv": round(r.get("revenue") or 0, 2),
            # Scroll-stop inputs. `vp` (autoplay initiations) is what marks an ad
            # as having served video at all — it is the denominator gate, not a
            # metric in its own right.
            "v3": r.get("video_3s") or 0,
            "vp": r.get("video_plays") or 0,
            # Funnel steps. `tp` (thruplay) and `v3` are video-only; the rest are
            # counted over all impressions. The funnel view keeps the two
            # denominators separate rather than implying one nested sequence.
            "tp": r.get("thruplays") or 0,
            "lc": r.get("link_clicks") or 0,
            "atc": r.get("add_to_cart") or 0,
            "ic": r.get("initiate_checkout") or 0,
            # First seen in market — the ad's creation date, which is what makes
            # "how has this asset performed since launch" answerable.
            "cr": (r.get("created_time") or "")[:10],
        }
        # Per-window conversion components. The dashboard re-weights purchases,
        # revenue and the funnel conversion steps from this; `default` equals the
        # flat fields above. Absent for ads ingested before attribution existed.
        if r.get("attribution_json"):
            try:
                rec["attr"] = json.loads(r["attribution_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        rk = r.get("_rank")
        if rk:
            rec["R"] = rk
        dl = r.get("_daily")
        if dl:
            rec["D"] = dl
        # Weekly series for the sparklines. Component metrics only — derived
        # rates (CTR, ROAS, scroll-stop) are recomputed per bucket in the
        # browser so a filtered sparkline stays exact rather than averaging
        # pre-computed per-ad rates.
        ser = r.get("series")
        if ser:
            rec["s"] = {
                "im": [round(x) for x in ser["im"]],
                "sp": [round(x, 2) for x in ser["sp"]],
                "ck": [round(x) for x in ser["ck"]],
                "pu": [round(x) for x in ser["pu"]],
                "rv": [round(x, 2) for x in ser["rv"]],
                "v3": [round(x) for x in ser["v3"]],
                "vp": [round(x) for x in ser["vp"]],
            }
        # Only readable analyses contribute vision attributes — see
        # MIN_ANALYSIS_CONFIDENCE for why unreadable ones are dropped entirely.
        if r.get("analysis") and _is_readable(r):
            a = r["analysis"]
            attrs: dict[str, Any] = {}
            for k, _lab in SCALAR_ATTRS:
                v = _norm(a.get(k))
                if v:
                    attrs[k] = v
            for k, _lab in NESTED_ATTRS:
                v = _norm(_dig(a, k))
                if v:
                    attrs[k] = v
            for k, _lab in LIST_ATTRS:
                vals = a.get(k)
                if isinstance(vals, list):
                    clean = [_norm(x) for x in vals]
                    clean = [x for x in clean if x]
                    if clean:
                        attrs[k] = clean
            if attrs:
                rec["A"] = attrs
        ads.append(rec)

    window = rows[0].get("_window") if rows else None
    prior_window = rows[0].get("_prior_window") if rows else None
    buckets: list[str] = rows[0].get("_buckets") or [] if rows else []
    rank_window = rows[0].get("_rank_window") if rows else None
    days: list[str] = (rows[0].get("_days") or []) if rows else []
    prior_rows = _fetch_prior(conn, prior_window, competitor_ids)
    def _prow(pr: dict) -> dict:
        rec = {"b": pr.get("competitor_id") or "", "ac": pr.get("account_name") or "",
               "cl": pr.get("creative_class") or "unknown",
               "stage": pr.get("audience_stage") or "", "gen": pr.get("audience_gender") or "",
               "age": pr.get("audience_age") or "", "geo": pr.get("audience_geo") or "",
               "im": pr.get("im") or 0, "sp": round(pr.get("sp") or 0, 2),
               "ck": pr.get("ck") or 0, "pu": pr.get("pu") or 0,
               "rv": round(pr.get("rv") or 0, 2),
               "v3": pr.get("v3") or 0, "vp": pr.get("vp") or 0}
        # Prior-period attribution, so the comparison re-weights on the same
        # window as the current period rather than reading a fixed default while
        # the current side moves — which would make every delta meaningless.
        if pr.get("attribution_json"):
            try:
                rec["attr"] = json.loads(pr["attribution_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return rec
    pads = [_prow(pr) for pr in prior_rows]

    total_spend = sum(a["sp"] for a in ads)
    analyzed = [a for a in ads if a.get("A")]
    brands = sorted({a["b"] for a in ads if a["b"]})

    filt_json = json.dumps(FILTERS)
    def _fmt(d: str) -> str:
        return d

    win_label = (
        f"{_fmt(window[0])} – {_fmt(window[1])}" if window else "all data"
    )
    prior_label = (
        f"{_fmt(prior_window[0])} – {_fmt(prior_window[1])}" if prior_window else None
    )
    sub = (
        f'{", ".join(html.escape(b) for b in brands)} · owned-account performance · '
        f"{html.escape(win_label)}"
        + (f" vs {html.escape(prior_label)}" if prior_label else "")
    )

    body = (
        # Permanent left-hand section nav. Fixed to the viewport so it stays put
        # while the long dashboard scrolls.
        '<nav class="secnav" id="secNav" aria-label="Section navigation">'
        '<div class="secnav-inner" id="secNavInner">'
        '<div class="secnav-hd">On this page</div>'
        '<a href="#kpiSec" data-sec="kpiSec"><i></i>Overview &amp; KPIs</a>'
        '<a href="#funnelSec" data-sec="funnelSec"><i></i>Conversion funnel</a>'
        '<a href="#skSec" data-sec="skSec"><i></i>Scale or kill</a>'
        '<a href="#tablesSec" data-sec="tablesSec"><i></i>Creative attributes</a>'
        '<div id="secNavAttrs" class="secnav-sub"></div>'
        '<a href="#rankSec" data-sec="rankSec"><i></i>Meta rating vs ROAS</a>'
        '<a href="#erSec" data-sec="erSec"><i></i>Early read vs scale</a>'
        '</div></nav>'
        '<div class="topbar"><div>'
        "<h1>Creative performance</h1>"
        f'<div class="sub">{sub}</div></div>'
        '<button class="toggle" id="themeToggle" type="button">Theme</button></div>'
        '<div class="fbar">'
        '<div class="r1">'
        '<span class="flabel">Reporting window</span>'
        f'<span class="win">{html.escape(win_label)}</span>'
        '<span class="segs" id="winSegs">'
        f'<button class="seg on" type="button" title="{html.escape(win_label)}">90D</button>'
        '</span>'
        '<span class="spacer"></span>'
        '<span class="flabel">Attribution</span>'
        '<span class="chip attr"><select id="attrSel" '
        'title="Which attribution window credits conversions">'
        '<option value="default">Account default</option>'
        '<option value="1d_view">1-day view</option>'
        '<option value="1d_click">1-day click</option>'
        '<option value="7d_click">7-day click</option>'
        '<option value="28d_click">28-day click</option>'
        '</select></span>'
        '<span class="flabel">Compare</span>'
        '<span class="segs">'
        '<button class="seg on" type="button" '
        f'title="{html.escape(prior_label or "no prior period ingested")}">Prior period</button>'
        "</span></div>"
        '<div class="r2">'
        '<span class="chip locked">Source:&nbsp;<b>Meta owned accounts</b></span>'
        '<span id="filterFields"></span>'
        '<button class="fbtn" id="resetFilters" type="button">Reset</button>'
        "</div>"
        '<div class="fstat" id="fstat"></div></div>'
        '<section id="kpiSec"><div class="kpis" id="kpis"></div></section>'
        '<div class="note" id="attrNote"></div>'
        '<section class="viz" id="funnelSec">'
        '<h2>Conversion funnel <span class="h2sub">where the drop-off is</span></h2>'
        '<div class="vizbar">'
        '<span class="flabel">Compare</span>'
        '<span class="chip"><select id="fnA"></select></span>'
        '<span class="vs">vs</span>'
        '<span class="chip"><select id="fnB"></select></span>'
        '<span class="spacer"></span>'
        '<span class="chip"><select id="fnMode">'
        '<option value="imp">Stages as % of impressions</option>'
        '<option value="step">Stages as % of previous step</option>'
        '</select></span></div>'
        '<div class="funnels" id="funnels"></div>'
        '<div class="note" id="fnNote"></div></section>'
        '<section class="viz" id="skSec">'
        '<h2>Scale or kill <span class="h2sub">every ad by spend and efficiency'
        "</span></h2>"
        '<div class="vizbar">'
        '<span class="flabel">Metric</span>'
        '<span class="segs" id="skMetric">'
        '<button class="seg on" data-m="cpa" type="button" title="Cost per purchase — lower-funnel efficiency">CPA</button>'
        '<button class="seg" data-m="roas" type="button" title="Return on ad spend">ROAS</button>'
        '<button class="seg" data-m="cpm" type="button" title="Cost per 1,000 impressions — reach efficiency for prospecting">CPM</button>'
        '<button class="seg" data-m="cpv" type="button" title="Cost per 3-second video view — upper-funnel / hook efficiency (video ads only)">3s view</button>'
        "</span>"
        '<span class="flabel tgt">Target</span>'
        '<input class="tin" id="skTarget" type="number" step="0.01" min="0" '
        'title="Absolute target for the active metric. Defaults to the account average.">'
        '<span class="flabel tgt">vs&nbsp;avg</span>'
        '<span class="segs" id="skAdj" '
        'title="Tighten the target below the account average by this much">'
        '<button class="seg on" data-adj="0" type="button">Avg</button>'
        '<button class="seg" data-adj="0.05" type="button">&minus;5%</button>'
        '<button class="seg" data-adj="0.1" type="button">&minus;10%</button>'
        '<button class="seg" data-adj="0.15" type="button">&minus;15%</button>'
        '<button class="seg" data-adj="0.2" type="button">&minus;20%</button>'
        "</span>"
        '<span class="flabel tgt">Launched</span>'
        '<span class="chip"><select id="skLaunch"></select></span>'
        '<span class="spacer"></span>'
        '<button class="fbtn" id="skReset" type="button">Reset target</button>'
        "</div>"
        '<div class="skzones" id="skZones"></div>'
        '<div id="skPlot"></div>'
        '<div class="tl" id="skTl"></div>'
        '<div class="note" id="skNote"></div>'
        '<div id="skTable"></div>'
        '<div id="adPanel"></div></section>'
        '<section id="tablesSec">'
        '<div class="note" id="cov"></div>'
        '<div id="tables"></div></section>'
        '<section class="viz" id="rankSec">'
        '<h2>Meta rating vs ROAS <span class="h2sub">does the platform\'s own '
        "grade predict return?</span></h2>"
        '<div class="vizbar">'
        '<span class="flabel">Rating</span>'
        '<span class="segs" id="rankSegs">'
        '<button class="seg on" data-r="q" type="button">Quality</button>'
        '<button class="seg" data-r="e" type="button">Engagement</button>'
        '<button class="seg" data-r="c" type="button">Conversion</button>'
        "</span></div>"
        '<div id="rankPlot"></div>'
        '<div class="note" id="rankNote"></div></section>'
        '<section class="viz" id="erSec">'
        '<h2>Early read vs final scale <span class="h2sub">does an ad\'s first days '
        "predict how far it scales?</span></h2>"
        '<div class="sub erhd2" id="erSub"></div>'
        '<div class="vizbar">'
        '<span class="flabel">Opening window</span>'
        '<input type="range" id="erWin" class="erslider" min="1" max="30" value="7">'
        '<span class="win" id="erWinLab">7 days</span>'
        '<span class="spacer"></span>'
        '<button class="fbtn" id="erAdjust" type="button">Adjust</button>'
        '<span class="segs" id="erView">'
        '<button class="seg on" data-v="scatter" type="button">Scatter</button>'
        '<button class="seg" data-v="sensitivity" type="button">Sensitivity</button>'
        '<button class="seg" data-v="quintiles" type="button">Quintiles</button>'
        "</span></div>"
        '<div class="eropts" id="erOpts"></div>'
        '<div class="erhead" id="erHead"></div>'
        '<div id="erChart"></div>'
        '<div class="note" id="erNote"></div></section>'
        "<footer>The funnel, rating, scale/kill and early-read views all respond to the same "
        "filter bar as the attribute tables. Two of them deliberately use a different time "
        "window than the 90-day headline: Meta's quality rankings only exist for recent "
        "delivery, so the rating plot is scoped to the window where they are populated, and "
        "the scale/kill chart accumulates day by day so an ad's position reflects spend up "
        "to the selected date. Zone boundaries are a confidence band around a target, not a "
        "fixed threshold — see the note under that chart for the formula and its limits. "
        "CTR/ROAS index: 100 = the baseline for the <em>current filter</em>, so "
        "indices re-base as you narrow. Above 100 is better than that baseline, below is "
        "worse. Audience facets are derived from each ad set's targeting spec (custom "
        "audiences, age, gender, geo); funnel stage is inferred, since Meta exposes no "
        "explicit prospecting/retargeting flag. Scroll-stop rate is 3-second video views "
        "divided by the impressions of <em>video ads only</em> — static and catalog-image "
        "ads cannot register a 3-second view, so including them would drag the rate down "
        "for reasons unrelated to the creative; the bracketed number is how many ads in "
        "that bucket actually served video. These are correlations across ads that "
        "actually ran, not causal effects — attributes co-vary with budget, bidding and "
        "placement.</footer>"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(
        "<!doctype html><html lang='en' data-theme='dark'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Creative performance — owned Meta accounts</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>{body}</div>"
        f"<script>var ADS={json.dumps(ads, separators=(',', ':'))};"
        f"var FILTERS={filt_json};"
        f"var META_SPECS={json.dumps(meta_specs)};"
        f"var TAIL_SPECS={json.dumps(tail_specs)};"
        f"var VISION_SPECS={json.dumps(vision_specs)};"
        f"var MINIMP={int(min_impressions)};var MAXC={MAX_CARDS_PER_BUCKET};"
        f"var BUCKETS={json.dumps(buckets)};"
        f"var DAYS={json.dumps(days)};"
        f"var RANKWIN={json.dumps(rank_window)};"
        f"var PADS={json.dumps(pads, separators=(',', ':'))};</script>"
        f"<script>{JS}</script></body></html>",
        encoding="utf-8",
    )
    return {
        "path": str(path), "brands": len(brands), "ads": len(ads),
        "spend": total_spend, "analyzed": len(analyzed),
    }
