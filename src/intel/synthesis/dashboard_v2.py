"""Static HTML dashboard, v2 — dark-mode redesign with a light-mode toggle.

A full visual redesign of `dashboard.py` modeled on the "Horizon Unified"
analytics aesthetic (UI Inspo/): near-black navy page, elevated cards with
subtle borders, bright blue accent, KPI stat cards, pill navigation, dark
tables. Defaults to dark; a top-bar toggle flips to a light theme and the
choice persists via localStorage.

This module is a deliberate DUPLICATE of the v1 dashboard: it imports the
data layer (`_collect`) and pure helpers read-only from `dashboard.py` and
rewrites every render function, the CSS, and the JS. v1 stays untouched and
remains the default; render with `intel dashboard --v2`.

All theme colors live in the two `html[data-theme=...]` token blocks below —
nothing outside those blocks may hardcode a color, or it will be wrong in one
of the two themes (heatmap cells re-theme via CSS color-mix on a `--share`
var; landing-bar SVG segments via per-section fill vars).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..analysis.landing import SECTION_CALLOUTS, SECTION_LABELS
from ..storage import connect
from .creative_readout import _LIST_ATTRS, _SCALAR_ATTRS
from .dashboard import (
    DEFAULT_ORG,
    _collect,
    _esc,
    _fmt_metric,
    _format_duration,
    _meta_ad_url,
    _perf_block_html,
    _relpath,
    _render_tv_spots_panel,
    _thumb_src,
    _video_meta_for_render,
)

# Combined with DEFAULT_ORG this brands the dashboard "Horizon Commerce
# Intelligence" — the topbar stacks org over product like the inspo lockup.
DEFAULT_PRODUCT_V2 = "Intelligence"


# ----- theming --------------------------------------------------------------

# Applied in <head> BEFORE the stylesheet so the stored theme paints first —
# no flash of the wrong theme on reload. localStorage can throw on file://
# in some browsers (Safari private windows), hence the try/catch.
HEAD_THEME_SCRIPT = """
(function () {
  var t = 'dark';
  try {
    var s = localStorage.getItem('intelDashTheme');
    if (s === 'light' || s === 'dark') t = s;
  } catch (e) {}
  document.documentElement.setAttribute('data-theme', t);
})();
"""

CSS_V2 = """
/* ===== THEME TOKENS ===== */
:root {
  --radius-card: 10px; --radius-ctl: 8px; --radius-chip: 6px; --radius-pill: 999px;
  --topbar-h: 56px; --sidebar-w: 232px;
  --font-ui: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --font-mono: ui-monospace, Menlo, Consolas, monospace;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg-page: #0a0e17;
  --bg-card: #111a2b;
  --bg-elev: #18223a;
  --bg-inset: #0d1422;
  --border-1: #1f2a40;
  --border-2: #2d3c5e;
  --text-1: #e9eef9;
  --text-2: #9aa8c7;
  --text-3: #627192;
  --accent: #548af7;
  --accent-strong: #2f6bdf;
  --accent-soft: rgba(84,138,247,0.14);
  --accent-contrast: #ffffff;
  --pos: #30c184;  --pos-soft: rgba(48,193,132,0.13);
  --neg: #e5484d;  --neg-soft: rgba(229,72,77,0.13);
  --warn: #f5a623; --warn-soft: rgba(245,166,35,0.12);
  --amazon: #ffa033;   --amazon-soft: rgba(255,160,51,0.10);
  --homepage: #34c97b; --homepage-soft: rgba(52,201,123,0.10);
  --google: #a78bfa;   --google-soft: rgba(167,139,250,0.12);
  --tv: #e879b9;       --tv-soft: rgba(232,121,185,0.12);
  --heat-lo: #101827; --heat-hi: #3f74e0; --heat-hi-text: #ffffff;
  --chart-bar: #4d76c9; --chart-line: #9db9f0;
  --pri-high: #e5484d; --pri-med: #f5a623; --pri-low: #627192;
  --shadow-1: 0 1px 2px rgba(0,0,0,0.5);
  --shadow-pop: 0 8px 24px rgba(0,0,0,0.55);
  --scrim: rgba(2,6,16,0.88);
  --topbar-bg: rgba(10,14,23,0.92);
  --sec-homepage: #8b97a8;
  --sec-product-browse: #46b369;
  --sec-product-detail: #2f9e57;
  --sec-samples: #cf9050;
  --sec-quote-lead: #bd7a40;
  --sec-where-to-buy: #548af7;
  --sec-inspiration-content: #9d7bd8;
  --sec-brand-story: #b388d9;
  --sec-template-unfilled: #f5a623;
  --sec-off-brand-tracker: #f06a65;
  --sec-off-brand-short: #e25549;
  --sec-off-brand-other: #c94436;
  --sec-unknown: #7d8794;
}
html[data-theme="light"] {
  color-scheme: light;
  --bg-page: #f4f6fb;
  --bg-card: #ffffff;
  --bg-elev: #eef2f9;
  --bg-inset: #f7f9fd;
  --border-1: #e2e8f2;
  --border-2: #c6d2e6;
  --text-1: #18233a;
  --text-2: #4f5f7e;
  --text-3: #8290ab;
  --accent: #2f6bdf;
  --accent-strong: #2456b8;
  --accent-soft: rgba(47,107,223,0.10);
  --accent-contrast: #ffffff;
  --pos: #178a50;  --pos-soft: rgba(23,138,80,0.10);
  --neg: #d4333f;  --neg-soft: rgba(212,51,63,0.10);
  --warn: #b96e00; --warn-soft: rgba(224,142,0,0.12);
  --amazon: #e88a00;   --amazon-soft: rgba(232,138,0,0.10);
  --homepage: #2da44e; --homepage-soft: rgba(45,164,78,0.10);
  --google: #7c3aed;   --google-soft: rgba(124,58,237,0.10);
  --tv: #c0398a;       --tv-soft: rgba(192,57,138,0.10);
  --heat-lo: #eef1f7; --heat-hi: #2a5fb0; --heat-hi-text: #ffffff;
  --chart-bar: #3d6cc4; --chart-line: #6f97e8;
  --pri-high: #d9534f; --pri-med: #f0ad4e; --pri-low: #8290ab;
  --shadow-1: 0 1px 2px rgba(16,24,40,0.06);
  --shadow-pop: 0 8px 24px rgba(16,24,40,0.14);
  --scrim: rgba(15,23,42,0.80);
  --topbar-bg: rgba(244,246,251,0.92);
  --sec-homepage: #6c757d;
  --sec-product-browse: #2a6c3a;
  --sec-product-detail: #1e7e34;
  --sec-samples: #7a4a1c;
  --sec-quote-lead: #a06030;
  --sec-where-to-buy: #2a5fb0;
  --sec-inspiration-content: #5e3a8a;
  --sec-brand-story: #854a99;
  --sec-template-unfilled: #e08e00;
  --sec-off-brand-tracker: #d9534f;
  --sec-off-brand-short: #c0392b;
  --sec-off-brand-other: #a93226;
  --sec-unknown: #9a9a9a;
}
/* ===== END THEME TOKENS ===== */

* { box-sizing: border-box; }
body { font: 14px/1.5 var(--font-ui); margin: 0; color: var(--text-1);
       background: var(--bg-page); -webkit-font-smoothing: antialiased;
       accent-color: var(--accent); }
code { font-family: var(--font-mono); }
a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Top app bar */
.topbar { position: fixed; top: 0; left: 0; right: 0; height: var(--topbar-h); z-index: 60;
          display: flex; align-items: center; gap: 18px; padding: 0 20px;
          background: var(--topbar-bg); backdrop-filter: blur(8px);
          -webkit-backdrop-filter: blur(8px);
          border-bottom: 1px solid var(--border-1); }
.topbar-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.logo-mark { width: 28px; height: 28px; border-radius: 8px; flex: 0 0 auto;
             background: linear-gradient(135deg, var(--accent), var(--accent-strong));
             display: flex; align-items: center; justify-content: center;
             color: var(--accent-contrast); font-weight: 800; font-size: 13px; }
.topbar-title { line-height: 1.2; min-width: 0; }
.topbar-title .org { font-size: 13.5px; font-weight: 700; color: var(--text-1); white-space: nowrap; }
.topbar-title .product { font-size: 10.5px; color: var(--text-3); white-space: nowrap;
                         overflow: hidden; text-overflow: ellipsis; }
.topbar-nav { position: absolute; left: 50%; transform: translateX(-50%);
              display: flex; gap: 4px; background: var(--bg-inset);
              border: 1px solid var(--border-1); border-radius: var(--radius-pill); padding: 3px; }
.topbar-nav a { color: var(--text-2); text-decoration: none; font-size: 12.5px; font-weight: 600;
                padding: 5px 14px; border-radius: var(--radius-pill); white-space: nowrap;
                transition: color .15s ease, background .15s ease; }
.topbar-nav a:hover { color: var(--text-1); }
.topbar-nav a.active { background: var(--accent-soft); color: var(--accent); }
.topbar-right { display: flex; align-items: center; gap: 12px; margin-left: auto; }
.topbar-meta { font-size: 11px; color: var(--text-3); white-space: nowrap; }
#theme-toggle { background: var(--bg-inset); border: 1px solid var(--border-1);
                border-radius: var(--radius-pill); width: 34px; height: 34px; flex: 0 0 auto;
                display: flex; align-items: center; justify-content: center; cursor: pointer;
                color: var(--text-2); transition: color .15s ease, border-color .15s ease; }
#theme-toggle:hover { color: var(--text-1); border-color: var(--border-2); }
#theme-toggle svg { width: 16px; height: 16px; display: block; }
html[data-theme="dark"] #theme-toggle .icon-moon { display: none; }
html[data-theme="light"] #theme-toggle .icon-sun { display: none; }

/* Left sidebar */
nav.sidebar { position: fixed; left: 0; top: var(--topbar-h); bottom: 0; width: var(--sidebar-w);
              padding: 14px 12px 28px; overflow-y: auto; z-index: 40;
              background: var(--bg-page); border-right: 1px solid var(--border-1); }
nav.sidebar .nav-group { padding: 0 10px; font-size: 10px; letter-spacing: 1.3px;
                         text-transform: uppercase; color: var(--text-3); font-weight: 700;
                         margin: 18px 0 6px; }
nav.sidebar a { display: block; color: var(--text-2); text-decoration: none; font-size: 13px;
                padding: 6px 10px; border-radius: 7px; margin: 1px 0;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                transition: color .12s ease, background .12s ease; }
nav.sidebar a:hover { color: var(--text-1); background: var(--bg-elev); }
nav.sidebar a.active { color: var(--accent); background: var(--accent-soft); font-weight: 600; }

main { margin-left: var(--sidebar-w); padding: calc(var(--topbar-h) + 18px) 28px 90px;
       max-width: 1340px; }
section { margin: 26px 0; background: var(--bg-card); border: 1px solid var(--border-1);
          border-radius: var(--radius-card); padding: 22px 26px; box-shadow: var(--shadow-1);
          scroll-margin-top: calc(var(--topbar-h) + 14px); }
section h2 { margin: 0 0 6px; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
section h2 .muted { font-weight: 400; font-size: 13px; }
section > p.muted { margin: 0 0 16px; color: var(--text-2); font-size: 12.5px; }
section h3 { margin: 24px 0 10px; font-size: 11px; text-transform: uppercase;
             color: var(--text-3); letter-spacing: 1px; font-weight: 700; }
.muted { color: var(--text-3); font-size: 12px; }
[hidden] { display: none !important; }
.flag-red { color: var(--neg); font-weight: 600; }
.flag-orange { color: var(--warn); font-weight: 600; }

/* KPI stat cards */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat { background: var(--bg-elev); border: 1px solid var(--border-1);
        border-radius: var(--radius-ctl); padding: 14px 16px; min-width: 0; }
.stat .label { font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
               color: var(--text-3); font-weight: 600; }
.stat .value { font-size: 26px; font-weight: 700; color: var(--text-1); margin-top: 6px;
               line-height: 1.15; font-variant-numeric: tabular-nums;
               display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.stat .value.sm { font-size: 14px; font-weight: 600; }
.stat-chip { font-size: 11px; font-weight: 700; padding: 2px 7px;
             border-radius: var(--radius-chip); letter-spacing: 0.3px; }
.stat-chip.up { background: var(--pos-soft); color: var(--pos); }
.stat-chip.neutral { background: var(--bg-inset); color: var(--text-3); }

/* Brand cards */
.brand-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.brand-card { background: var(--bg-elev); border: 1px solid var(--border-1);
              border-radius: var(--radius-ctl); padding: 14px 16px;
              transition: border-color .15s ease; }
.brand-card:hover { border-color: var(--accent); }
.brand-card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.brand-card a { color: var(--accent); text-decoration: none; font-weight: 700; font-size: 14.5px; }
.brand-card .vertical { font-size: 10px; color: var(--text-3); text-transform: uppercase;
                        letter-spacing: 0.5px; white-space: nowrap; }
.pri-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           margin-right: 6px; vertical-align: 0; }
.pri-dot.pri-high { background: var(--pri-high); }
.pri-dot.pri-medium { background: var(--pri-med); }
.pri-dot.pri-low { background: var(--pri-low); }
.brand-card dl { display: grid; grid-template-columns: auto auto; gap: 4px 12px;
                 margin: 12px 0 0; font-size: 12px; }
.brand-card dt { color: var(--text-3); }
.brand-card dd { margin: 0; font-weight: 600; color: var(--text-2); text-align: right;
                 font-variant-numeric: tabular-nums; }

/* Tables (heatmaps + shared chrome) */
table { border-collapse: collapse; width: 100%; margin: 8px 0 18px; font-size: 12px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border-1); }
th { background: var(--bg-elev); color: var(--text-2); font-weight: 600; font-size: 10.5px;
     text-transform: uppercase; letter-spacing: 0.7px; }
th code { text-transform: none; letter-spacing: 0; font-size: 11px; }
td.heat { text-align: center; font-variant-numeric: tabular-nums; color: var(--text-1);
          background: var(--heat-lo);
          background: color-mix(in srgb, var(--heat-hi) calc(var(--share, 0) * 1%), var(--heat-lo)); }
td.heat.hi { color: var(--heat-hi-text); }
td.brand-label { font-family: var(--font-mono); font-size: 11px; color: var(--text-2);
                 background: var(--bg-inset); }
tr.set-row td { background: var(--bg-elev); color: var(--text-1); font-weight: 700;
                border-top: 2px solid var(--border-2); }

/* Distinctiveness */
.distinct-row { display: grid; grid-template-columns: 130px 150px 1fr 90px; gap: 10px;
                padding: 9px 12px; margin: 4px 0; border-radius: var(--radius-chip);
                background: var(--bg-elev); font-size: 12px; align-items: center; }
.distinct-row code { color: var(--text-1); }
.delta { color: var(--accent); background: var(--accent-soft); padding: 2px 8px;
         border-radius: var(--radius-pill); font-weight: 700; text-align: center;
         font-variant-numeric: tabular-nums; }

/* Creative gallery */
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px;
           margin-top: 12px; }
