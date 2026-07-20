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
               oa.audience_stage, oa.audience_gender, oa.audience_age,
               oa.audience_geo, oa.audience_name, oa.optimization_goal,
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
    ("an", "Ad set (audience)", "scalar"),
    ("cp", "Campaign", "scalar"),
    ("ac", "Ad account", "scalar"),
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
.empty{color:var(--dim);padding:24px;text-align:center;background:var(--panel);
border:1px solid var(--line);border-radius:10px}
footer{color:var(--dim);font-size:12px;margin-top:48px;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.wrap{padding:20px 12px 60px}
.assets{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.fld,.fld select{min-width:130px}}
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
  var esc=function(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});};

  // --- aggregation (single definition, used for buckets and baselines) ---
  function agg(list){
    var im=0,sp=0,ck=0,pu=0,rv=0;
    for(var i=0;i<list.length;i++){var a=list[i];
      im+=a.im||0; sp+=a.sp||0; ck+=a.ck||0; pu+=a.pu||0; rv+=a.rv||0;}
    return {ads:list.length,im:im,sp:sp,ck:ck,pu:pu,rv:rv,
      ctr:im?100*ck/im:0, cpm:im?1000*sp/im:0, roas:sp?rv/sp:0};
  }
  function idxCell(v){
    if(!v||!isFinite(v)) return '<span class="flat">&mdash;</span>';
    var c=v>=105?'up':(v<=95?'down':'flat');
    return '<span class="idx '+c+'">'+v.toFixed(0)+'</span>';
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
      return '<div class="fld"><label>'+esc(f[1])+'</label><select data-k="'+key+'">'+
        '<option value="">All ('+opts.length+')</option>'+
        opts.map(function(o){return '<option value="'+esc(o)+'">'+esc(o)+' &middot; '+vals[o]+'</option>';}).join('')+
        '</select></div>';
    }).join('');
    host.querySelectorAll('select').forEach(function(sel){
      sel.addEventListener('change',function(){state[sel.dataset.k]=sel.value;render();});
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
      '<div class="mrow"><span>CTR</span><b>'+(a.im?100*a.ck/a.im:0).toFixed(2)+'%</b></div>'+
      '<div class="mrow"><span>CPM</span><b>$'+(a.im?1000*a.sp/a.im:0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>ROAS</span><b>'+(a.sp?a.rv/a.sp:0).toFixed(2)+'</b></div>'+
      '<div class="mrow"><span>purch.</span><b>'+num(a.pu)+'</b></div>'+
      '</div></div>';
  }

  // --- one attribute table ---
  function tableHTML(spec, pool, base, tid){
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
      DRILL[rid]=e.ads_list;
      return '<tr class="sum" data-target="'+rid+'">'+
        '<td><span class="caret">&#9654;</span>'+esc(e.value)+
        '<div class="bar"><i style="width:'+(100*e.im/maxi).toFixed(0)+'%"></i></div></td>'+
        '<td class="n">'+e.ads+'</td><td>'+num(e.im)+'</td><td>'+money(e.sp)+'</td>'+
        '<td>'+e.ctr.toFixed(2)+'%</td><td>'+idxCell(ctrIdx)+'</td>'+
        '<td>$'+e.cpm.toFixed(2)+'</td><td>'+e.roas.toFixed(2)+'</td><td>'+idxCell(roasIdx)+'</td></tr>'+
        '<tr class="detail" id="'+rid+'" hidden><td colspan="9"><div class="drill"></div></td></tr>';
    }).join('');
    return '<h2>'+esc(label)+'<span class="n">click a row to see its ads</span></h2>'+
      '<div class="tblwrap"><table><thead><tr><th>'+esc(label)+'</th><th>ads</th>'+
      '<th>impressions</th><th>spend</th><th>CTR</th><th>CTR idx</th><th>CPM</th>'+
      '<th>ROAS</th><th>ROAS idx</th></tr></thead><tbody>'+rows+'</tbody>'+
      '<tfoot><tr><td class="n">baseline (current filter)</td><td class="n">'+base.ads+'</td>'+
      '<td class="n">'+num(base.im)+'</td><td class="n">'+money(base.sp)+'</td>'+
      '<td class="n">'+base.ctr.toFixed(2)+'%</td><td class="n">100</td>'+
      '<td class="n">$'+base.cpm.toFixed(2)+'</td><td class="n">'+base.roas.toFixed(2)+'</td>'+
      '<td class="n">100</td></tr></tfoot></table></div>';
  }

  var DRILL={};

  function render(){
    var pool=ADS.filter(passes);
    var readable=pool.filter(function(a){return a.A;});
    DRILL={};

    // headline cards
    var all=agg(pool), rd=agg(readable);
    document.getElementById('cards').innerHTML=[
      ['Ads', num(all.ads)],['Spend', money(all.sp)],['Impressions', num(all.im)],
      ['CTR', all.ctr.toFixed(2)+'%'],['CPM','$'+all.cpm.toFixed(2)],
      ['ROAS', all.roas.toFixed(2)],['Purchases', num(all.pu)],
      ['Analyzed', num(rd.ads)]
    ].map(function(c){return '<div class="card"><div class="k">'+c[0]+'</div><div class="v">'+c[1]+'</div></div>';}).join('');

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
      return;
    }
    var active=FILTERS.filter(function(f){return state[f[0]];})
      .map(function(f){return f[1]+': <b>'+esc(state[f[0]])+'</b>';});
    document.getElementById('fstat').innerHTML= active.length
      ? 'Filtered to <b>'+all.ads+'</b> ads &middot; '+money(all.sp)+' &mdash; '+active.join(' &middot; ')
      : 'Showing all <b>'+all.ads+'</b> ads &middot; '+money(all.sp);

    var html='<h3 class="grp">Audience &amp; metadata &mdash; all ads (100% of filtered spend)</h3>';
    META_SPECS.forEach(function(s,i){ html+=tableHTML(s, pool, all, 'm'+i); });
    if(readable.length){
      html+='<h3 class="grp">Creative attributes &mdash; analyzable ads only ('+pct.toFixed(0)+'% of filtered spend)</h3>';
      VISION_SPECS.forEach(function(s,i){ html+=tableHTML(s, readable, rd, 'v'+i); });
    }else{
      html+='<div class="note">No creative-attribute tables for this filter &mdash; none of the '+
        'matching ads have a readable creative analysis (catalog ads have no fixed creative).</div>';
    }
    document.getElementById('tables').innerHTML=html;
    wireRows();
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
    document.querySelectorAll('#filterFields select').forEach(function(s){s.value='';});
    render();
  });

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

    ads: list[dict[str, Any]] = []
    for r in rows:
        rec: dict[str, Any] = {
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

    total_spend = sum(a["sp"] for a in ads)
    analyzed = [a for a in ads if a.get("A")]
    brands = sorted({a["b"] for a in ads if a["b"]})

    filt_json = json.dumps(FILTERS)
    body = (
        '<div class="topbar"><div>'
        "<h1>Creative performance — owned Meta accounts</h1>"
        f'<div class="sub">{", ".join(html.escape(b) for b in brands)} · '
        "first-party spend joined to creative attributes and audience targeting, on ad id"
        "</div></div>"
        '<button class="toggle" id="themeToggle" type="button">Theme</button></div>'
        '<div class="filters"><div class="frow" id="filterFields"></div>'
        '<div class="frow" style="margin-top:12px">'
        '<button class="fbtn" id="resetFilters" type="button">Reset filters</button>'
        '<div class="fstat" id="fstat" style="margin:0"></div></div></div>'
        '<div class="cards" id="cards"></div>'
        '<div class="note" id="cov"></div>'
        '<div id="tables"></div>'
        "<footer>CTR/ROAS index: 100 = the baseline for the <em>current filter</em>, so "
        "indices re-base as you narrow. Above 100 is better than that baseline, below is "
        "worse. Audience facets are derived from each ad set's targeting spec (custom "
        "audiences, age, gender, geo); funnel stage is inferred, since Meta exposes no "
        "explicit prospecting/retargeting flag. These are correlations across ads that "
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
        f"var VISION_SPECS={json.dumps(vision_specs)};"
        f"var MINIMP={int(min_impressions)};var MAXC={MAX_CARDS_PER_BUCKET};</script>"
        f"<script>{JS}</script></body></html>",
        encoding="utf-8",
    )
    return {
        "path": str(path), "brands": len(brands), "ads": len(ads),
        "spend": total_spend, "analyzed": len(analyzed),
    }
