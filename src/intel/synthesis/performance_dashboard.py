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
h2{font-size:18px;margin:36px 0 12px;display:flex;align-items:baseline;gap:10px}
.sub{color:var(--dim);margin-bottom:24px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.toggle{background:var(--panel);border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:7px 13px;cursor:pointer;font-size:13px;white-space:nowrap}
.toggle:hover{border-color:var(--accent);color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;
box-shadow:var(--shadow)}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
.note{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:8px;padding:12px 14px;margin:16px 0;font-size:13px;color:var(--fg)}
.note b{color:var(--warn)}
.tblwrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
border-radius:10px;margin-bottom:20px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:820px;font-size:13px}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:200px}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tbody tr.sum:last-child td{border-bottom:none}
tbody tr.sum{cursor:pointer}
tbody tr.sum:hover{background:var(--panel2)}
tbody tr.sum.open{background:var(--panel2)}
.caret{display:inline-block;width:11px;color:var(--dim);transition:transform .15s ease;
margin-right:6px;font-size:10px}
tr.sum.open .caret{transform:rotate(90deg);color:var(--accent)}
.idx{font-weight:600}
.up{color:var(--good)} .down{color:var(--bad)} .flat{color:var(--dim)}
.n{color:var(--dim);font-size:12px}
.bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:4px;max-width:280px}
.bar>i{display:block;height:100%;background:var(--accent)}
/* --- drill-down --- */
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
padding:1.5px 6px;border-radius:99px;border:1px solid var(--line);color:var(--dim);margin-bottom:6px}
.pill.dpa{border-color:var(--warn);color:var(--warn)}
footer{color:var(--dim);font-size:12px;margin-top:48px;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.wrap{padding:20px 12px 60px}
.assets{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}}
"""


def _idx_cell(v: float) -> str:
    if not v:
        return '<span class="flat">—</span>'
    cls = "up" if v >= 105 else ("down" if v <= 95 else "flat")
    return f'<span class="idx {cls}">{v:.0f}</span>'


def _render_table(t: dict, tid: str) -> str:
    """One attribute table. Each value row is clickable and expands a hidden
    sibling row that JS fills with the ads in that bucket.

    The ad cards are NOT emitted inline per bucket: an ad belongs to many buckets
    (one per attribute), so inlining would repeat the same markup dozens of times
    and balloon the file. Instead each row carries just its ad ids and the cards
    are rendered on demand from a single shared JSON blob.
    """
    maxi = max((e["impressions"] for e in t["entries"]), default=0) or 1
    rows = []
    for i, e in enumerate(t["entries"]):
        pct = 100.0 * e["impressions"] / maxi
        ids = ",".join(e.get("ad_ids") or [])
        rid = f"{tid}-{i}"
        rows.append(
            f'<tr class="sum" data-target="{rid}" data-ads="{html.escape(ids)}">'
            f'<td><span class="caret">&#9654;</span>{html.escape(e["value"])}'
            f'<div class="bar"><i style="width:{pct:.0f}%"></i></div></td>'
            f'<td class="n">{int(e["ads"])}</td>'
            f'<td>{e["impressions"]:,.0f}</td>'
            f'<td>${e["spend"]:,.0f}</td>'
            f'<td>{e["ctr"]:.2f}%</td>'
            f"<td>{_idx_cell(e['ctr_index'])}</td>"
            f'<td>${e["cpm"]:,.2f}</td>'
            f'<td>{e["roas"]:.2f}</td>'
            f"<td>{_idx_cell(e['roas_index'])}</td></tr>"
            f'<tr class="detail" id="{rid}" hidden><td colspan="9">'
            f'<div class="drill"></div></td></tr>'
        )
    b = t["baseline"]
    return (
        f"<h2>{html.escape(t['label'])}"
        f'<span class="n" style="font-weight:400">click a row to see its ads</span></h2>'
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


# Cap on cards rendered per expanded bucket. Announced in the drill-down header
# whenever it bites — a silently truncated list reads as "these are all the ads".
MAX_CARDS_PER_BUCKET = 60

JS = """
(function(){
  var root=document.documentElement;
  var saved=null;
  try{saved=localStorage.getItem('perfdash-theme');}catch(e){}
  root.setAttribute('data-theme', saved || 'dark');
  var btn=document.getElementById('themeToggle');
  function label(){btn.textContent=root.getAttribute('data-theme')==='dark'?'\u2600 Light':'\u263E Dark';}
  label();
  btn.addEventListener('click',function(){
    var next=root.getAttribute('data-theme')==='dark'?'light':'dark';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem('perfdash-theme',next);}catch(e){}
    label();
  });

  var money=function(n){return '$'+(n||0).toLocaleString(undefined,{maximumFractionDigits:0});};
  var num=function(n){return (n||0).toLocaleString(undefined,{maximumFractionDigits:0});};

  function card(a){
    if(!a) return '';
    var img = a.img
      ? '<img class="thumb" loading="lazy" src="'+a.img+'" alt="">'
      : '<div class="noimg">no fixed creative<br>(catalog / dynamic ad)</div>';
    var pill = a.cls==='analyzable'
      ? '<span class="pill">'+(a.at||'creative')+'</span>'
      : '<span class="pill dpa">'+a.cls+'</span>';
    return '<div class="asset">'+img+'<div class="body">'+pill+
      '<div class="nm" title="'+(a.nm||'').replace(/"/g,'&quot;')+'">'+(a.nm||'(unnamed)')+'</div>'+
      '<div class="mrow"><span>spend</span><b>'+money(a.sp)+'</b></div>'+
      '<div class="mrow"><span>impr.</span><b>'+num(a.im)+'</b></div>'+
      '<div class="mrow"><span>CTR</span><b>'+(a.ctr||0).toFixed(2)+'%</b></div>'+
      '<div class="mrow"><span>CPM</span><b>$'+(a.cpm||0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>ROAS</span><b>'+(a.roas||0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>purch.</span><b>'+num(a.pu)+'</b></div>'+
      '</div></div>';
  }

  document.querySelectorAll('tr.sum').forEach(function(tr){
    tr.addEventListener('click',function(){
      var det=document.getElementById(tr.dataset.target);
      if(!det) return;
      var open=!det.hidden;
      if(open){det.hidden=true;tr.classList.remove('open');return;}
      var host=det.querySelector('.drill');
      if(!host.dataset.filled){
        var ids=(tr.dataset.ads||'').split(',').filter(Boolean);
        var total=ids.length;
        var shown=ids.slice(0,MAXC);
        var head='<div class="drillhead"><span>'+total+' ad'+(total===1?'':'s')+
          ' in this bucket, highest spend first</span>'+
          (total>shown.length?'<span>showing top '+shown.length+' of '+total+'</span>':'')+
          '</div>';
        host.innerHTML=head+'<div class="assets">'+shown.map(function(id){return card(ADS[id]);}).join('')+'</div>';
        host.dataset.filled='1';
      }
      det.hidden=false;tr.classList.add('open');
    });
  });
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

    # One shared record per ad, keyed by platform ad id. Emitted once and reused
    # by every bucket's drill-down (an ad appears in many buckets).
    ads_blob = {}
    for r in rows:
        imp = r.get("impressions") or 0
        spend = r.get("spend") or 0
        clicks = r.get("clicks") or 0
        ads_blob[r["platform_ad_id"]] = {
            "nm": r.get("ad_name") or r.get("title") or "",
            "cls": r.get("creative_class") or "unknown",
            "at": r.get("asset_type") or "",
            "img": r.get("asset_path") or "",
            "sp": round(spend, 2),
            "im": imp,
            "ctr": (100.0 * clicks / imp) if imp else 0.0,
            "cpm": (1000.0 * spend / imp) if imp else 0.0,
            # Recomputed from summed revenue/spend rather than averaging Meta's
            # per-window roas figures, which would weight windows equally.
            "roas": ((r.get("revenue") or 0) / spend) if spend else 0.0,
            "pu": r.get("purchases") or 0,
        }

    body = [
        '<div class="topbar"><div>'
        "<h1>Creative performance — owned Meta accounts</h1>"
        f'<div class="sub">{", ".join(html.escape(b) for b in brands)} · '
        "first-party spend joined to creative attributes on ad id</div></div>"
        '<button class="toggle" id="themeToggle" type="button">Theme</button></div>',
        f'<div class="cards">{cards}</div>',
        coverage_note,
    ]
    if meta_tables:
        body.append("<h2 style='margin-top:40px'>All ads — metadata attributes "
                    "<span class='n'>(100% of spend)</span></h2>")
        body.extend(_render_table(tb, f"m{i}") for i, tb in enumerate(meta_tables))
    if attr_tables:
        body.append("<h2 style='margin-top:40px'>Analyzable creative — vision attributes "
                    f"<span class='n'>({cov_pct:.0f}% of spend)</span></h2>")
        body.extend(_render_table(tb, f"a{i}") for i, tb in enumerate(attr_tables))
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
    # data-theme is stamped on <html> up front so the dark default paints on the
    # first frame; the inline script then restores any saved preference. Without
    # it the page flashes unstyled-light before JS runs.
    path.write_text(
        "<!doctype html><html lang='en' data-theme='dark'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Creative performance — owned Meta accounts</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        f"{''.join(body)}</div>"
        f"<script>var ADS={json.dumps(ads_blob)};"
        f"var MAXC={MAX_CARDS_PER_BUCKET};</script>"
        f"<script>{JS}</script>"
        "</body></html>",
        encoding="utf-8",
    )
    return {
        "path": str(path), "brands": len(brands), "ads": len(rows),
        "spend": total_spend, "analyzed": len(analyzed),
        "analyzable": len(analyzable), "coverage_pct": cov_pct,
    }