.creative { border: 1px solid var(--border-1); border-radius: var(--radius-ctl); overflow: hidden;
            background: var(--bg-elev); display: flex; flex-direction: column;
            transition: border-color .15s ease; }
.creative:hover { border-color: var(--border-2); }
.creative img { width: 100%; aspect-ratio: 1; object-fit: cover; background: var(--bg-inset);
                cursor: zoom-in; display: block; }
.creative .body { padding: 9px 11px; font-size: 11px; }
.creative .summary { color: var(--text-2); line-height: 1.4; margin-bottom: 4px; }
.creative .tags { margin-top: 6px; }
.tag { display: inline-block; background: var(--accent-soft); color: var(--accent); font-size: 10px;
       font-weight: 600; padding: 2px 7px; border-radius: var(--radius-chip); margin: 2px 3px 0 0; }
.tag.kf { background: var(--warn-soft); color: var(--warn); }
.tag.prod { background: var(--pos-soft); color: var(--pos); }

/* Brand-store / homepage thumbnails. The img inside lacks any intrinsic
   constraint, so without these rules natural-resolution product photos
   (often 1500+ px) blow past their grid cell. Force a 140px crop. */
.thumb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
              gap: 10px; margin-top: 12px; }
.bs-thumb { overflow: hidden; border-radius: var(--radius-chip); background: var(--bg-inset);
            border: 1px solid var(--border-1); min-width: 0; }
.bs-thumb img { display: block; width: 100%; height: 140px; object-fit: cover;
                background: var(--bg-inset); cursor: zoom-in; }
.bs-thumb .muted { padding: 4px 6px; line-height: 1.3; max-height: 34px;
                   overflow: hidden; text-overflow: ellipsis; font-size: 11px; }
/* Stitched full-page captures can be 5000+ px tall — show a fixed-size
   preview tile that's click-through to the full image. */
.landing-tile { display: block; width: 280px; max-width: 100%; height: 360px;
                overflow: hidden; border: 1px solid var(--border-1);
                border-radius: var(--radius-ctl); background: var(--bg-inset); position: relative; }
.landing-tile img { width: 100%; height: auto; display: block; transition: transform .3s ease; }
.landing-tile:hover img { transform: translateY(-8px); }
.landing-tile::after { content: 'click to open full page'; position: absolute;
                       bottom: 0; left: 0; right: 0; background: var(--scrim);
                       color: var(--accent-contrast); font-size: 11px; padding: 4px 8px;
                       text-align: center; opacity: 0; transition: opacity .15s ease; }
.landing-tile:hover::after { opacity: 1; }
.lane-hp img, .lane-bs img { max-width: 100%; }
.two-col { display: grid; grid-template-columns: auto 1fr; gap: 18px; align-items: start; }

/* Whitespace */
.ws { margin: 8px 0; padding: 11px 14px; border-left: 3px solid var(--neg);
      background: var(--neg-soft); border-radius: 0 var(--radius-chip) var(--radius-chip) 0; }
.ws-brand { font-family: var(--font-mono); font-weight: 700; font-size: 13px;
            margin-bottom: 4px; color: var(--text-1); }
.ws-item { font-size: 12px; margin-left: 12px; color: var(--text-2); }

/* Landing pages — "where ads send traffic" */
.lp-toolbar { display: flex; align-items: center; gap: 12px; margin: 4px 0 14px;
              font-size: 12px; color: var(--text-3); flex-wrap: wrap; }
.lp-mode { display: inline-flex; gap: 0; border: 1px solid var(--border-1);
           border-radius: var(--radius-pill); overflow: hidden; background: var(--bg-inset);
           padding: 2px; }
.lp-mode label { padding: 4px 12px; cursor: pointer; color: var(--text-2); font-weight: 600;
                 border-radius: var(--radius-pill); transition: color .12s ease, background .12s ease; }
.lp-mode label:hover { color: var(--text-1); }
.lp-mode label:has(input:checked) { background: var(--accent); color: var(--accent-contrast); }
.lp-mode input { position: absolute; opacity: 0; pointer-events: none; }
.lp-brand-card { border: 1px solid var(--border-1); border-radius: var(--radius-ctl);
                 padding: 16px 18px; margin: 10px 0 16px; background: var(--bg-elev); }
.lp-brand-card.empty { background: var(--bg-inset); color: var(--text-3); font-style: italic; }
.lp-brand-head { display: flex; align-items: baseline; justify-content: space-between;
                 gap: 14px; margin-bottom: 10px; }
.lp-brand-head .lp-brand { font-weight: 700; font-size: 14px; color: var(--text-1); }
.lp-brand-head .lp-stats { color: var(--text-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.lp-bar { width: 100%; height: 22px; background: var(--bg-inset);
          border-radius: var(--radius-chip); overflow: hidden; display: block; }
.lp-bar rect { transition: width .25s ease, x .25s ease; }
.lp-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px;
             font-size: 11px; color: var(--text-2); }
.lp-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.lp-legend-swatch { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
.lp-table { width: 100%; border-collapse: collapse; margin: 12px 0 0; font-size: 12px; }
.lp-table th.num, .lp-table td.num { text-align: right; }
.lp-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-1); vertical-align: top; }
.lp-table td.num { font-variant-numeric: tabular-nums; color: var(--text-2); }
.lp-table td.url { font-family: var(--font-mono); font-size: 11px; word-break: break-all;
                   max-width: 340px; }
.lp-table td.url a { color: var(--accent); text-decoration: none; }
.lp-table td.url a:hover { text-decoration: underline; }
.lp-table tr.flagged td.section { color: var(--neg); font-weight: 600; }
.lp-section-pill { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
                   vertical-align: middle; margin-right: 7px; }
.lp-findings { margin-top: 10px; padding: 10px 13px; background: var(--warn-soft);
               border-left: 3px solid var(--warn);
               border-radius: 0 var(--radius-chip) var(--radius-chip) 0;
               font-size: 12px; color: var(--text-2); }
.lp-findings strong { color: var(--text-1); }
.lp-findings em { color: var(--warn); font-style: normal; font-weight: 600; }
.lp-findings.danger { background: var(--neg-soft); border-left-color: var(--neg); }
.lp-findings.danger em { color: var(--neg); }
.lp-findings code { background: var(--bg-inset); padding: 0 4px; border-radius: 3px; }

/* Landing-section palette hooks. SVG segments take fill from the theme vars;
   legend swatches + table pills take background from the same vars. */
.lp-seg--homepage { fill: var(--sec-homepage); }            .sec-bg--homepage { background: var(--sec-homepage); }
.lp-seg--product-browse { fill: var(--sec-product-browse); } .sec-bg--product-browse { background: var(--sec-product-browse); }
.lp-seg--product-detail { fill: var(--sec-product-detail); } .sec-bg--product-detail { background: var(--sec-product-detail); }
.lp-seg--samples { fill: var(--sec-samples); }               .sec-bg--samples { background: var(--sec-samples); }
.lp-seg--quote-lead { fill: var(--sec-quote-lead); }         .sec-bg--quote-lead { background: var(--sec-quote-lead); }
.lp-seg--where-to-buy { fill: var(--sec-where-to-buy); }     .sec-bg--where-to-buy { background: var(--sec-where-to-buy); }
.lp-seg--inspiration-content { fill: var(--sec-inspiration-content); } .sec-bg--inspiration-content { background: var(--sec-inspiration-content); }
.lp-seg--brand-story { fill: var(--sec-brand-story); }       .sec-bg--brand-story { background: var(--sec-brand-story); }
.lp-seg--template-unfilled { fill: var(--sec-template-unfilled); } .sec-bg--template-unfilled { background: var(--sec-template-unfilled); }
.lp-seg--off-brand-tracker { fill: var(--sec-off-brand-tracker); } .sec-bg--off-brand-tracker { background: var(--sec-off-brand-tracker); }
.lp-seg--off-brand-short { fill: var(--sec-off-brand-short); } .sec-bg--off-brand-short { background: var(--sec-off-brand-short); }
.lp-seg--off-brand-other { fill: var(--sec-off-brand-other); } .sec-bg--off-brand-other { background: var(--sec-off-brand-other); }
.lp-seg--unknown { fill: var(--sec-unknown); }               .sec-bg--unknown { background: var(--sec-unknown); }

/* Ads list */
.ad { display: grid; grid-template-columns: 116px 1fr 110px; gap: 12px; padding: 10px 0;
      border-bottom: 1px solid var(--border-1); font-size: 12px; align-items: start; }
.ad .id { font-family: var(--font-mono); color: var(--text-3); min-width: 0;
          overflow-wrap: anywhere; word-break: break-all; font-size: 11px; line-height: 1.5; }
.ad .body { color: var(--text-2); min-width: 0; overflow-wrap: anywhere; }
.ad .cta { text-align: right; font-weight: 600; color: var(--text-2); }
.ad .cta a { color: var(--accent); text-decoration: none; }
.ad .cta a:hover { text-decoration: underline; }

/* Briefing */
.briefing { white-space: pre-wrap; font-size: 13px; line-height: 1.6; color: var(--text-2);
            background: var(--bg-inset); border: 1px solid var(--border-1);
            border-radius: var(--radius-ctl); padding: 16px 18px; overflow-x: auto; }
/* Strategy-report link (replaces briefing when --strategy-doc is set) */
.strategy-cta { padding: 2px 0 4px; }
.strategy-links { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.strategy-btn { display: inline-block; padding: 10px 18px; border-radius: var(--radius-ctl);
                background: var(--accent); color: #fff; font-weight: 600; font-size: 13px;
                text-decoration: none; }
.strategy-btn:hover { filter: brightness(1.08); }
.strategy-btn.ghost { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
.strategy-btn.ghost:hover { background: var(--accent-soft); filter: none; }

/* Lightbox */
#lightbox { display: none; position: fixed; inset: 0; background: var(--scrim); z-index: 100;
            align-items: center; justify-content: center; cursor: zoom-out; }
#lightbox.open { display: flex; }
#lightbox img, #lightbox video { max-width: 92%; max-height: 92%; box-shadow: var(--shadow-pop);
                                 border-radius: 6px; }
#lightbox .meta-cta { position: absolute; bottom: 24px; right: 24px; background: var(--accent-strong);
                      color: var(--accent-contrast); padding: 10px 16px;
                      border-radius: var(--radius-ctl); font-size: 13px; font-weight: 600;
                      text-decoration: none; cursor: pointer; }
#lightbox .meta-cta:hover { background: var(--accent); }

/* Video thumbnail treatment — applied everywhere a creative renders. The
   wrapper element must have data-asset-type="video" or "video_evicted". */
.thumb-wrap { position: relative; display: block; }
.thumb-wrap[data-asset-type="video"]::after,
.thumb-wrap[data-asset-type="video_evicted"]::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 60 60'><circle cx='30' cy='30' r='26' fill='rgba(0,0,0,0.55)'/><polygon points='24,18 24,42 44,30' fill='white'/></svg>");
  background-repeat: no-repeat; background-position: center; background-size: 36px 36px;
}
.thumb-wrap .dur-chip {
  position: absolute; bottom: 4px; left: 4px; background: rgba(0,0,0,0.75);
  color: rgba(255,255,255,0.96); font-size: 10px; font-weight: 700; padding: 1px 5px;
  border-radius: 2px; letter-spacing: 0.3px; pointer-events: none;
}

