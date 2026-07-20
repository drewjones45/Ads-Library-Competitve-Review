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


def _fetch(conn: sqlite3.Connection, competitor_ids: list[str] | None) -> list[dict]:
    """One row per owned ad, with metrics summed across windows and the vision
    analysis of its best available creative attached.

    An ad can have several creative rows (dynamic creative serves multiple
    variants, and a rendered preview sits alongside the raw asset). The preview
    is preferred when present because it is the ad as actually served; otherwise
    the first analyzed asset wins.
    """
    where, params = "", []
    if competitor_ids:
        where = f"WHERE oa.competitor_id IN ({','.join('?' * len(competitor_ids))})"
        params = list(competitor_ids)

    rows = conn.execute(f"""
        SELECT oa.platform_ad_id, oa.competitor_id, oa.account_name, oa.ad_name,
               oa.campaign_name, oa.creative_class, oa.object_type, oa.cta_type,
               oa.title, oa.body,
               SUM(p.impressions) AS impressions, SUM(p.spend) AS spend,
               SUM(p.clicks) AS clicks, SUM(p.link_clicks) AS link_clicks,
               SUM(p.purchases) AS purchases, SUM(p.revenue) AS revenue,
               SUM(p.thruplays) AS thruplays, SUM(p.video_p100) AS video_p100,
               MAX(p.frequency) AS frequency
        FROM owned_ads oa
        LEFT JOIN ad_performance p ON p.platform_ad_id = oa.platform_ad_id
        {where}
        GROUP BY oa.platform_ad_id
    """, params).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # Prefer the rendered preview's analysis; fall back to any analyzed asset.
        cre = conn.execute("""
            SELECT c.asset_type, c.analysis_json
            FROM creatives c
            JOIN owned_ads oa ON oa.ad_db_id = c.ad_id
            WHERE oa.platform_ad_id = ? AND c.analysis_json IS NOT NULL
            ORDER BY CASE c.asset_type WHEN 'ad_preview' THEN 0 ELSE 1 END, c.id
            LIMIT 1
        """, (d["platform_ad_id"],)).fetchone()
        d["analysis"] = None
        if cre and cre["analysis_json"]:
            try:
                d["analysis"] = json.loads(cre["analysis_json"])
            except json.JSONDecodeError:
                d["analysis"] = None
        out.append(d)
    return out


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

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e8eaf0;--dim:#9aa3b2;
--good:#3fb950;--bad:#f85149;--accent:#58a6ff;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:18px;margin:36px 0 12px}
.sub{color:var(--dim);margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
.note{background:#1d1a12;border:1px solid #3d3524;border-left:3px solid var(--warn);
border-radius:8px;padding:12px 14px;margin:16px 0;color:#e8dcc0;font-size:13px}
.note b{color:var(--warn)}
.tblwrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
border-radius:10px;margin-bottom:20px}
table{border-collapse:collapse;width:100%;min-width:760px;font-size:13px}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:180px}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:#1c2029}
.idx{font-weight:600}
.up{color:var(--good)} .down{color:var(--bad)} .flat{color:var(--dim)}
.n{color:var(--dim);font-size:12px}
.bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;background:var(--accent)}
footer{color:var(--dim);font-size:12px;margin-top:48px;border-top:1px solid var(--line);padding-top:16px}
@media(prefers-color-scheme:light){
:root{--bg:#fbfcfd;--panel:#fff;--line:#e3e7ec;--fg:#1b1f27;--dim:#5c6672;--accent:#0969da}
.note{background:#fff8e6;border-color:#e6d5a8;color:#5c4a1a}
tbody tr:hover{background:#f5f7fa}}
"""


def _idx_cell(v: float) -> str:
    if not v:
        return '<span class="flat">—</span>'
    cls = "up" if v >= 105 else ("down" if v <= 95 else "flat")
    return f'<span class="idx {cls}">{v:.0f}</span>'


def _render_table(t: dict) -> str:
    maxi = max((e["impressions"] for e in t["entries"]), default=0) or 1
    rows = []
    for e in t["entries"]:
        pct = 100.0 * e["impressions"] / maxi
        rows.append(
            f"<tr><td>{html.escape(e['value'])}"
            f'<div class="bar"><i style="width:{pct:.0f}%"></i></div></td>'
            f'<td class="n">{int(e["ads"])}</td>'
            f'<td>{e["impressions"]:,.0f}</td>'
            f'<td>${e["spend"]:,.0f}</td>'
            f'<td>{e["ctr"]:.2f}%</td>'
            f"<td>{_idx_cell(e['ctr_index'])}</td>"
            f'<td>${e["cpm"]:,.2f}</td>'
            f'<td>{e["roas"]:.2f}</td>'
            f"<td>{_idx_cell(e['roas_index'])}</td></tr>"
        )
    b = t["baseline"]
    return (
        f"<h2>{html.escape(t['label'])}</h2>"
        f'<div class="tblwrap"><table><thead><tr>'
        f"<th>{html.escape(t['label'])}</th><th>ads</th><th>impressions</th><th>spend</th>"
        f"<th>CTR</th><th>CTR idx</th><th>CPM</th><th>ROAS</th><th>ROAS idx</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody>"
        f'<tfoot><tr><td class="n">baseline (all ads in scope)</td>'
        f'<td class="n">{int(b["ads"])}</td><td class="n">{b["impressions"]:,.0f}</td>'
        f'<td class="n">${b["spend"]:,.0f}</td><td class="n">{b["ctr"]:.2f}%</td>'
        f'<td class="n">100</td><td class="n">${b["cpm"]:,.2f}</td>'
        f'<td class="n">{b["roas"]:.2f}</td><td class="n">100</td></tr></tfoot>'
        f"</table></div>"
    )


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

    total_spend = sum(r.get("spend") or 0 for r in rows)
    total_imp = sum(r.get("impressions") or 0 for r in rows)
    analyzed = [r for r in rows if r.get("analysis")]
    analyzable = [r for r in rows if r.get("creative_class") == "analyzable"]
    an_spend = sum(r.get("spend") or 0 for r in analyzed)
    brands = sorted({r["competitor_id"] for r in rows if r.get("competitor_id")})

    overall = _bucket_stats(rows)
    meta_tables = _metadata_table(rows, min_impressions)
    attr_tables = _attribute_table(rows, min_impressions)

    cov_pct = (100.0 * an_spend / total_spend) if total_spend else 0.0
    coverage_note = (
        f'<div class="note"><b>Coverage:</b> creative-attribute tables below cover '
        f'<b>{len(analyzed)}</b> of {len(rows)} ads — <b>${an_spend:,.0f}</b> of '
        f'${total_spend:,.0f} spend ({cov_pct:.0f}%). The remainder is mostly '
        f'catalog/dynamic-product ads, whose creative is assembled per product at '
        f'serve time, so there is no fixed image to analyze. Those ads are still '
        f'included in the metadata tables (CTA, object type, campaign), which cover '
        f'100% of spend. Attribute figures are impression-weighted; buckets under '
        f'{min_impressions:,} impressions are dropped as too thin to read.</div>'
    )

    cards = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in [
            ("Brands", str(len(brands))),
            ("Ads", f"{len(rows):,}"),
            ("Spend", f"${total_spend:,.0f}"),
            ("Impressions", f"{total_imp:,.0f}"),
            ("Blended CTR", f'{overall["ctr"]:.2f}%'),
            ("Blended ROAS", f'{overall["roas"]:.2f}'),
            ("Analyzed creatives", f"{len(analyzed):,}"),
        ]
    )

    body = [
        "<h1>Creative performance — owned Meta accounts</h1>",
        f'<div class="sub">{", ".join(html.escape(b) for b in brands)} · '
        f"first-party spend joined to creative attributes on ad id</div>",
        f'<div class="cards">{cards}</div>',
        coverage_note,
    ]
    if meta_tables:
        body.append("<h2 style='margin-top:40px'>All ads — metadata attributes "
                    "<span class='n'>(100% of spend)</span></h2>")
        body.extend(_render_table(t) for t in meta_tables)
    if attr_tables:
        body.append("<h2 style='margin-top:40px'>Analyzable creative — vision attributes "
                    f"<span class='n'>({cov_pct:.0f}% of spend)</span></h2>")
        body.extend(_render_table(t) for t in attr_tables)
    else:
        body.append(
            '<div class="note">No vision-attribute tables yet — the analyzable '
            "creatives have not been vision-analyzed. Run the creative analysis "
            "pass, then rebuild.</div>"
        )
    body.append(
        "<footer>CTR/ROAS index: 100 = the scope baseline. Above 100 is better than "
        "average, below is worse. These are correlations across existing ads, not "
        "causal effects — attributes co-vary with targeting, placement and budget."
        "</footer>"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Creative performance — owned Meta accounts</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        f"{''.join(body)}</div></body></html>",
        encoding="utf-8",
    )
    return {
        "path": str(path), "brands": len(brands), "ads": len(rows),
        "spend": total_spend, "analyzed": len(analyzed),
        "analyzable": len(analyzable), "coverage_pct": cov_pct,
    }