/* Uploaded creative analytics — per-card metrics block + sort control. */
.perf-block {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(60px, 1fr));
  gap: 4px 8px; margin: 6px 0 2px; padding: 6px 8px;
  background: var(--accent-soft, rgba(91,140,255,0.10));
  border: 1px solid var(--border-1, #2a3140); border-radius: var(--radius-ctl, 6px);
}
.perf-stat { display: flex; flex-direction: column; line-height: 1.15; }
.perf-stat b { color: var(--accent, #5b8cff); font-size: 12px; font-weight: 800; }
.perf-stat .perf-lbl { color: var(--text-3, #8a93a3); font-size: 9.5px;
  text-transform: uppercase; letter-spacing: 0.3px; }
.perf-sort-label { color: var(--text-3, #8a93a3); font-size: 11px; margin-left: 12px; }
.perf-sort-btn {
  background: var(--bg-inset, #11151d); border: 1px solid var(--border-1, #2a3140);
  color: var(--text-2, #c7cdd9); border-radius: var(--radius-ctl, 6px);
  font-size: 11px; padding: 2px 8px; margin-left: 4px; cursor: pointer; font-family: inherit;
}
.perf-sort-btn.active { background: var(--accent, #5b8cff); color: var(--accent-contrast, #fff);
  border-color: var(--accent, #5b8cff); }

/* Filter UI — dropdown style */
.filter-bar { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px;
              margin: 10px 0 16px; }
.filter-dropdown { position: relative; }
.filter-dropdown > button { width: 100%; background: var(--bg-inset); border: 1px solid var(--border-1);
                            border-radius: var(--radius-ctl); padding: 8px 26px 8px 12px;
                            font-size: 12px; text-align: left; cursor: pointer; color: var(--text-1);
                            position: relative; transition: border-color .12s ease;
                            font-family: inherit; }
.filter-dropdown > button:hover { border-color: var(--border-2); }
.filter-dropdown.open > button { border-color: var(--accent); background: var(--bg-elev); }
.filter-dropdown > button .label { font-weight: 600; color: var(--text-2); font-size: 10.5px;
                                   text-transform: uppercase; letter-spacing: 0.6px; }
.filter-dropdown > button .badge { background: var(--accent); color: var(--accent-contrast);
                                   border-radius: var(--radius-pill); padding: 1px 7px;
                                   font-size: 10px; margin-left: 6px; font-weight: 700; }
.filter-dropdown > button::after { content: '▾'; position: absolute; right: 10px; top: 50%;
                                   transform: translateY(-50%); font-size: 10px; color: var(--text-3); }
.filter-dropdown.open > button::after { transform: translateY(-50%) rotate(180deg);
                                        color: var(--accent); }
.filter-menu { display: none; position: absolute; left: 0; right: 0; top: calc(100% + 4px);
               background: var(--bg-elev); border: 1px solid var(--border-2);
               border-radius: var(--radius-ctl); padding: 6px 0; box-shadow: var(--shadow-pop);
               z-index: 50; max-height: 320px; overflow-y: auto; min-width: 200px; }
.filter-dropdown.open .filter-menu { display: block; }
.filter-menu .menu-head { display: flex; justify-content: space-between; align-items: center;
                          padding: 4px 12px 6px; border-bottom: 1px solid var(--border-1);
                          margin-bottom: 4px; }
.filter-menu .menu-head .group-clear { background: none; border: none; color: var(--accent);
                                       cursor: pointer; font-size: 11px; padding: 0;
                                       font-family: inherit; }
.filter-menu .menu-head .group-clear:hover { text-decoration: underline; }
.filter-menu .menu-head .group-clear:disabled { color: var(--text-3); cursor: default;
                                                text-decoration: none; }
.filter-menu label { display: flex; align-items: center; gap: 8px; padding: 5px 12px;
                     font-size: 12px; cursor: pointer; user-select: none; color: var(--text-2); }
.filter-menu label:hover { background: var(--bg-inset); color: var(--text-1); }
.filter-menu label input { margin: 0; cursor: pointer; }
.filter-menu label .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filter-menu label .count { color: var(--text-3); font-size: 10px;
                            font-variant-numeric: tabular-nums; }
.filter-status { font-size: 12px; color: var(--text-2); padding: 6px 0; margin-bottom: 8px;
                 display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.filter-status button { background: var(--bg-inset); border: 1px solid var(--border-1);
                        border-radius: var(--radius-chip); padding: 5px 12px; font-size: 11px;
                        cursor: pointer; color: var(--text-2); font-family: inherit;
                        transition: color .12s ease, border-color .12s ease; }
.filter-status button:hover { border-color: var(--border-2); color: var(--text-1); }

/* Brand-vs-brand */
.bvb-controls { display: grid; grid-template-columns: 1fr 1fr auto; gap: 16px; margin: 12px 0 18px;
                align-items: end; }
.bvb-controls label { display: block; font-size: 10.5px; color: var(--text-3);
                      text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 5px;
                      font-weight: 700; }
.bvb-controls select { width: 100%; padding: 8px 10px; font-size: 13px;
                       border: 1px solid var(--border-1); border-radius: var(--radius-ctl);
                       background: var(--bg-inset); color: var(--text-1); font-family: inherit; }
.bvb-mode-radios { display: flex; gap: 12px; padding-bottom: 7px; }
.bvb-mode-radios label { display: flex; gap: 5px; align-items: center; cursor: pointer;
                         text-transform: none; letter-spacing: 0; font-size: 12.5px;
                         font-weight: 500; margin: 0; color: var(--text-2); }
.bvb-split { display: grid; grid-template-columns: 1fr 60px 1fr; gap: 14px; align-items: start; }
.bvb-col { background: var(--bg-elev); border: 1px solid var(--border-1);
           border-radius: var(--radius-ctl); padding: 16px; }
.bvb-col h3 { margin: 0 0 12px; font-size: 14px; color: var(--text-1); text-transform: none;
              letter-spacing: 0; }
.bvb-row { display: grid; grid-template-columns: 170px 1fr 44px; gap: 10px; font-size: 12px;
           padding: 3px 0; align-items: center; }
.bvb-row .val { background: var(--accent-soft); color: var(--accent); padding: 1px 6px;
                border-radius: var(--radius-chip); font-size: 11px; text-align: center;
                min-width: 36px; font-weight: 700; font-variant-numeric: tabular-nums; }
.bvb-row .lab { font-family: var(--font-mono); font-size: 11px; color: var(--text-2);
                min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bvb-track { background: var(--bg-inset); height: 8px; border-radius: 4px; overflow: hidden; }
.bvb-fill { height: 100%; background: var(--accent); border-radius: 4px; }
.bvb-vs { font-size: 12px; color: var(--text-3); text-align: center; padding-top: 40px;
          font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }
.bvb-attr-block { margin-bottom: 14px; }
.bvb-attr-block h4 { margin: 0 0 6px; font-size: 10.5px; text-transform: uppercase;
                     color: var(--text-3); letter-spacing: 0.8px; }

/* Delta view */
.delta-controls { display: flex; gap: 16px; align-items: center; margin: 10px 0 16px;
                  flex-wrap: wrap; }
.delta-controls label { font-size: 12px; color: var(--text-2); }
.delta-controls input[type=date] { padding: 7px 10px; font-size: 13px;
                                   border: 1px solid var(--border-1);
                                   border-radius: var(--radius-ctl); background: var(--bg-inset);
                                   color: var(--text-1); font-family: inherit; margin-left: 8px; }
.delta-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
               gap: 12px; margin-bottom: 16px; }
.delta-brand-bar { display: grid; grid-template-columns: 160px 1fr 50px; gap: 10px;
                   padding: 5px 0; font-size: 12px; align-items: center; }
.delta-brand-bar code { color: var(--text-2); }
.delta-brand-bar .bar { height: 12px; background: var(--bg-inset); border-radius: 6px;
                        overflow: hidden; }
.delta-brand-bar .bar > div { height: 100%; border-radius: 6px;
                              background: linear-gradient(90deg, var(--accent-strong), var(--accent)); }
.delta-brand-bar .n { text-align: right; font-weight: 700; font-family: var(--font-mono);
                      color: var(--text-1); }

/* Most-served snapshot strip */
.ms-head { font-size: 10.5px; font-weight: 700; letter-spacing: 1.1px; text-transform: uppercase;
           color: var(--text-3); margin-bottom: 12px; }
.ms-head .muted { text-transform: none; letter-spacing: 0; font-weight: 400; }
.ms-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
.ms-card { background: var(--bg-elev); border: 1px solid var(--border-1);
           border-radius: var(--radius-ctl); padding: 10px 12px; display: flex;
           flex-direction: column; gap: 6px; text-decoration: none; color: var(--text-1);
           min-width: 0; transition: border-color .15s ease; }
.ms-card:hover { border-color: var(--accent); }
.ms-card.empty { background: var(--bg-inset); }
.ms-name { font-size: 10.5px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase;
           color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ms-thumb { width: 100%; aspect-ratio: 1.4/1; object-fit: cover; border-radius: 5px;
            background: var(--bg-inset); display: block; }
.ms-ph { background: var(--bg-inset); color: var(--text-3); aspect-ratio: 1.4/1; border-radius: 5px;
         display: flex; align-items: center; justify-content: center; font-size: 16px; }
.ms-chips { display: flex; gap: 4px; flex-wrap: wrap; }
.ms-note { font-size: 10px; }

/* Chips (rank / days / active state) */
.chip { font-size: 10px; font-weight: 700; letter-spacing: 0.5px; padding: 2px 7px;
        border-radius: var(--radius-chip); display: inline-block; }
.chip--rank { background: var(--pos-soft); color: var(--pos); }
.chip--days { background: var(--accent-soft); color: var(--accent); }
.chip--active { background: var(--pos-soft); color: var(--pos); }
.chip--inactive { background: var(--neg-soft); color: var(--neg); }
.chip--neutral { background: var(--bg-inset); color: var(--text-3); }

/* Per-brand lanes (Meta / Homepage / Brand Store) */
.lane { margin-top: 28px; background: var(--bg-inset); border: 1px solid var(--border-1);
        border-left: 3px solid var(--accent); border-radius: var(--radius-ctl);
        padding: 18px 20px; }
.lane-meta { border-left-color: var(--accent); }
.lane-hp { border-left-color: var(--homepage); }
.lane-bs { border-left-color: var(--amazon); }
.lane-google { border-left-color: var(--google); }
.lane-tv { border-left-color: var(--tv); }
.lane-hp-blocked { border-left-color: var(--neg); }
.lane .stats { margin-bottom: 14px; }
.lane-label { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.lane-label h3 { margin: 0; font-size: 16px; text-transform: none; letter-spacing: 0;
                 color: var(--text-1); font-weight: 700; }
.lane-badge { font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
              padding: 3px 8px; border-radius: 4px; }
.lane-badge--meta { background: var(--accent-soft); color: var(--accent); }
.lane-badge--amazon { background: var(--amazon-soft); color: var(--amazon); }
.lane-badge--website { background: var(--homepage-soft); color: var(--homepage); }
.lane-badge--blocked { background: var(--neg-soft); color: var(--neg); }
.lane-badge--google { background: var(--google-soft); color: var(--google); }
.lane-badge--tv { background: var(--tv-soft); color: var(--tv); }
.lane h4 { margin: 18px 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
           color: var(--text-3); font-weight: 700; }
.lane h4 .muted { text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 11px; }

/* Google text-ads panel */
.ta-grid { display: flex; flex-direction: column; gap: 10px; }
.ta-card { background: var(--bg-card); border: 1px solid var(--border-1);
           border-radius: var(--radius-ctl); padding: 12px 14px; }
.ta-summary { font-weight: 700; color: var(--text-1); margin-bottom: 8px; font-size: 13px; }
.ta-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.ta-coltitle { font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
               color: var(--text-3); font-weight: 700; margin-bottom: 4px; }
.ta-list { margin: 0; padding-left: 16px; font-size: 12px; color: var(--text-2); line-height: 1.5; }
.ta-list li { margin: 3px 0; }

/* Top-served ads panel */
.ts-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
.ts-card { background: var(--bg-card); border: 1px solid var(--border-1);
           border-radius: var(--radius-ctl); padding: 10px; display: flex; flex-direction: column;
           gap: 8px; }
.ts-thumb { width: 100%; aspect-ratio: 1.4/1; object-fit: cover; border-radius: 5px;
            background: var(--bg-inset); display: block; }
.ts-chips { display: flex; gap: 4px; flex-wrap: wrap; }
.ts-body { font-size: 12.5px; line-height: 1.4; color: var(--text-2); flex: 1; }
.ts-cta { font-size: 11px; color: var(--text-3); }
.ts-cta a { color: var(--accent); text-decoration: none; font-weight: 600; }
.ts-id { font-size: 10px; color: var(--text-3); font-family: var(--font-mono); }

/* Homepage site-content analysis card */
.hp-card { background: var(--bg-card); border: 1px solid var(--border-1);
           border-radius: var(--radius-ctl); padding: 18px 22px; margin-bottom: 14px; }
.hp-card-head { display: flex; align-items: center; justify-content: space-between;
                margin-bottom: 10px; gap: 10px; }
.micro-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 1px;
               font-weight: 700; color: var(--text-3); }
.cache-note { color: var(--text-3); font-size: 11px; font-weight: 400; text-transform: none;
              letter-spacing: 0; }
.conf-pill { background: var(--pos-soft); color: var(--pos); font-size: 10px; font-weight: 700;
             padding: 2px 7px; border-radius: var(--radius-chip); letter-spacing: 0.5px;
             white-space: nowrap; }
.hp-oneliner { background: var(--homepage-soft); color: var(--homepage); font-style: italic;
               padding: 14px 18px; border-radius: var(--radius-chip); margin-bottom: 14px;
               line-height: 1.5; font-size: 14px; }
.offer-badge { display: inline-block; background: var(--warn-soft); color: var(--warn);
               padding: 4px 10px; border-radius: var(--radius-chip); font-weight: 600;
               font-size: 12px; margin-bottom: 8px; }
.hp-headline { font-size: 17px; font-weight: 700; color: var(--text-1); line-height: 1.3; }
.hp-subhead { margin-top: 3px; font-size: 13.5px; line-height: 1.45; color: var(--text-2); }
.hp-cta-row { margin-top: 8px; }
.cta-code { background: var(--accent-soft); color: var(--accent); padding: 3px 8px;
            border-radius: 4px; font-weight: 600; display: inline-block; margin-top: 4px; }
.cta-chip { background: var(--bg-inset); color: var(--text-2); padding: 2px 7px; border-radius: 4px;
            border: 1px solid var(--border-1); font-size: 11px; margin: 0 5px 5px 0;
            display: inline-block; }
.stance-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 14px; }
.stance-tag { background: var(--bg-elev); color: var(--text-1); font-size: 11px; font-weight: 600;
              padding: 3px 9px; border-radius: var(--radius-chip);
              border: 1px solid var(--border-1); }
.stance-tag .k { color: var(--text-3); font-weight: 500; }
.hp-divider { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border-1); }
.hp-divider .micro-label { display: block; margin-bottom: 6px; }
.hp-positioning { font-size: 13.5px; line-height: 1.55; color: var(--text-2); }
.hp-details { margin-top: 10px; }
.hp-details summary { cursor: pointer; font-size: 10.5px; text-transform: uppercase;
                      letter-spacing: 1px; font-weight: 700; color: var(--text-2); }
.hp-details-body { margin-top: 10px; }
.critique-row { display: grid; grid-template-columns: 130px 1fr; gap: 10px; padding: 6px 0;
                border-bottom: 1px solid var(--border-1); font-size: 13px; line-height: 1.5; }
.critique-row .k { font-weight: 600; color: var(--text-1); }
.critique-row .v { color: var(--text-2); }
.notable { margin-top: 8px; }
.notable-head { font-weight: 600; color: var(--text-1); font-size: 13px; }
.notable ul { margin: 4px 0 0 18px; padding: 0; font-size: 13px; color: var(--text-2); }
.notable li { margin: 4px 0; }
.ww-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.ww-head { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 700;
           margin-bottom: 6px; }
.ww-head.works { color: var(--pos); }
.ww-head.misses { color: var(--neg); }
.ww-grid ul { margin: 0 0 0 16px; padding: 0; font-size: 13px; color: var(--text-2);
              line-height: 1.5; }
.ww-grid li { margin: 5px 0; }
.hp-exp { margin-top: 6px; font-size: 12px; }
.hp-notes { font-size: 13px; color: var(--text-2); line-height: 1.55; }
.hp-notes strong { color: var(--text-1); }
.hp-attempt { font-size: 12px; margin-top: 8px; }
.mitigation { margin-top: 10px; font-size: 12px; }
.mitigation summary { cursor: pointer; color: var(--text-2); }
.mitigation ul { margin: 8px 0 0 18px; padding: 0; color: var(--text-2); line-height: 1.55; }

/* Responsive guards */
@media (max-width: 1180px) { .topbar-meta { display: none; } }
@media (max-width: 1100px) { .topbar-nav { display: none; } }
@media (max-width: 980px) {
  nav.sidebar { display: none; }
  main { margin-left: 0; }
  .bvb-split { grid-template-columns: 1fr; }
  .bvb-vs { padding: 6px 0; }
  .two-col { grid-template-columns: 1fr; }
}
"""

JS_V2 = """
// ---- theme toggle (dark default; persisted; pre-paint script in <head>) ----
(function setupThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.setAttribute('aria-pressed',
    document.documentElement.getAttribute('data-theme') === 'light' ? 'true' : 'false');
  btn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    btn.setAttribute('aria-pressed', next === 'light' ? 'true' : 'false');
    try { localStorage.setItem('intelDashTheme', next); } catch (e) {}
  });
})();

// ---- shared: read embedded JSON data ----
const DATA = JSON.parse(document.getElementById('intel-data').textContent);
const dashboardBase = location.pathname.replace(/\\/[^/]*$/, '/');
function relativize(absPath) {
  // The Python side embeds absolute fs paths; convert to relative for file:// browsing.
  // Strip the repo root prefix that's common to dashboard dir and image dir.
  const segs = location.pathname.split('/');
  // walk up to 'reports/<date>/dashboard' → repo root is 3 levels above this file
  const upToRepoRoot = segs.slice(0, -4).join('/') + '/';
  if (absPath.startsWith(upToRepoRoot)) {
    const rel = absPath.slice(upToRepoRoot.length);
    return '../../../' + rel;
  }
  return absPath;
}

// ---- lightbox ----
// Branches on data-asset-type. For 'video' creatives we know the mp4 exists
// locally — render an inline <video controls>. For 'video_evicted' (mp4
// deleted by the popularity prune) we show the first frame plus a "Watch on
// Meta Ad Library" CTA so the user can still see the ad in motion.
function _openLightbox({assetType, assetPath, thumbPath, metaUrl, adId}) {
  const lb = document.getElementById('lightbox');
  lb.innerHTML = '';
  if (assetType === 'video' && assetPath && assetPath.endsWith('.mp4')) {
    const v = document.createElement('video');
    v.src = assetPath;
    v.controls = true;
    v.autoplay = true;
    v.playsInline = true;
    v.poster = thumbPath || '';
    lb.appendChild(v);
  } else {
    const img = document.createElement('img');
    img.src = thumbPath || assetPath;
    img.alt = adId ? `ad ${adId}` : '';
    lb.appendChild(img);
    if ((assetType === 'video' || assetType === 'video_evicted') && metaUrl) {
      const a = document.createElement('a');
      a.className = 'meta-cta';
      a.href = metaUrl;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = 'Watch on Meta Ad Library →';
      lb.appendChild(a);
    }
  }
  lb.classList.add('open');
}

function bindLightbox(scope) {
  // Two click surfaces: legacy `.creative img` (image-only fallback) and the
  // new `.creative-link` wrapper which carries data-asset-* attributes.
  (scope || document).querySelectorAll('.creative-link').forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      _openLightbox({
        assetType: link.getAttribute('data-asset-type'),
        assetPath: relativize(link.getAttribute('data-asset-path')),
        thumbPath: relativize(link.getAttribute('data-thumb-path')),
        metaUrl: link.getAttribute('data-meta-url'),
        adId: link.getAttribute('data-ad-id'),
      });
    });
  });
  (scope || document).querySelectorAll('.creative > img').forEach(img => {
    // Plain images (no wrapper) — keep simple behavior.
    img.addEventListener('click', () => {
      _openLightbox({assetType: 'image', assetPath: null, thumbPath: img.src});
    });
  });
}
bindLightbox(document);
document.getElementById('lightbox').addEventListener('click', e => {
  // Click on the backdrop OR the close edge — but not on the controls / CTA.
  if (e.target.id === 'lightbox') {
    e.currentTarget.classList.remove('open');
    e.currentTarget.innerHTML = '';
  }
});

// ---- FILTER GALLERY ----
// Active filters: {group: Set(values)}. AND across groups, OR within a group.
const filterState = {
  comp: new Set(), photo: new Set(), prod: new Set(), emphasis: new Set(),
  hook: new Set(), appeal: new Set(), features: new Set(), products: new Set(),
  valueProps: new Set(),
  // Phase A1 extended taxonomy filters:
  context: new Set(), bgColor: new Set(), modelGender: new Set(),
  productInUse: new Set(), productGrouping: new Set(),
  certifications: new Set(), awards: new Set(), layoutFlags: new Set(),
  assetType: new Set(), platform: new Set(),
  // Landing-page filters (parsed from ad link_url at collect time):
  landingSection: new Set(), utmCampaign: new Set(),
  utmSource: new Set(), utmMedium: new Set(),
};

// Optional perf sort key ('sessions' | 'cvr' | null) — only when analytics
// have been uploaded. null preserves the default (filter-order) layout.
let _perfSort = null;
function _initPerfSort() {
  const host = document.getElementById('perf-sort');
  if (!host || !DATA.has_analytics) return;
  const opts = [['', 'Default'], ['sessions', 'Sessions'], ['cvr', 'Key-event rate'],
                ['bounceRate', 'Bounce rate'], ['engagementRate', 'Engagement']];
  host.innerHTML = '<span class="perf-sort-label">Sort by performance:</span> ' +
    opts.map(([k, l]) =>
      `<button class="perf-sort-btn${k === (_perfSort || '') ? ' active' : ''}" data-k="${k}">${l}</button>`
    ).join('');
  host.querySelectorAll('.perf-sort-btn').forEach(b => b.addEventListener('click', () => {
    _perfSort = b.dataset.k || null;
    _initPerfSort();
    renderFilterGallery();
  }));
}

function matchesFilters(c) {
  for (const [grp, set] of Object.entries(filterState)) {
    if (set.size === 0) continue;
    let val = c[grp];
    if (Array.isArray(val)) {
      // any-of match for list fields
      if (![...set].some(v => val.includes(v))) return false;
    } else {
      if (!set.has(val)) return false;
    }
  }
  return true;
}

// Format seconds → 'M:SS' (small helper used in both the gallery + delta view).
function _fmtDur(sec) {
  if (!sec) return '';
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// For video creatives, the thumbnail must be a static image — pick the first
// frame in the asset's directory. Mp4 paths follow the convention
// `.../{ad_id}/video.mp4`; the first frame is `.../{ad_id}/frame_00_t*.jpg`.
function _thumbForCreative(c) {
  // Server resolves the real first-frame into thumbPath (extraction timestamps
  // vary, so a client-side guess of frame_00_t00000.jpg 404s). Prefer it.
  if (c.thumbPath) return c.thumbPath;
  if ((c.assetType === 'video' || c.assetType === 'video_evicted')
      && c.imgPath && c.imgPath.endsWith('.mp4')) {
    return c.imgPath.replace(/video\\.mp4$/, 'frame_00_t00000.jpg');
  }
  return c.imgPath;
}

// Compact traffic/conversion chip from uploaded analytics. Renders nothing
// when no analytics are present for the creative (so non-Philo decks are clean).
function _fmtInt(n) {
  if (n === null || n === undefined || n === '') return '';
  return Math.round(Number(n)).toLocaleString('en-US');
}
function _pct(v) {
  if (v === null || v === undefined || v === '') return null;
  return (Number(v) * 100).toFixed(1) + '%';
}
function _perfStat(val, label) {
  return val === null ? '' : `<span class="perf-stat"><b>${val}</b><span class="perf-lbl">${label}</span></span>`;
}
function _perfChip(c) {
  const keys = ['sessions','cvr','keyEvents','bounceRate','engagementRate'];
  if (!keys.some(k => c[k] !== null && c[k] !== undefined && c[k] !== '')) return '';
  const stats = [
    _perfStat(c.sessions != null && c.sessions !== '' ? _fmtInt(c.sessions) : null, 'sessions'),
    _perfStat(_pct(c.cvr), 'key-event rate'),
    _perfStat(_pct(c.bounceRate), 'bounce'),
    _perfStat(_pct(c.engagementRate), 'engaged'),
    _perfStat(c.avgSessionDuration ? _fmtDur(c.avgSessionDuration) : null, 'avg time'),
  ].filter(Boolean).join('');
  if (!stats) return '';
  const seg = c.perfSegment ? ` title="GA4 segment: ${escapeHTML(c.perfSegment)}"` : '';
  return `<div class="perf-block"${seg}>${stats}</div>`;
}

function renderFilterGallery() {
  const container = document.getElementById('filter-gallery-grid');
  if (!container) return;
  let matched = DATA.client_creatives.filter(matchesFilters);
  if (DATA.has_analytics && _perfSort) {
    // Sort by the chosen metric desc; creatives without data sink to the bottom.
    const key = _perfSort;
    matched = matched.slice().sort((a, b) => (Number(b[key]) || -1) - (Number(a[key]) || -1));
  }
  container.innerHTML = matched.map(c => {
    const thumb = _thumbForCreative(c);
    const dur = _fmtDur(c.videoDurationSec);
    const at = c.assetType || 'image';
    return `
    <div class="creative" data-comp="${c.comp}">
      <a class="thumb-wrap creative-link" data-asset-type="${at}"
         data-asset-path="${c.imgPath}"
         data-thumb-path="${thumb}"
         data-meta-url="${c.metaAdUrl || ''}"
         data-ad-id="${c.adId}" style="display:block">
        <img src="${relativize(thumb)}" loading="lazy" alt="${c.adId}">
        ${dur ? `<span class="dur-chip">${dur}</span>` : ''}
      </a>
      <div class="body">
        <div class="summary">${escapeHTML(c.summary || '(no summary)')}</div>
        <div class="muted"><code>${escapeHTML(c.comp)}</code> · ad <code>${c.adId}</code></div>
        ${_perfChip(c)}
        <div class="tags">
          ${DATA.google_in_scope && c.platform ? `<span class="tag">${escapeHTML(c.platform)}</span>` : ''}
          ${c.photo ? `<span class="tag">${c.photo}</span>` : ''}
          ${c.hook ? `<span class="tag">${c.hook}</span>` : ''}
          ${(c.products || []).slice(0,2).map(p => `<span class="tag prod">${escapeHTML(p)}</span>`).join('')}
          ${(c.features || []).slice(0,3).map(f => `<span class="tag kf">${escapeHTML(f)}</span>`).join('')}
        </div>
      </div>
    </div>
  `;}).join('');
  document.getElementById('filter-status-count').textContent =
    `${matched.length} of ${DATA.client_creatives.length} creatives`;
  bindLightbox(container);
}

function buildFilterDropdowns() {
  const root = document.getElementById('filter-bar');
  if (!root) return;
  const groups = [
    {key: 'comp', label: 'Brand', getter: c => [c.comp]},
    // Platform (Meta/Google) filter — only in the with-Google report
    // (google_in_scope=false in the Meta build, so it never renders).
    ...(DATA.google_in_scope ? [{key: 'platform', label: 'Platform', getter: c => c.platform ? [c.platform] : []}] : []),
    {key: 'assetType', label: 'Asset type', getter: c => c.assetType ? [c.assetType] : []},
    {key: 'context', label: 'Source', getter: c => c.context ? [c.context] : []},
    {key: 'photo', label: 'Photography', getter: c => c.photo ? [c.photo] : []},
    {key: 'prod', label: 'Production', getter: c => c.prod ? [c.prod] : []},
    {key: 'emphasis', label: 'Emphasis', getter: c => c.emphasis ? [c.emphasis] : []},
    {key: 'hook', label: 'Hook style', getter: c => c.hook ? [c.hook] : []},
    {key: 'appeal', label: 'Appeal', getter: c => c.appeal ? [c.appeal] : []},
    {key: 'bgColor', label: 'Background color', getter: c => c.bgColor ? [c.bgColor] : []},
    {key: 'modelGender', label: 'Model gender', getter: c => c.modelGender ? [c.modelGender] : []},
    {key: 'productInUse', label: 'Product in use', getter: c => c.productInUse ? [c.productInUse] : []},
    {key: 'productGrouping', label: 'Product grouping', getter: c => c.productGrouping ? [c.productGrouping] : []},
    {key: 'features', label: 'Key features', getter: c => c.features || []},
    {key: 'layoutFlags', label: 'Layout flags', getter: c => c.layoutFlags || []},
    {key: 'products', label: 'Products', getter: c => c.products || []},
    {key: 'valueProps', label: 'Value props', getter: c => c.valueProps || []},
    {key: 'certifications', label: 'Certifications', getter: c => c.certifications || []},
    {key: 'awards', label: 'Awards / rankings', getter: c => c.awards || []},
    // Landing-page filters — parsed from each ad's link_url at collect time.
    // utmCampaign strings can be huge ("trex | +25mi | meta | ..."), so the
    // dropdown label truncates to 40 chars; the underlying value stays full.
    {key: 'landingSection', label: 'Landing section', getter: c => c.landingSection ? [c.landingSection] : []},
    {key: 'utmCampaign', label: 'UTM campaign', getter: c => c.utmCampaign ? [c.utmCampaign] : []},
    {key: 'utmSource', label: 'UTM source', getter: c => c.utmSource ? [c.utmSource] : []},
    {key: 'utmMedium', label: 'UTM medium', getter: c => c.utmMedium ? [c.utmMedium] : []},
  ];

  const populated = groups.map(g => {
    const counts = new Map();
    DATA.client_creatives.forEach(c => g.getter(c).forEach(v => {
      if (v !== null && v !== undefined && v !== '') counts.set(v, (counts.get(v) || 0) + 1);
    }));
    return {...g, items: [...counts.entries()].sort((a, b) => b[1] - a[1])};
  }).filter(g => g.items.length > 0);

  root.innerHTML = populated.map(g => `
    <div class="filter-dropdown" data-grp="${g.key}">
      <button type="button" aria-haspopup="true" aria-expanded="false">
        <span class="label">${escapeHTML(g.label)}</span><span class="badge" hidden>0</span>
      </button>
      <div class="filter-menu" role="menu">
        <div class="menu-head">
          <span class="muted" style="font-size:11px">${g.items.length} option(s)</span>
          <button type="button" class="group-clear" disabled>Clear</button>
        </div>
        ${g.items.map(([v, n]) => {
          // Truncate display label for noisy fields like utmCampaign — the
          // underlying data-val keeps the full string for matching.
          const display = (g.key === 'utmCampaign' && String(v).length > 40)
            ? String(v).slice(0, 37) + '…' : String(v);
          return `
          <label title="${escapeHTML(v)}">
            <input type="checkbox" data-grp="${g.key}" data-val="${escapeHTML(v)}">
            <span class="name">${escapeHTML(display)}</span>
            <span class="count">${n}</span>
          </label>`;
        }).join('')}
      </div>
    </div>
  `).join('');

  // Per-dropdown wiring.
  root.querySelectorAll('.filter-dropdown').forEach(dd => {
    const btn = dd.querySelector(':scope > button');
    const badge = btn.querySelector('.badge');
    const menu = dd.querySelector('.filter-menu');
    const clearBtn = menu.querySelector('.group-clear');
    const grpKey = dd.dataset.grp;

    const refreshBadge = () => {
      const n = filterState[grpKey].size;
      if (n > 0) { badge.hidden = false; badge.textContent = n; clearBtn.disabled = false; }
      else       { badge.hidden = true;                          clearBtn.disabled = true;  }
    };

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = dd.classList.contains('open');
      // Close any other open dropdowns first.
      root.querySelectorAll('.filter-dropdown.open').forEach(other => {
        other.classList.remove('open');
        other.querySelector(':scope > button').setAttribute('aria-expanded', 'false');
      });
      if (!wasOpen) {
        dd.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
      }
    });

    menu.addEventListener('click', (e) => e.stopPropagation());

    menu.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () => {
        const set = filterState[grpKey];
        if (cb.checked) set.add(cb.dataset.val);
        else            set.delete(cb.dataset.val);
        refreshBadge();
        renderFilterGallery();
      });
    });

    clearBtn.addEventListener('click', () => {
      filterState[grpKey].clear();
      menu.querySelectorAll('input[type=checkbox]:checked').forEach(cb => { cb.checked = false; });
      refreshBadge();
      renderFilterGallery();
    });
  });

  // Click outside closes all menus.
  document.addEventListener('click', () => {
    root.querySelectorAll('.filter-dropdown.open').forEach(dd => {
      dd.classList.remove('open');
      dd.querySelector(':scope > button').setAttribute('aria-expanded', 'false');
    });
  });
}

document.getElementById('clear-filters')?.addEventListener('click', () => {
  for (const k of Object.keys(filterState)) filterState[k].clear();
  document.querySelectorAll('#filter-bar .filter-dropdown').forEach(dd => {
    dd.querySelectorAll('input[type=checkbox]:checked').forEach(cb => { cb.checked = false; });
    const badge = dd.querySelector(':scope > button .badge');
    if (badge) badge.hidden = true;
    const clr = dd.querySelector('.group-clear');
    if (clr) clr.disabled = true;
  });
  renderFilterGallery();
});

buildFilterDropdowns();
_initPerfSort();
renderFilterGallery();

// ---- BRAND vs BRAND ----
function populateBrandSelectors() {
  const sels = ['bvb-a', 'bvb-b'];
  const brandIds = Object.keys(DATA.client_tallies).sort();
  const brandLabel = id => (DATA.brands_meta?.[id] || id);
  for (const id of sels) {
    const sel = document.getElementById(id);
    if (!sel) continue;
    sel.innerHTML = brandIds.map(b => `<option value="${b}">${b}</option>`).join('');
  }
  if (brandIds.length >= 2) {
    document.getElementById('bvb-a').value = brandIds[0];
    document.getElementById('bvb-b').value = brandIds[1];
  }
}
function topValuesUnion(attr, kind, a, b) {
  const out = new Map();
  for (const t of [a, b]) {
    const c = t[kind][attr] || {};
    for (const [v, n] of Object.entries(c)) {
      out.set(v, (out.get(v) || 0) + n);
    }
  }
  return [...out.entries()].sort((x,y) => y[1] - x[1]).slice(0, 6).map(e => e[0]);
}
function renderBvbCol(brandId, tally, peerTally) {
  const attrs = [
    {key: 'photography_style', label: 'Photography', kind: 'scalar'},
    {key: 'production_style', label: 'Production', kind: 'scalar'},
    {key: 'product_emphasis', label: 'Emphasis', kind: 'scalar'},
    {key: 'hook_style', label: 'Hook', kind: 'scalar'},
    {key: 'emotional_vs_rational', label: 'Appeal', kind: 'scalar'},
    {key: 'value_props', label: 'Value props', kind: 'listed'},
    {key: 'key_features', label: 'Key features', kind: 'listed'},
    {key: 'products_visible', label: 'Products', kind: 'listed'},
  ];
  let html = `<h3>${brandId} <span class="muted">(n=${tally.n})</span></h3>`;
  for (const a of attrs) {
    const vals = topValuesUnion(a.key, a.kind, tally, peerTally);
    if (vals.length === 0) continue;
    html += `<div class="bvb-attr-block"><h4>${a.label}</h4>`;
    for (const v of vals) {
      const c = (tally[a.kind][a.key] || {})[v] || 0;
      const share = tally.n ? Math.round(100 * c / tally.n) : 0;
      html += `<div class="bvb-row">
        <span class="lab">${escapeHTML(v)}</span>
        <div class="bvb-track">
          <div class="bvb-fill" style="width:${share}%"></div>
        </div>
        <span class="val">${share}%</span>
      </div>`;
    }
    html += '</div>';
  }
  return html;
}
function renderBvb() {
  const a = document.getElementById('bvb-a').value;
  const b = document.getElementById('bvb-b').value;
  // 'By count' uses all analyzed creatives (Meta + homepage + brand-store).
  // 'By popularity' weights each Meta-ad creative by its popularity_score and
  // drops standalone creatives — see _collect for the rationale.
  const mode = document.querySelector('input[name="bvb-mode"]:checked')?.value || 'count';
  const src = (mode === 'popularity')
    ? (DATA.client_tallies_weighted || DATA.client_tallies)
    : DATA.client_tallies;
  const tA = src[a], tB = src[b];
  if (!tA || !tB) {
    document.getElementById('bvb-left').innerHTML =
      `<p class="muted">No data for this brand in the selected view.</p>`;
    document.getElementById('bvb-right').innerHTML = '';
    return;
  }
  document.getElementById('bvb-left').innerHTML = renderBvbCol(a, tA, tB);
  document.getElementById('bvb-right').innerHTML = renderBvbCol(b, tB, tA);
}
populateBrandSelectors();
document.getElementById('bvb-a')?.addEventListener('change', renderBvb);
document.getElementById('bvb-b')?.addEventListener('change', renderBvb);
document.querySelectorAll('input[name="bvb-mode"]').forEach(r =>
  r.addEventListener('change', renderBvb)
);
renderBvb();

// ---- LANDING-PAGES: count/popularity toggle ----
// Each <rect> in the lp-bar SVG carries data-count-x / -w and data-pop-x / -w
// attributes populated server-side; the toggle just swaps which pair is read.
// Same for table-row share cells — data-count-share / -pop-share both present,
// the active one is shown.
function applyLandingPagesMode(mode) {
  const useCount = (mode !== 'popularity');
  document.querySelectorAll('.lp-bar rect').forEach(rect => {
    const x = useCount ? rect.dataset.countX : rect.dataset.popX;
    const w = useCount ? rect.dataset.countW : rect.dataset.popW;
    if (x !== undefined) rect.setAttribute('x', x);
    if (w !== undefined) rect.setAttribute('width', w);
  });
  document.querySelectorAll('.lp-table tbody tr').forEach(tr => {
    const cell = tr.querySelector('td[data-count-share]');
    if (!cell) return;
    const s = parseFloat(useCount ? cell.dataset.countShare : cell.dataset.popShare) || 0;
    cell.textContent = Math.round(s * 100) + '%';
  });
  const hdr = document.querySelector('.lp-share-header');
  if (hdr) hdr.textContent = useCount ? 'Share' : 'Pop. share';
}
document.querySelectorAll('input[name="lp-mode"]').forEach(r =>
  r.addEventListener('change', e => applyLandingPagesMode(e.target.value))
);

// ---- DELTA VIEW: new since X ----
function renderDelta() {
  const dateInput = document.getElementById('delta-date');
  if (!dateInput) return;
  const since = dateInput.value;  // YYYY-MM-DD
  const sinceIso = since + 'T00:00:00';
  const newAds = DATA.client_ads.filter(a => a.firstSeen >= sinceIso);
  const newCreatives = DATA.client_creatives.filter(c => c.firstSeen >= sinceIso);
  // counts
  document.getElementById('delta-stats').innerHTML = `
    <div class="stat"><div class="label">New ads since ${since}</div><div class="value">${newAds.length}</div></div>
    <div class="stat"><div class="label">New creatives</div><div class="value">${newCreatives.length}</div></div>
    <div class="stat"><div class="label">Brands w/ activity</div><div class="value">${new Set(newAds.map(a => a.comp)).size}</div></div>
  `;
  // per-brand breakdown bars
  const perBrand = new Map();
  for (const a of newAds) perBrand.set(a.comp, (perBrand.get(a.comp) || 0) + 1);
  const maxN = Math.max(...perBrand.values(), 1);
  const sorted = [...perBrand.entries()].sort((a,b) => b[1] - a[1]);
  document.getElementById('delta-brand-list').innerHTML = sorted.map(([cid, n]) => `
    <div class="delta-brand-bar">
      <code>${cid}</code>
      <div class="bar"><div style="width:${Math.round(100*n/maxN)}%"></div></div>
      <span class="n">${n}</span>
    </div>
  `).join('') || '<p class="muted">No activity in window.</p>';
  // new-creative gallery (capped)
  const gal = document.getElementById('delta-gallery');
  gal.innerHTML = newCreatives.slice(0, 24).map(c => `
    <div class="creative">
      <img src="${relativize(c.imgPath)}" loading="lazy">
      <div class="body">
        <div class="summary">${escapeHTML(c.summary || '')}</div>
        <div class="muted"><code>${escapeHTML(c.comp)}</code> · ${c.firstSeen.slice(0,10)}</div>
      </div>
    </div>
  `).join('') || '<p class="muted">No new creatives in window.</p>';
  bindLightbox(gal);
}
document.getElementById('delta-date')?.addEventListener('change', renderDelta);
renderDelta();

// ---- nav active-section highlight ----
// v2 has two navs (left sidebar + top-bar pills); both are <nav> elements, so
// one section id can map to multiple links — highlight all of them.
(function setupNavObserver() {
  const navLinks = new Map();
  document.querySelectorAll('nav a[href^="#"]').forEach(a => {
    const id = a.getAttribute('href').slice(1);
    if (!navLinks.has(id)) navLinks.set(id, []);
    navLinks.get(id).push(a);
  });
  if (navLinks.size === 0) return;
  const observer = new IntersectionObserver(entries => {
    // Pick the section with the largest visible area near the top.
    let best = null, bestRatio = 0;
    entries.forEach(e => {
      if (e.isIntersecting && e.intersectionRatio > bestRatio) {
        best = e.target.id; bestRatio = e.intersectionRatio;
      }
    });
    if (best && navLinks.has(best)) {
      document.querySelectorAll('nav a.active').forEach(a => a.classList.remove('active'));
      navLinks.get(best).forEach(a => a.classList.add('active'));
    }
  }, { rootMargin: '-15% 0px -60% 0px', threshold: [0.1, 0.5, 1] });
  navLinks.forEach((_, id) => {
    const el = document.getElementById(id);
    if (el) observer.observe(el);
  });
})();

// ---- helpers ----
function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
"""


# ----- render helpers --------------------------------------------------------

_KNOWN_SECTIONS = set(SECTION_LABELS.keys())


def _sec_class(sec: str) -> str:
    """Landing-section key → CSS class suffix. Unknown keys fall back to the
    'unknown' palette var (mirrors v1's SECTION_PALETTE.get(sec, grey))."""
    key = sec if sec in _KNOWN_SECTIONS else "unknown"
    return key.replace("_", "-")


def _chip(text: str, kind: str = "neutral") -> str:
    return f'<span class="chip chip--{kind}">{_esc(text)}</span>'


def _stat(label: str, value: Any, *, chip: str = "", small: bool = False) -> str:
    """One KPI card. `value` may carry markup (chips), so the caller escapes."""
    cls = "value sm" if small else "value"
    return (f'<div class="stat"><div class="label">{_esc(label)}</div>'
            f'<div class="{cls}">{value}{chip}</div></div>')


_SUN_SVG = (
    '<svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2'
    'M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
)
_MOON_SVG = (
    '<svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
)


def _render_top_bar(org_name: str, product_name: str, generated_at: str,
                    days: int, n_brands: int, strategy_doc: str | None = None) -> str:
    initial = (org_name or "I").strip()[:1].upper()
    report_label = "Strategy" if strategy_doc else "Briefing"
    return f"""
<div class="topbar">
  <div class="topbar-left">
    <div class="logo-mark">{_esc(initial)}</div>
    <div class="topbar-title">
      <div class="org">{_esc(org_name)}</div>
      <div class="product">{_esc(product_name)}</div>
    </div>
  </div>
  <nav class="topbar-nav">
    <a href="#overview">Overview</a>
    <a href="#delta">What's new</a>
    <a href="#browse">Browse</a>
    <a href="#briefing">{report_label}</a>
  </nav>
  <div class="topbar-right">
    <div class="topbar-meta">Generated {_esc(generated_at)} · last {days}d · {n_brands} brands</div>
    <button id="theme-toggle" type="button" aria-label="Toggle light / dark theme"
            aria-pressed="false" title="Toggle light / dark theme">{_SUN_SVG}{_MOON_SVG}</button>
  </div>
</div>
"""


def _render_sidebar(nav_groups: list[tuple[str | None, list[tuple[str, str]]]]) -> str:
    parts = []
    for group_label, items in nav_groups:
        if group_label:
            parts.append(f'<div class="nav-group">{_esc(group_label)}</div>')
        for label, href in items:
            parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
    return f'<nav class="sidebar">{"".join(parts)}</nav>'


# ----- section renderers ------------------------------------------------------

def _render_most_served_snapshot_v2(brands: list, top_ads_by_brand: dict,
                                    dashboard_dir: Path) -> str:
    """Snapshot strip: the rank-1 ad per brand. First card row inside <main>."""
    cards = []
    for b in brands:
        cid = b["id"]
        top = top_ads_by_brand.get(cid) or []
        first = top[0] if top else None
        if not first or first.get("popularity_score", 0) <= 0:
            cards.append(f"""
            <a href="#brand-{_esc(cid)}" class="ms-card empty">
              <div class="ms-name">{_esc(b['name'])}</div>
              <div class="ms-ph">—</div>
              <div class="muted ms-note">no ranked ads</div>
            </a>
            """)
            continue
        asset_type = first.get("thumb_asset_type") or "image"
        vmeta = _video_meta_for_render(first.get("thumb_analysis_json"))
        dur_chip = (
            f'<span class="dur-chip">{_format_duration(vmeta.get("duration_sec"))}</span>'
            if vmeta.get("duration_sec") else ''
        )
        if first.get("thumb_path"):
            thumb_rel = _thumb_src(first["thumb_path"], asset_type, dashboard_dir)
            thumb_html = (
                f'<span class="thumb-wrap" data-asset-type="{_esc(asset_type)}">'
                f'<img class="ms-thumb" src="{thumb_rel}" loading="lazy" alt="top served ad">'
                f'{dur_chip}'
                f'</span>'
            )
        else:
            thumb_html = '<div class="ms-ph">no thumbnail</div>'
        rank = first.get("serp_position_rank")
        rank_label = f"#{int(rank)+1}" if rank is not None else "—"
        days_run = int(first.get("run_days") or 0)
        cards.append(f"""
        <a href="#brand-{_esc(cid)}" class="ms-card">
          <div class="ms-name">{_esc(b['name'])}</div>
          {thumb_html}
          <div class="ms-chips">{_chip(f"RANK {rank_label}", "rank")}{_chip(f"{days_run}d", "days")}</div>
        </a>
        """)
    return f"""
    <section id="most-served" class="ms-section">
      <div class="ms-head">Most-served snapshot
        <span class="muted"> — each brand's top-scoring ad by popularity proxy (SERP rank × run duration, intra-brand only)</span>
      </div>
      <div class="ms-grid">{''.join(cards)}</div>
    </section>
    """


def _render_stats_v2(data: dict) -> str:
    n_brands = len(data["brands"])
    n_ads = sum(b["ads_total"] for b in data["brands"])
    n_active = sum(b["ads_active"] for b in data["brands"])
    n_creatives = sum(b["creatives_total"] for b in data["brands"])
    n_analyzed = sum(b["creatives_analyzed"] for b in data["brands"])
    n_new = sum(b["new_ads"] for b in data["brands"])
    ads_chip = (f'<span class="stat-chip up">▲ {n_new} new</span>' if n_new > 0 else "")
    cov_chip = (f'<span class="stat-chip neutral">{n_analyzed / n_creatives:.0%}</span>'
                if n_creatives else "")
    return f"""
    <div class="stats">
      {_stat("Brands tracked", n_brands)}
      {_stat("Ads (total)", n_ads, chip=ads_chip)}
      {_stat("Active ads", n_active)}
      {_stat("Creatives stored", n_creatives)}
      {_stat("Creatives analyzed", n_analyzed, chip=cov_chip)}
      {_stat(f"New ads ({data['window_days']}d)", n_new)}
    </div>
    """


def _render_brand_cards_v2(data: dict) -> str:
    items = []
    for b in data["brands"]:
        pri = b["priority"] or "medium"
        items.append(f"""
        <div class="brand-card">
          <div class="brand-card-head">
            <a href="#brand-{_esc(b['id'])}">{_esc(b['name'])}</a>
            <span class="vertical"><span class="pri-dot pri-{_esc(pri)}"
                  title="priority {_esc(pri)}"></span>{_esc(b['vertical'])}</span>
          </div>
          <dl>
            <dt>Ads (total / active)</dt><dd>{b['ads_total']} / {b['ads_active']}</dd>
            <dt>New in {data['window_days']}d</dt><dd>{b['new_ads']}</dd>
            <dt>Creatives (analyzed)</dt><dd>{b['creatives_total']} ({b['creatives_analyzed']})</dd>
            <dt>Top CTA</dt><dd>{_esc(b['top_cta'] or '—')}</dd>
          </dl>
        </div>
        """)
    return f'<div class="brand-grid">{"".join(items)}</div>'


def _render_heatmap_table_v2(attr_label: str, comp_tallies: dict, set_tally,
                             *, attr_kind: str) -> str:
    """attr_kind: 'scalar' or 'listed'. Cells emit a --share CSS var (0..100)
    instead of a baked background color so they re-theme via color-mix."""
    counter = (set_tally.scalar if attr_kind == "scalar" else set_tally.listed).get(attr_label)
    if not counter:
        return ""
    top_vals = [v for v, _ in counter.most_common(7)]
    head = "<th>brand</th>" + "".join(f"<th><code>{_esc(v)}</code></th>" for v in top_vals) + "<th>n</th>"
    body_rows = []
    for cid in sorted(comp_tallies.keys()):
        t = comp_tallies[cid]
        cells = []
        for v in top_vals:
            c = (t.scalar if attr_kind == "scalar" else t.listed).get(attr_label, Counter()).get(v, 0)
            share = c / t.n_total if t.n_total else 0
            hi = " hi" if share > 0.55 else ""
            cells.append(f'<td class="heat{hi}" style="--share:{share * 100:.1f}">{share:.0%}</td>')
        body_rows.append(
            f'<tr><td class="brand-label">{_esc(cid)}</td>{"".join(cells)}'
            f'<td class="heat">{t.n_total}</td></tr>'
        )
    # set totals row — no --share vars; tr.set-row styling wins instead
    set_cells = []
    for v in top_vals:
        c = (set_tally.scalar if attr_kind == "scalar" else set_tally.listed).get(attr_label, Counter()).get(v, 0)
        share = c / set_tally.n_total if set_tally.n_total else 0
        set_cells.append(f'<td class="heat">{share:.0%}</td>')
    body_rows.append(
        f'<tr class="set-row"><td>set</td>{"".join(set_cells)}<td class="heat">{set_tally.n_total}</td></tr>'
    )
    return f"<h3>{_esc(attr_label)}</h3><table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_distinctiveness_v2(data: dict) -> str:
    rows = []
    for row in data["distinct"]:
        rows.append(f"""
        <div class="distinct-row">
          <code>{_esc(row['brand'])}</code>
          <span>{_esc(row['attribute'])}</span>
          <span><code>{_esc(row['value'])}</code> &nbsp;
            <span class="muted">brand {row['brand_share']:.0%} vs set {row['set_share']:.0%}</span></span>
          <span class="delta">+{row['delta']:.0%}</span>
        </div>
        """)
    if not rows:
        return '<p class="muted">No strong distinctiveness signals yet — need more analyzed creatives.</p>'
    return "".join(rows)


def _render_landing_screenshot_v2(tu: dict, dashboard_dir: Path) -> str:
    """Thumbnail + one-line read + expandable analysis for a captured landing
    page (v2, theme-var styled — reuses the homepage card's detail classes)."""
    rel = _relpath(Path(tu["screenshot_path"]), dashboard_dir)
    a = tu.get("analysis") or {}
    parts = [
        f'<a class="landing-tile" href="{rel}" target="_blank" '
        f'title="landing page — click to open full"><img src="{rel}" alt="landing page"></a>'
    ]
    summary = _esc(tu.get("summary") or "")
    if summary:
        parts.append(f'<div class="muted" style="margin-top:6px;font-style:italic">{summary}</div>')
    rows = []
    for label, key in [("Page intent", "page_intent"), ("Primary CTA", "primary_cta"),
                       ("Offer", "offer"), ("Message match", "message_match")]:
        v = a.get(key)
        if v:
            rows.append(f'<div class="critique-row"><span class="k">{_esc(label)}</span>'
                        f'<span class="v">{_esc(v)}</span></div>')
    for label, key in [("Trust", "trust_signals"), ("Friction", "friction_points")]:
        items = a.get(key) or []
        if items:
            rows.append(f'<div class="critique-row"><span class="k">{_esc(label)}</span>'
                        f'<span class="v">{_esc(", ".join(items[:4]))}</span></div>')

    def _ww(label, key, cls):
        items = a.get(key) or []
        if not items:
            return ""
        lis = "".join(f"<li>{_esc(x)}</li>" for x in items[:3])
        return f'<div><div class="ww-head {cls}">{label}</div><ul>{lis}</ul></div>'
    works = _ww("What works", "what_works", "works")
    misses = _ww("What it misses", "what_misses", "misses")
    ww = f'<div class="ww-grid">{works}{misses}</div>' if (works or misses) else ""
    body = "".join(rows)
    if body or ww:
        parts.append(
            f'<details class="hp-details"><summary>Landing-page analysis</summary>'
            f'<div class="hp-details-body">{body}{ww}</div></details>'
        )
    return f'<div style="margin-top:8px">{"".join(parts)}</div>'


def _render_landing_pages_section_v2(data: dict, dashboard_dir: Path) -> str:
    """Per-brand 'where ads send traffic' breakdown — stacked horizontal bar +
    drill-down table. The Count/Popularity toggle hot-swaps section widths in
    JS using data attributes set on each <rect>; server-side default is Count
    mode. Segment/legend/pill colors come from per-theme --sec-* CSS vars."""
    landing_by_brand = data.get("landing_by_brand") or {}
    if not landing_by_brand:
        return '<p class="muted">No ad link_urls collected yet — run ingest.</p>'

    toolbar = """
    <div class="lp-toolbar">
      <span>Weight bars by</span>
      <span class="lp-mode">
        <label><input type="radio" name="lp-mode" value="count" checked>
          <span>Ad count</span></label>
        <label><input type="radio" name="lp-mode" value="popularity">
          <span>Popularity-weighted</span></label>
      </span>
      <span class="muted">Popularity = SERP rank × duration × active bonus per ad. Same intra-brand caveats as other ranked views.</span>
    </div>
    """

    blocks = [toolbar]
    for brand in data["brands"]:
        cid = brand["id"]
        payload = landing_by_brand.get(cid) or {}
        total = payload.get("total_ads", 0)
        with_link = payload.get("with_link", 0)
        on_brand_share = payload.get("on_brand_share", 0.0)
        by_section = payload.get("by_section") or []

        head = (
            f'<div class="lp-brand-head">'
            f'<div class="lp-brand">{_esc(brand["name"])}</div>'
            f'<div class="lp-stats">{with_link} of {total} ads · '
            f'{on_brand_share:.0%} on-brand'
            f'</div></div>'
        )

        if not by_section:
            blocks.append(
                f'<div class="lp-brand-card empty">{head}'
                f'<div class="muted">no link_urls captured in this window — '
                f'either no paid ads or a scraper coverage gap.</div></div>'
            )
            continue

        # SVG stacked horizontal bar. Both shares baked into data attrs so the
        # JS toggle can swap widths without re-rendering. Initial x/width use
        # ad_share (count mode). Fill comes from the per-theme CSS class.
        viewbox_w = 1000
        rects = []
        legend_items = []
        x_count = 0.0
        x_pop = 0.0
        for s in by_section:
            sec = s["section"]
            sec_cls = _sec_class(sec)
            label = SECTION_LABELS.get(sec, sec)
            count_w = s["ad_share"] * viewbox_w
            pop_w = s["popularity_share"] * viewbox_w
            rects.append(
                f'<rect class="lp-seg lp-seg--{sec_cls}" data-section="{_esc(sec)}" '
                f'data-count-x="{x_count:.2f}" data-count-w="{count_w:.2f}" '
                f'data-pop-x="{x_pop:.2f}" data-pop-w="{pop_w:.2f}" '
                f'x="{x_count:.2f}" y="0" width="{count_w:.2f}" height="22">'
                f'<title>{_esc(label)} — {s["ad_count"]} ads '
                f'({s["ad_share"]:.0%} of links · {s["popularity_share"]:.0%} pop-weighted)</title>'
                f'</rect>'
            )
            legend_items.append(
                f'<span class="lp-legend-item">'
                f'<span class="lp-legend-swatch sec-bg--{sec_cls}"></span>'
                f'{_esc(label)}</span>'
            )
            x_count += count_w
            x_pop += pop_w
        bar_svg = (
            f'<svg class="lp-bar" viewBox="0 0 {viewbox_w} 22" '
            f'preserveAspectRatio="none" aria-label="landing section breakdown">'
            f'{"".join(rects)}</svg>'
        )
        legend_html = f'<div class="lp-legend">{"".join(legend_items)}</div>'

        # Drill-down table — one row per section, matching bar order. Show the
        # top destination URL for each section.
        rows_html = []
        for s in by_section:
            sec = s["section"]
            sec_cls = _sec_class(sec)
            label = SECTION_LABELS.get(sec, sec)
            flagged_cls = " flagged" if sec in ("template_unfilled", "off_brand_tracker", "off_brand_short", "off_brand_other") else ""
            top_urls = s.get("top_urls") or []
            top_url_html = ""
            screenshot_html = ""
            if top_urls:
                tu = top_urls[0]
                href = _esc(tu.get("clean_url") or "")
                top_url_html = f'<a href="{href}" target="_blank" rel="noopener">{href}</a>'
                if tu.get("screenshot_path"):
                    screenshot_html = _render_landing_screenshot_v2(tu, dashboard_dir)
            elif s.get("example_raw_urls"):
                # Template-unfilled URLs have no clean_url — show the raw form.
                ex = _esc(s["example_raw_urls"][0])[:140]
                top_url_html = f'<span class="muted">{ex}</span>'
            rows_html.append(
                f'<tr class="lp-row{flagged_cls}" data-section="{_esc(sec)}">'
                f'<td class="section">'
                f'<span class="lp-section-pill sec-bg--{sec_cls}"></span>'
                f'{_esc(label)}</td>'
                f'<td class="num">{s["ad_count"]}</td>'
                f'<td class="num" data-count-share="{s["ad_share"]:.4f}" '
                f'data-pop-share="{s["popularity_share"]:.4f}">'
                f'{s["ad_share"]:.0%}</td>'
                f'<td class="url">{top_url_html}{screenshot_html}</td>'
                f'</tr>'
            )
        table_html = f"""
        <table class="lp-table">
          <thead>
            <tr><th>Section</th><th class="num">Ads</th>
                <th class="num lp-share-header">Share</th>
                <th>Top destination</th></tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
        """

        # Flagged findings callouts — one per flagged section with ads.
        findings_html = ""
        for s in by_section:
            sec = s["section"]
            if sec in SECTION_CALLOUTS and s["ad_count"] > 0:
                cls = " danger" if sec.startswith("off_brand") else ""
                callout = SECTION_CALLOUTS[sec]
                # Translate `code` markdown into <code> tags.
                callout_html = callout.replace("`", "@@CODE@@")
                parts = callout_html.split("@@CODE@@")
                rendered = parts[0]
                for i, part in enumerate(parts[1:], start=1):
                    rendered += (f"<code>{_esc(part)}</code>" if i % 2 == 1 else _esc(part))
                findings_html += (
                    f'<div class="lp-findings{cls}">'
                    f'<strong>{s["ad_count"]} ads</strong> in '
                    f'<em>{_esc(SECTION_LABELS.get(sec, sec))}</em> — {rendered}'
                    f'</div>'
                )

        blocks.append(
            f'<div class="lp-brand-card" data-brand="{_esc(cid)}">'
            f'{head}{bar_svg}{legend_html}{table_html}{findings_html}'
            f'</div>'
        )

    return "".join(blocks)


def _render_whitespace_v2(data: dict) -> str:
    blocks = []
    for cid, gaps in data["whitespace"].items():
        if not gaps:
            continue
        items = []
        for g in gaps[:6]:
            items.append(
                f'<div class="ws-item">{_esc(g["attribute"])} = '
                f'<code>{_esc(g["value"])}</code> — used by {g["set_share"]:.0%} of set</div>'
            )
        blocks.append(f"""
        <div class="ws">
          <div class="ws-brand">{_esc(cid)}</div>
          {''.join(items)}
        </div>
        """)
    if not blocks:
        return '<p class="muted">No whitespace gaps detected yet.</p>'
    return "".join(blocks)


def _render_brand_store_block_v2(bs_data: dict | None, dashboard_dir: Path) -> str:
    """Per-brand 'Amazon Brand Store' lane. Empty string if no activity."""
    if not bs_data:
        return ""
    screenshot_html = ""
    if bs_data.get("latest_screenshot"):
        rel = _relpath(Path(bs_data["latest_screenshot"]), dashboard_dir)
        screenshot_html = (
            f'<a class="landing-tile" href="{rel}" target="_blank" '
            f'title="brand store landing — click to open full"><img src="{rel}" '
            f'alt="brand store landing"></a>'
        )
    creatives = bs_data.get("creatives") or []
    cre_html_parts = []
    for cre in creatives[:12]:
        rel = _relpath(Path(cre["asset_path"]), dashboard_dir)
        cap = _esc((cre.get("summary") or "")[:80])
        cre_html_parts.append(
            f'<div class="bs-thumb"><img src="{rel}" loading="lazy" alt="brand store image">'
            f'<div class="muted">{cap}</div></div>'
        )
    cre_html = (
        f'<div class="thumb-grid">{"".join(cre_html_parts)}</div>'
    ) if cre_html_parts else (
        '<p class="muted">Images captured but not yet analyzed. Run '
        '<code>intel analyze-creatives</code>.</p>'
    )
    observed = bs_data.get("latest_observed_at") or "—"
    return f"""
    <div class="lane lane-bs">
      <div class="lane-label">
        <span class="lane-badge lane-badge--amazon">Amazon</span>
        <h3>Brand Store</h3>
      </div>
      <div class="stats">
        {_stat("Pages captured", bs_data.get('pages_count', 0))}
        {_stat("Images captured", bs_data.get('image_count_total', 0))}
        {_stat("Analyzed", f"{bs_data.get('analyzed_count', 0)}/{len(creatives)}")}
        {_stat("Last captured", _esc(observed[:16].replace('T', ' ')), small=True)}
      </div>
      <div class="two-col">
        <div>{screenshot_html}</div>
        <div>{cre_html}</div>
      </div>
    </div>
    """


def _render_homepage_block_v2(hp_data: dict | None, dashboard_dir: Path) -> str:
    """Per-brand 'Website / Homepage' lane. Green accent; blocked state shows a
    clear red BLOCKED banner instead of pretending the captcha is the page."""
    if not hp_data:
        return ""

    if hp_data.get("blocked"):
        vendor = (hp_data.get("block_vendor") or "unknown").replace("_", " ").title()
        observed = (hp_data.get("latest_observed_at") or "—")[:16].replace("T", " ")
        return f"""
        <div class="lane lane-hp lane-hp-blocked">
          <div class="lane-label">
            <span class="lane-badge lane-badge--blocked">Website</span>
            <h3>Homepage — blocked</h3>
          </div>
          <div class="hp-notes">
            <strong>Anti-bot wall detected:</strong> <code>{_esc(vendor)}</code>.
            The scraper hit a captcha / human-verification screen instead of the brand's homepage.
            <em>No images or promo data extracted — this brand is excluded from analysis until we can route around the block.</em>
          </div>
          <div class="muted hp-attempt">Last attempted: {_esc(observed)}</div>
          <details class="mitigation">
            <summary>Mitigation options</summary>
            <ul>
              <li><strong>Residential proxies</strong> — Bright Data / Smartproxy / Oxylabs rotating IPs (~$30–80/mo for low volume). Captcha walls discriminate against datacenter IPs first.</li>
              <li><strong>Managed scraping browser</strong> — Bright Data Scraping Browser, ScrapingBee, Browserless. Drop-in Playwright endpoint with built-in fingerprint randomization and captcha solving (~$50–200/mo).</li>
              <li><strong>undetected-playwright-python</strong> — community fingerprint-spoofing patch on top of Playwright. Free, works against many walls (not all).</li>
              <li><strong>One-time cookie capture</strong> — pass the captcha manually once in a headed browser, save cookies, reuse until they expire (days to weeks).</li>
              <li><strong>Fall back to an unblocked surface</strong> — Meta Ads Library (we already have it) or the brand's email creative + RSS, instead of scraping the homepage.</li>
            </ul>
          </details>
        </div>
        """
    screenshot_html = ""
    if hp_data.get("screenshot_path"):
        rel = _relpath(Path(hp_data["screenshot_path"]), dashboard_dir)
        screenshot_html = (
            f'<a class="landing-tile" href="{rel}" target="_blank" '
            f'title="homepage landing — click to open full"><img src="{rel}" '
            f'alt="homepage landing"></a>'
        )
    observed = hp_data.get("latest_observed_at") or "—"

    # Site Content Analysis card — surfaces the structured homepage_promos record
    # PLUS the richer raw_json fields (positioning_statement, messaging_stance,
    # design_critique, what_works/misses, strategist_one_liner) when available.
    promo = hp_data.get("latest_promo") or {}
    raw = (promo.get("raw") or {}) if promo else {}
    promo_html = ""
    if promo:
        head = _esc(promo.get("headline") or "")
        sub = _esc(promo.get("subhead") or "")
        cta = _esc(promo.get("primary_cta_text") or "")
        claim = _esc(promo.get("offer_claim") or "")
        exp = _esc(promo.get("expiration") or "")
        confidence = promo.get("confidence")
        cache = ' <span class="cache-note">(cached)</span>' if promo.get("cache_hit") else ""
        conf_pill = (f'<span class="conf-pill">CONFIDENCE {int(round(confidence * 100))}%</span>'
                     if isinstance(confidence, (int, float)) else "")
        offer_badge = f'<div class="offer-badge">{claim}</div>' if claim else ""

        # Strategist one-liner (top of card, distinct treatment)
        one_liner = _esc(raw.get("strategist_one_liner") or "")
        one_liner_html = f'<div class="hp-oneliner">{one_liner}</div>' if one_liner else ""

        # Messaging stance tag row
        stance = raw.get("messaging_stance") or {}
        stance_tags = []
        for label_key, value in [
            ("appeal", stance.get("rational_vs_emotional")),
            ("signal", stance.get("value_vs_premium_signaling")),
            ("leads with", stance.get("lead_with")),
            ("tone", stance.get("tone_one_word")),
        ]:
            if value:
                stance_tags.append(
                    f'<span class="stance-tag"><span class="k">{label_key}:</span> '
                    f'{_esc(value)}</span>')
        stance_html = (f'<div class="stance-tags">{"".join(stance_tags)}</div>'
                       if stance_tags else "")

        # CTAs — primary + secondary
        primary_cta_html = (f'<div class="hp-cta-row"><span class="micro-label">Primary CTA</span><br>'
                            f'<code class="cta-code">{cta}</code></div>') if cta else ""
        secondary_ctas = raw.get("secondary_ctas") or []
        secondary_html = ""
        if secondary_ctas:
            chips = "".join(
                f'<code class="cta-chip">{_esc(s)}</code>'
                for s in secondary_ctas[:8]
            )
            secondary_html = (f'<div class="hp-cta-row"><span class="micro-label">'
                              f'Secondary CTAs ({len(secondary_ctas)})</span>'
                              f'<div style="margin-top:5px">{chips}</div></div>')

        # Positioning + design critique (expandable)
        positioning = _esc(raw.get("positioning_statement") or "")
        positioning_html = (f'<div class="hp-divider">'
                            f'<span class="micro-label">Positioning read</span>'
                            f'<div class="hp-positioning">{positioning}</div></div>') if positioning else ""

        design = raw.get("design_critique") or {}
        design_html = ""
        if design:
            critique_rows = []
            for label, key in [
                ("Hero layout", "hero_layout"),
                ("Visual consistency", "visual_consistency"),
                ("CTA clarity", "cta_clarity"),
                ("Imagery", "use_of_imagery"),
            ]:
                v = design.get(key)
                if v:
                    critique_rows.append(
                        f'<div class="critique-row"><span class="k">{label}</span>'
                        f'<span class="v">{_esc(v)}</span></div>')
            notable = design.get("notable_design_choices") or []
            notable_html = ""
            if notable:
                items = "".join(f'<li>{_esc(n)}</li>' for n in notable)
                notable_html = (f'<div class="notable"><span class="notable-head">Notable choices</span>'
                                f'<ul>{items}</ul></div>')
            design_html = (f'<details class="hp-details"><summary>Design critique</summary>'
                           f'<div class="hp-details-body">{"".join(critique_rows)}'
                           f'{notable_html}</div></details>')

        # What works / What misses (two-column)
        works = raw.get("what_this_homepage_does_well") or []
        misses = raw.get("what_it_misses") or []
        ww_html = ""
        if works or misses:
            works_html = "".join(f'<li>{_esc(w)}</li>' for w in works[:5])
            misses_html = "".join(f'<li>{_esc(m)}</li>' for m in misses[:5])
            ww_html = f"""
            <div class="hp-divider">
              <div class="ww-grid">
                <div>
                  <div class="ww-head works">What works</div>
                  <ul>{works_html}</ul>
                </div>
                <div>
                  <div class="ww-head misses">What it misses</div>
                  <ul>{misses_html}</ul>
                </div>
              </div>
            </div>
            """

        exp_html = f'<div class="muted hp-exp">expires: {exp}</div>' if exp else ""
        promo_html = f"""
        <div class="hp-card">
          <div class="hp-card-head">
            <div class="micro-label">Site Content Analysis (latest){cache}</div>
            {conf_pill}
          </div>
          {one_liner_html}
          {offer_badge}
          <div class="hp-headline">{head}</div>
          <div class="hp-subhead">{sub}</div>
          {primary_cta_html}
          {secondary_html}
          {stance_html}
          {positioning_html}
          {design_html}
          {ww_html}
          {exp_html}
        </div>
        """

    return f"""
    <div class="lane lane-hp">
      <div class="lane-label">
        <span class="lane-badge lane-badge--website">Website</span>
        <h3>Homepage</h3>
      </div>
      {promo_html}
      <div class="stats">
        {_stat("Last captured", _esc(observed[:16].replace('T', ' ')), small=True)}
      </div>
      <div>{screenshot_html}</div>
    </div>
    """


def _render_top_served_panel_v2(top_ads: list, dashboard_dir: Path) -> str:
    """Per-brand 'Top served ads' panel. See storage.popularity_score for the
    underlying signal. Intra-brand only — don't infer cross-brand ranking."""
    if not top_ads:
        return (
            '<p class="muted">No served-rank data yet. '
            'Re-run <code>intel ingest</code> to populate.</p>'
        )
    cards = []
    for ad in top_ads:
        body = (ad.get("body_text") or "").replace("\n", " ")[:140]
        cta = ad.get("cta_type") or "—"
        link = ad.get("link_url")
        rank = ad.get("serp_position_rank")
        rank_chip = (_chip(f"RANK #{int(rank)+1}", "rank") if rank is not None
                     else _chip("RANK —", "neutral"))
        days_chip = (_chip(f"{int(ad['run_days'])}d RUNNING", "days")
                     if ad.get("run_days") else "")
        active_chip = (_chip("ACTIVE", "active") if (ad.get("active") or 0)
                       else _chip("INACTIVE", "inactive"))
        thumb_html = ''
        asset_type = ad.get("thumb_asset_type") or "image"
        vmeta = _video_meta_for_render(ad.get("thumb_analysis_json"))
        dur_chip = (
            f'<span class="dur-chip">{_format_duration(vmeta.get("duration_sec"))}</span>'
            if vmeta.get("duration_sec") else ''
        )
        if ad.get("thumb_path"):
            thumb_rel = _thumb_src(ad["thumb_path"], asset_type, dashboard_dir)
            asset_rel = _relpath(Path(ad["thumb_path"]), dashboard_dir)
            mau = _meta_ad_url(ad.get("ad_archive_id"))
            thumb_html = (
                f'<a class="thumb-wrap creative-link" data-asset-type="{_esc(asset_type)}" '
                f'data-asset-path="{_esc(asset_rel)}" '
                f'data-thumb-path="{_esc(thumb_rel)}" '
                f'data-meta-url="{_esc(mau)}" '
                f'data-ad-id="{_esc(ad["ad_archive_id"])}">'
                f'<img class="ts-thumb" src="{thumb_rel}" loading="lazy" '
                f'alt="ad {_esc(ad["ad_archive_id"])}">'
                f'{dur_chip}'
                f'</a>'
            )
        cta_html = (
            f'<a href="{_esc(link)}" target="_blank">{_esc(cta)}</a>'
            if link else _esc(cta)
        )
        cards.append(f"""
        <div class="ts-card">
          {thumb_html}
          <div class="ts-chips">{rank_chip}{days_chip}{active_chip}</div>
          <div class="ts-body">{_esc(body)}</div>
          <div class="ts-cta"><span class="muted">CTA</span> {cta_html}</div>
          <div class="ts-id">{_esc(ad['ad_archive_id'])}</div>
        </div>
        """)
    return f'<div class="ts-grid">{"".join(cards)}</div>'


def _render_text_ads_panel_v2(text_ads: list, dashboard_dir: Path) -> str:
    """v2 Google text-ads cards — headlines (Titles) and descriptions (Body copy)
    with per-component classification chips + an ad-level rollup. Themed with
    var() tokens / existing classes only (no hardcoded color)."""
    if not text_ads:
        return ""
    cards = []
    for t in text_ads[:24]:
        h_rows = []
        for h in (t.get("headlines") or [])[:15]:
            if not isinstance(h, dict):
                h = {"text": h}
            chips = []
            if h.get("intent"):
                chips.append(f'<span class="tag">{_esc(h["intent"])}</span>')
            if h.get("sale_status") and h["sale_status"] not in (None, "no_sale", "unclear"):
                chips.append(f'<span class="tag kf">{_esc(h["sale_status"])}</span>')
            if h.get("urgency"):
                chips.append('<span class="tag kf">urgency</span>')
            h_rows.append(f'<li>{_esc(h.get("text", ""))} {"".join(chips)}</li>')
        d_rows = []
        for d in (t.get("descriptions") or [])[:6]:
            if not isinstance(d, dict):
                d = {"text": d}
            chips = []
            if d.get("copy_lean") and d["copy_lean"] != "none":
                chips.append(f'<span class="tag">{_esc(d["copy_lean"])}</span>')
            for vp in (d.get("value_props") or [])[:3]:
                chips.append(f'<span class="tag prod">{_esc(vp)}</span>')
            if d.get("urgency"):
                chips.append('<span class="tag kf">urgency</span>')
            d_rows.append(f'<li>{_esc(d.get("text", ""))} {"".join(chips)}</li>')
        rollup = []
        for label, val in [("sale", t.get("sale_status")), ("offer", t.get("offer_kind")),
                           ("hook", t.get("hook_style")), ("appeal", t.get("emotional_vs_rational"))]:
            if val and val not in ("none", "no_sale", "unclear"):
                rollup.append(f'<span class="tag">{_esc(label)}: {_esc(val)}</span>')
        if t.get("offer_value"):
            rollup.append(f'<span class="tag kf">{_esc(t["offer_value"])}</span>')
        link = t.get("link_url")
        link_html = f' · <a href="{_esc(link)}" target="_blank">landing</a>' if link else ""
        unanalyzed = "" if t.get("analyzed") else ' <span class="muted">(unanalyzed)</span>'
        cards.append(f"""
        <div class="ta-card">
          <div class="ta-summary">{_esc(t.get("summary") or "(text ad)")}{unanalyzed}</div>
          <div class="ta-cols">
            <div>
              <div class="ta-coltitle">Titles</div>
              <ul class="ta-list">{"".join(h_rows) or '<li class="muted">—</li>'}</ul>
            </div>
            <div>
              <div class="ta-coltitle">Body copy</div>
              <ul class="ta-list">{"".join(d_rows) or '<li class="muted">—</li>'}</ul>
            </div>
          </div>
          <div class="tags">{"".join(rollup)}</div>
          <div class="muted">ad <code>{_esc(t.get("ad_archive_id"))}</code>{link_html}</div>
        </div>""")
    return f'<div class="ta-grid">{"".join(cards)}</div>'


def _render_brand_section_v2(brand: dict, recs: list, recent_ads: list,
                             dashboard_dir: Path, *, days: int,
                             bs_data: dict | None = None,
                             hp_data: dict | None = None,
                             top_ads: list | None = None,
                             text_ads: list | None = None,
                             tv_ads: list | None = None,
                             tv_metrics: dict | None = None,
                             analytics_by_ad: dict | None = None) -> str:
    analytics_by_ad = analytics_by_ad or {}
    # Creative gallery
    gallery_items = []
    for rec in recs[:36]:
        a = rec.analysis
        asset_type = rec.asset_type or "image"
        asset_rel = _relpath(Path(rec.asset_path), dashboard_dir)
        thumb_rel = _thumb_src(rec.asset_path, asset_type, dashboard_dir)
        vmeta = a.get("video_meta") or {}
        dur_chip = (
            f'<span class="dur-chip">{_format_duration(vmeta.get("duration_sec"))}</span>'
            if vmeta.get("duration_sec") else ''
        )
        mau = _meta_ad_url(rec.ad_archive_id)
        summary = _esc(a.get("summary_one_line") or "")
        tags_html = []
        if getattr(rec, "source", "meta") == "google":
            tags_html.append('<span class="tag">google</span>')
        ps = a.get("photography_style")
        if ps:
            tags_html.append(f'<span class="tag">{_esc(ps)}</span>')
        hs = a.get("hook_style")
        if hs:
            tags_html.append(f'<span class="tag">{_esc(hs)}</span>')
        for p in (a.get("products_visible") or [])[:2]:
            tags_html.append(f'<span class="tag prod">{_esc(p)}</span>')
        for f in (a.get("key_features") or [])[:3]:
            tags_html.append(f'<span class="tag kf">{_esc(f)}</span>')
        gallery_items.append(f"""
        <div class="creative">
          <span class="thumb-wrap creative-link" data-asset-type="{_esc(asset_type)}"
                data-asset-path="{_esc(asset_rel)}"
                data-thumb-path="{_esc(thumb_rel)}"
                data-meta-url="{_esc(mau)}"
                data-ad-id="{_esc(rec.ad_archive_id)}">
            <img src="{thumb_rel}" loading="lazy" alt="ad {rec.ad_archive_id}">
            {dur_chip}
          </span>
          <div class="body">
            <div class="summary">{summary}</div>
            <div class="muted">ad <code>{_esc(rec.ad_archive_id)}</code></div>
            {_perf_block_html(analytics_by_ad.get(rec.ad_id))}
            <div class="tags">{''.join(tags_html)}</div>
          </div>
        </div>
        """)
    if not gallery_items:
        gallery_html = ('<p class="muted">No analyzed creatives. Run '
                        '<code>intel analyze-creatives --competitor '
                        + _esc(brand['id']) + '</code>.</p>')
    else:
        gallery_html = f'<div class="gallery">{"".join(gallery_items)}</div>'

    # Recent ads list
    ads_items = []
    for ad in recent_ads[:15]:
        body = (ad.get("body_text") or "").replace("\n", " ")[:160]
        link = ad.get("link_url")
        cta = ad.get("cta_type") or "—"
        cta_html = f'<a href="{_esc(link)}" target="_blank">{_esc(cta)}</a>' if link else _esc(cta)
        ads_items.append(f"""
        <div class="ad">
          <span class="id">{_esc(ad['ad_archive_id'])}<br><span class="muted">{_esc(ad['first_seen'][:10])}</span></span>
          <span class="body">{_esc(body)}</span>
          <span class="cta">{cta_html}</span>
        </div>
        """)
    if not ads_items:
        ads_html = '<p class="muted">No ads in window.</p>'
    else:
        ads_html = "".join(ads_items)

    pri = brand["priority"] or "medium"
    brand_store_html = _render_brand_store_block_v2(bs_data, dashboard_dir)
    homepage_html = _render_homepage_block_v2(hp_data, dashboard_dir)
    top_served_html = _render_top_served_panel_v2(top_ads or [], dashboard_dir)
    # Google text-ads lane — only when this brand has Google text ads.
    text_ads_panel = _render_text_ads_panel_v2(text_ads or [], dashboard_dir)
    google_lane_html = ""
    if text_ads_panel:
        google_lane_html = f"""
      <div class="lane lane-google">
        <div class="lane-label">
          <span class="lane-badge lane-badge--google">Google</span>
          <h3>Text ads</h3>
          <span class="muted">Transparency Center · classified by title vs. body copy</span>
        </div>
        <div class="stats">
          {_stat("Google ads", brand.get('ads_google', 0))}
          {_stat("Text ads shown", len(text_ads or []))}
        </div>
        {text_ads_panel}
      </div>"""
    tv_spots_panel = _render_tv_spots_panel(tv_ads or [], dashboard_dir)
    tv_lane_html = ""
    if tv_spots_panel:
        m = tv_metrics or {}
        spend_rank = ('#' + str(m['spend_rank'])) if m.get('spend_rank') is not None else _fmt_metric(None)
        tv_lane_html = f"""
      <div class="lane lane-tv">
        <div class="lane-label">
          <span class="lane-badge lane-badge--tv">TV</span>
          <h3>TV ads</h3>
          <span class="muted">iSpot.tv · national linear + streaming spots</span>
        </div>
        <div class="stats">
          {_stat("TV spots shown", len(tv_ads or []))}
          {_stat("National airings", _fmt_metric(m.get('national_airings')))}
          {_stat("Total creatives", _fmt_metric(m.get('total_creatives')))}
          {_stat("Spend rank", spend_rank, small=True)}
        </div>
        {tv_spots_panel}
      </div>"""
    return f"""
    <section id="brand-{_esc(brand['id'])}">
      <h2>{_esc(brand['name'])} <span class="muted">— {_esc(brand['vertical'])} · priority {_esc(pri)}</span></h2>

      <div class="lane lane-meta">
        <div class="lane-label">
          <span class="lane-badge lane-badge--meta">Meta</span>
          <h3>Ads Library</h3>
        </div>
        <div class="stats">
          {_stat("Active ads", brand['ads_active'])}
          {_stat("New in window", brand['new_ads'])}
          {_stat("Ad creatives analyzed", f"{brand['creatives_analyzed']}/{brand['creatives_total']}")}
          {_stat("Top CTA", _esc(brand['top_cta'] or '—'), small=True)}
        </div>
        <h4>Top served ads
          <span class="muted"> — ranked by Meta's "most served" sort × run duration (intra-brand only, proxy not raw impressions)</span>
        </h4>
        {top_served_html}
        <h4>Creative gallery</h4>
        {gallery_html}
        <h4>Recent ads (last {days} days)</h4>
        {ads_html}
      </div>
      {google_lane_html}
      {tv_lane_html}
      {homepage_html}
      {brand_store_html}
    </section>
    """


def _render_briefing_v2(data: dict) -> str:
    b = data["latest_briefing"]
    if not b:
        return '<p class="muted">No briefings yet. Run <code>intel brief</code>.</p>'
    # render markdown crudely (we want zero external deps for the HTML)
    body = b["body_md"]
    return f"""
    <div class="muted">{_esc(b['title'])} — generated {_esc(b['created_at'])} (briefing id {b['id']})</div>
    <pre class="briefing">{_esc(body)}</pre>
    """


def _render_strategy_link_v2(strategy_doc: str) -> str:
    """When --strategy-doc is set, the 'Latest briefing' section links to that
    standalone strategy report (+ its sibling PDF) instead of the briefing body."""
    pdf = (strategy_doc[:-5] + ".pdf") if strategy_doc.lower().endswith(".html") else (strategy_doc + ".pdf")
    return f"""
    <div class="strategy-cta">
      <p class="muted">The full strategist's read for this set — positioning, competitive tone, and the whitespace worth moving on — is a standalone report.</p>
      <div class="strategy-links">
        <a class="strategy-btn" href="{_esc(strategy_doc)}" target="_blank" rel="noopener">Open strategy report &rarr;</a>
        <a class="strategy-btn ghost" href="{_esc(pdf)}" target="_blank" rel="noopener">Download PDF</a>
      </div>
    </div>
    """


# ----- page assembly ----------------------------------------------------------

def build_dashboard_v2(
    out_dir: Path | str,
    *,
    days: int = 30,
    org_name: str = DEFAULT_ORG,
    product_name: str = DEFAULT_PRODUCT_V2,
    brand_ids: set[str] | None = None,
    sources: set[str] | None = None,
    strategy_doc: str | None = None,
) -> dict[str, Any]:
    """Generate the v2 dashboard at out_dir/index.html. Returns summary metadata.

    Same data, sections, section ids, and interactive features as v1 —
    redesigned chrome and theming only (plus the weighted brand-vs-brand
    tallies, which v1 collects but forgets to embed).

    brand_ids restricts the dashboard to an allow-list of competitor ids (see
    dashboard._collect); None renders every competitor. sources scopes ad
    platforms ({"meta"} for the unchanged Meta report, {"meta","google"} for
    the with-Google report); None means all platforms."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        data = _collect(conn, days=days, brand_ids=brand_ids, sources=sources)

    sections_html = []

    # Overview
    sections_html.append(f"""
    <section id="overview">
      <h2>Overview</h2>
      {_render_stats_v2(data)}
      <h3>Brands</h3>
      {_render_brand_cards_v2(data)}
    </section>
    """)

    # Delta view (what's new since X)
    default_since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    sections_html.append(f"""
    <section id="delta">
      <h2>What's new since…</h2>
      <p class="muted">Filters ads and analyzed creatives whose first_seen date is on or after the picked date. Default: 7 days ago.</p>
      <div class="delta-controls">
        <label>Show ads first seen on or after:
          <input type="date" id="delta-date" value="{default_since}">
        </label>
      </div>
      <div class="delta-stats stats" id="delta-stats"></div>
      <h3>New ads by brand</h3>
      <div id="delta-brand-list"></div>
      <h3>New creatives (first 24)</h3>
      <div class="gallery" id="delta-gallery"></div>
    </section>
    """)

    # Cross-set comparison — scalar attrs + list attrs (heatmaps)
    cross_html = []
    for attr in _SCALAR_ATTRS:
        cross_html.append(_render_heatmap_table_v2(attr, data["comp_tallies"], data["set_tally"], attr_kind="scalar"))
    for attr in _LIST_ATTRS:
        cross_html.append(_render_heatmap_table_v2(attr, data["comp_tallies"], data["set_tally"], attr_kind="listed"))
    sections_html.append(f"""
    <section id="comparison">
      <h2>Cross-competitor creative comparison</h2>
      <p class="muted">{data['set_tally'].n_total} analyzed creatives. Cell color = share of brand's creatives with that attribute value (darker = higher).</p>
      {''.join(cross_html)}
    </section>
    """)

    # Brand-vs-brand
    sections_html.append("""
    <section id="bvb">
      <h2>Brand vs Brand</h2>
      <p class="muted">Pick any two brands to see their attribute distributions side by side. Bars show % of each brand's analyzed creatives with that value.</p>
      <div class="bvb-controls">
        <div><label>Brand A</label><select id="bvb-a"></select></div>
        <div><label>Brand B</label><select id="bvb-b"></select></div>
        <div class="bvb-mode">
          <label>Weight</label>
          <div class="bvb-mode-radios">
            <label><input type="radio" name="bvb-mode" value="count" checked> By count</label>
            <label><input type="radio" name="bvb-mode" value="popularity"> By popularity <span class="muted">(Meta ads only)</span></label>
          </div>
        </div>
      </div>
      <div class="bvb-split">
        <div class="bvb-col" id="bvb-left"></div>
        <div class="bvb-vs">vs</div>
        <div class="bvb-col" id="bvb-right"></div>
      </div>
    </section>
    """)

    # Distinctiveness
    sections_html.append(f"""
    <section id="distinctiveness">
      <h2>Distinctiveness — where each brand over-indexes vs the set</h2>
      {_render_distinctiveness_v2(data)}
    </section>
    """)

    # Whitespace
    sections_html.append(f"""
    <section id="whitespace">
      <h2>Whitespace — what each brand is NOT using that the set is</h2>
      {_render_whitespace_v2(data)}
    </section>
    """)

    # Landing pages — "where ads send traffic"
    sections_html.append(f"""
    <section id="landing-pages">
      <h2>Where ads send traffic</h2>
      <p class="muted">Per-brand breakdown of ad <code>link_url</code> destinations. Sections in <span class="flag-red">red</span> / <span class="flag-orange">orange</span> are flagged findings, not traffic worth applauding — measurement redirects (DoubleClick), Dynamic Creative templates that never resolved, or opaque short-links.</p>
      {_render_landing_pages_section_v2(data, out_dir)}
    </section>
    """)

    # Filterable all-brand gallery
    sections_html.append("""
    <section id="browse">
      <h2>Browse all creatives</h2>
      <p class="muted">Open a dropdown to select values. Multiple values within a group are OR; across groups are AND.</p>
      <div class="filter-bar" id="filter-bar"></div>
      <div class="filter-status">
        <span id="filter-status-count"></span>
        <button id="clear-filters">Clear all filters</button>
        <span id="perf-sort"></span>
      </div>
      <div class="gallery" id="filter-gallery-grid"></div>
    </section>
    """)

    # Per-brand
    for brand in data["brands"]:
        recs = data["by_comp_recs"].get(brand["id"], [])
        recent = data["recent_ads"].get(brand["id"], [])
        top = data["top_ads"].get(brand["id"], [])
        bs_data = data["brand_store_by_brand"].get(brand["id"])
        hp_data = data["homepage_by_brand"].get(brand["id"])
        sections_html.append(
            _render_brand_section_v2(brand, recs, recent, out_dir, days=days,
                                     bs_data=bs_data, hp_data=hp_data, top_ads=top,
                                     text_ads=data["text_ads_by_brand"].get(brand["id"], []),
                                     tv_ads=data["tv_ads_by_brand"].get(brand["id"], []),
                                     tv_metrics=data["tv_metrics_by_brand"].get(brand["id"], {}),
                                     analytics_by_ad=data.get("analytics_by_ad", {}))
        )

    # Reporting: --strategy-doc link replaces the latest-briefing body when set.
    if strategy_doc:
        sections_html.append(f"""
    <section id="briefing">
      <h2>Strategy report</h2>
      {_render_strategy_link_v2(strategy_doc)}
    </section>
    """)
    else:
        sections_html.append(f"""
    <section id="briefing">
      <h2>Latest briefing</h2>
      {_render_briefing_v2(data)}
    </section>
    """)

    # Sidebar nav: grouped sections. Same groups/hrefs as v1.
    nav_groups: list[tuple[str | None, list[tuple[str, str]]]] = [
        (None, [('Overview', '#overview')]),
        ('Analysis', [
            ("What's new", '#delta'),
            ('Comparison', '#comparison'),
            ('Brand vs Brand', '#bvb'),
            ('Distinctiveness', '#distinctiveness'),
            ('Whitespace', '#whitespace'),
            ('Where ads send traffic', '#landing-pages'),
            ('Browse all creatives', '#browse'),
        ]),
        ('Brands', [(brand["name"], f"#brand-{brand['id']}") for brand in data["brands"]]),
        ('Reporting', [('Strategy report' if strategy_doc else 'Latest briefing', '#briefing')]),
    ]
    sidebar_html = _render_sidebar(nav_groups)
    topbar_html = _render_top_bar(org_name, product_name, data["generated_at"],
                                  days, len(data["brands"]), strategy_doc=strategy_doc)

    # ---- embedded JSON payload for client-side features ----
    # Includes client_tallies_weighted (v1 collects it but omits it from the
    # payload, which silently downgraded the BvB "By popularity" toggle).
    brands_meta = {b["id"]: b["name"] for b in data["brands"]}
    payload = {
        "client_creatives": data["client_creatives"],
        "client_ads": data["client_ads"],
        "client_tallies": data["client_tallies"],
        "client_tallies_weighted": data["client_tallies_weighted"],
        "client_set_tally": data["client_set_tally"],
        "google_in_scope": data["google_in_scope"],
        "has_analytics": data.get("has_analytics", False),
        "brands_meta": brands_meta,
        "window_days": days,
    }
    # `</` never appears in JSON syntax itself, only inside string values —
    # escaping it keeps a literal '</script>' in any summary from ending the tag.
    payload_json = json.dumps(payload, default=str).replace("</", "<\\/")

    # Space-joined so the default reads "Horizon Commerce Intelligence".
    page_title = f"{org_name} {product_name}"
    page = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{_esc(page_title)} — {data['generated_at'][:10]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script>{HEAD_THEME_SCRIPT}</script>
<style>{CSS_V2}</style>
</head>
<body>
{topbar_html}
{sidebar_html}
<main>
{_render_most_served_snapshot_v2(data['brands'], data['top_ads'], out_dir)}
{''.join(sections_html)}
</main>
<div id="lightbox"><img src="" alt=""></div>
<script id="intel-data" type="application/json">{payload_json}</script>
<script>{JS_V2}</script>
</body>
</html>
"""
    out_file = out_dir / "index.html"
    out_file.write_text(page, encoding="utf-8")
    return {
        "path": str(out_file),
        "n_brands": len(data["brands"]),
        "n_analyzed": data["set_tally"].n_total,
        "n_distinct_signals": len(data["distinct"]),
        "size_bytes": out_file.stat().st_size,
    }
