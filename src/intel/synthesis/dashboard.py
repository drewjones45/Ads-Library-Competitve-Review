"""Static HTML dashboard — renders the full intel state into a single
self-contained file: brand cards, cross-set heatmaps, distinctiveness,
whitespace, per-brand creative galleries, and the latest briefing.

Generated from the SQLite db + the on-disk creative images. Designed to be
opened locally (file://) — no server required. Re-run after every ingest +
analysis to refresh.
"""
from __future__ import annotations

import html
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..storage import connect
from .creative_readout import (
    _LIST_ATTRS,
    _SCALAR_ATTRS,
    _distinctiveness,
    _tally,
    _whitespace_for_brand,
    pull_analyzed_creatives,
)


# ----- helpers --------------------------------------------------------------

def _esc(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _heatmap_bg(share: float) -> str:
    """Linear interpolation white → brand color based on share 0..1."""
    if share <= 0:
        return "#f7f7f7"
    # base ramp from #eef → #2a5fb0 (cool blue)
    r = int(238 + (42 - 238) * share)
    g = int(238 + (95 - 238) * share)
    b = int(255 + (176 - 255) * share)
    return f"rgb({r},{g},{b})"


def _txt_color_for_bg(share: float) -> str:
    return "#ffffff" if share > 0.55 else "#1c1c1c"


def _relpath(target: Path, base: Path) -> str:
    """Compute a relative path from `base` (dashboard dir) to `target` (image)."""
    try:
        return str(Path(target).resolve().relative_to(Path(base).resolve()))
    except ValueError:
        # fall back to a ../ relative computation
        from os.path import relpath
        return relpath(str(target), str(base))


# ----- data collection ------------------------------------------------------

def _collect(conn: sqlite3.Connection, *, days: int) -> dict[str, Any]:
    """Pull every shape of data the dashboard needs in one pass."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Brand summary
    brands = []
    for r in conn.execute(
        "SELECT id, name, vertical, priority FROM competitors ORDER BY priority DESC, name"
    ).fetchall():
        cid = r["id"]
        ads_total = conn.execute("SELECT COUNT(*) c FROM ads WHERE competitor_id=?", (cid,)).fetchone()["c"]
        ads_active = conn.execute("SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND active=1", (cid,)).fetchone()["c"]
        # Counts the brand's PAID AD creatives only (excludes brand-store assets,
        # which get their own counts in the brand-store sub-section). Paid ads
        # are creatives where ad_id IS NOT NULL.
        creatives_total = conn.execute(
            "SELECT COUNT(*) c FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
            "WHERE a.competitor_id=?",
            (cid,),
        ).fetchone()["c"]
        creatives_analyzed = conn.execute(
            "SELECT COUNT(*) c FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
            "WHERE a.competitor_id=? AND cr.analyzed_at IS NOT NULL",
            (cid,),
        ).fetchone()["c"]
        new_ads = conn.execute(
            "SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND first_seen >= ?",
            (cid, since),
        ).fetchone()["c"]
        top_cta = conn.execute(
            "SELECT cta_type, COUNT(*) n FROM ads WHERE competitor_id=? AND cta_type IS NOT NULL "
            "GROUP BY cta_type ORDER BY n DESC LIMIT 1",
            (cid,),
        ).fetchone()
        brands.append({
            "id": cid,
            "name": r["name"],
            "vertical": r["vertical"],
            "priority": r["priority"],
            "ads_total": ads_total,
            "ads_active": ads_active,
            "creatives_total": creatives_total,
            "creatives_analyzed": creatives_analyzed,
            "new_ads": new_ads,
            "top_cta": top_cta["cta_type"] if top_cta else None,
        })

    # All analyzed creatives are pulled once. We then split into two cohorts
    # for downstream use:
    #   - ad_recs : creatives belonging to a paid ad (ad_id > 0). Used for the
    #     "Meta Ads Library" gallery AND for all cross-set tallies (comparison,
    #     distinctiveness, whitespace, brand-vs-brand) — keeping those views
    #     pure to paid-ad signal so brand-store data doesn't muddy the share %.
    #   - all_recs : everything analyzed (ads + brand-store). Used only for the
    #     unfiltered "Browse all creatives" gallery, where the Source filter
    #     chip lets the user toggle between contexts.
    all_recs = pull_analyzed_creatives()
    ad_recs = [r for r in all_recs if (r.ad_id or 0) > 0]

    # Cross-set views: ad creatives only.
    by_comp: dict[str, list] = {}
    for rec in ad_recs:
        by_comp.setdefault(rec.competitor_id, []).append(rec)
    set_tally = _tally(ad_recs)
    comp_tallies = {cid: _tally(recs) for cid, recs in by_comp.items()}
    distinct = _distinctiveness(comp_tallies, set_tally)
    whitespace = {cid: _whitespace_for_brand(cid, comp_tallies, set_tally) for cid in by_comp.keys()}

    # Recent ads (last N days, by brand)
    recent_ads: dict[str, list] = {}
    for cid in [b["id"] for b in brands]:
        rows = conn.execute(
            "SELECT ad_archive_id, first_seen, body_text, cta_type, link_url, page_name "
            "FROM ads WHERE competitor_id=? AND first_seen >= ? "
            "ORDER BY first_seen DESC LIMIT 50",
            (cid, since),
        ).fetchall()
        recent_ads[cid] = [dict(r) for r in rows]

    # Brand-store data per brand: most recent landing screenshot + last 24 analyzed
    # brand-store image creatives. Empty dict for brands without an amazon store.
    brand_store_by_brand: dict[str, dict] = {}
    for cid in [b["id"] for b in brands]:
        latest_obs = conn.execute(
            "SELECT observations.observed_at, observations.parsed_json "
            "FROM observations JOIN sources ON sources.id=observations.source_id "
            "WHERE sources.competitor_id=? AND sources.type='amazon_brand_store' "
            "ORDER BY observations.observed_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
        bs_creatives_rows = conn.execute(
            "SELECT id, asset_path, analysis_json, analyzed_at FROM creatives "
            "WHERE competitor_id=? AND asset_type='amazon_store_image' "
            "ORDER BY id DESC LIMIT 24",
            (cid,),
        ).fetchall()
        if not latest_obs and not bs_creatives_rows:
            continue
        latest_screenshot = None
        pages_count = 0
        image_count_total = 0
        if latest_obs and latest_obs["parsed_json"]:
            try:
                parsed = json.loads(latest_obs["parsed_json"])
                pages = parsed.get("pages") or []
                pages_count = len(pages)
                image_count_total = (parsed.get("totals") or {}).get("images", 0)
                if pages:
                    latest_screenshot = pages[0].get("screenshot_path")
            except Exception:
                pass
        bs_creatives = []
        analyzed_count = 0
        for r in bs_creatives_rows:
            try:
                analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else {}
            except Exception:
                analysis = {}
            if r["analyzed_at"]:
                analyzed_count += 1
            bs_creatives.append({
                "asset_path": r["asset_path"],
                "summary": analysis.get("summary_one_line"),
                "analyzed_at": r["analyzed_at"],
            })
        brand_store_by_brand[cid] = {
            "latest_observed_at": latest_obs["observed_at"] if latest_obs else None,
            "latest_screenshot": latest_screenshot,
            "pages_count": pages_count,
            "image_count_total": image_count_total,
            "analyzed_count": analyzed_count,
            "creatives": bs_creatives,
        }

    # Homepage data per brand: latest screenshot + latest hero promo +
    # most-recent analyzed homepage creatives. Empty dict for brands with
    # no website-source activity yet.
    homepage_by_brand: dict[str, dict] = {}
    for cid in [b["id"] for b in brands]:
        latest_obs = conn.execute(
            "SELECT observations.observed_at, observations.parsed_json "
            "FROM observations JOIN sources ON sources.id=observations.source_id "
            "WHERE sources.competitor_id=? AND sources.type='website' "
            "ORDER BY observations.observed_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
        latest_promo = conn.execute(
            "SELECT observed_at, headline, subhead, primary_cta_text, "
            "offer_claim, offer_value, offer_kind, expiration, "
            "channel_callouts, urgency_cues, confidence, cache_hit, raw_json "
            "FROM homepage_promos WHERE competitor_id=? "
            "ORDER BY observed_at DESC LIMIT 1",
            (cid,),
        ).fetchone()
        hp_creatives_rows = conn.execute(
            "SELECT id, asset_path, analysis_json, analyzed_at FROM creatives "
            "WHERE competitor_id=? AND asset_type='homepage_image' "
            "ORDER BY id DESC LIMIT 24",
            (cid,),
        ).fetchall()
        if not latest_obs and not latest_promo and not hp_creatives_rows:
            continue
        screenshot_path = None
        hero_image_path = None
        image_count_total = 0
        blocked = False
        block_vendor: str | None = None
        if latest_obs and latest_obs["parsed_json"]:
            try:
                parsed = json.loads(latest_obs["parsed_json"])
                blocked = bool(parsed.get("blocked"))
                block_vendor = parsed.get("block_vendor")
                if not blocked:
                    screenshot_path = parsed.get("screenshot_path")
                    hero_image_path = parsed.get("hero_image_path")
                    image_count_total = parsed.get("image_count", 0)
            except Exception:
                pass
        hp_creatives = []
        analyzed_count = 0
        for r in hp_creatives_rows:
            try:
                analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else {}
            except Exception:
                analysis = {}
            if r["analyzed_at"]:
                analyzed_count += 1
            hp_creatives.append({
                "asset_path": r["asset_path"],
                "summary": analysis.get("summary_one_line"),
                "analyzed_at": r["analyzed_at"],
            })
        promo_dict = None
        if latest_promo:
            promo_dict = dict(latest_promo)
            for k in ("channel_callouts", "urgency_cues"):
                try:
                    promo_dict[k] = json.loads(promo_dict[k] or "[]")
                except Exception:
                    promo_dict[k] = []
            # Decode raw_json for the richer site-content fields (positioning_statement,
            # design_critique, messaging_stance, products_pushed, audience_signals,
            # what_this_homepage_does_well, what_it_misses, strategist_one_liner,
            # secondary_ctas). Hand-authored entries via persist_homepage_analyses.py
            # carry the full structured analysis here.
            try:
                promo_dict["raw"] = json.loads(promo_dict.get("raw_json") or "{}")
            except Exception:
                promo_dict["raw"] = {}
        homepage_by_brand[cid] = {
            "latest_observed_at": latest_obs["observed_at"] if latest_obs else None,
            "screenshot_path": screenshot_path,
            "hero_image_path": hero_image_path,
            "image_count_total": image_count_total,
            "analyzed_count": analyzed_count,
            "creatives": hp_creatives,
            "latest_promo": promo_dict,
            "blocked": blocked,
            "block_vendor": block_vendor,
        }

    # Latest briefing
    latest_briefing = conn.execute(
        "SELECT id, title, body_md, created_at FROM briefings "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    # --- payloads for client-side features (filter, brand-vs-brand, delta view) ---
    creatives_index: list[dict] = []
    for rec in all_recs:
        a = rec.analysis
        # Derive a 'layoutFlags' list from the brand-store-ish booleans so they
        # can be filtered as chips alongside other arrays.
        layout_flags = []
        if a.get("before_after_present"):       layout_flags.append("before_after")
        if a.get("shoppable_imagery"):          layout_flags.append("shoppable_imagery")
        if a.get("hero_banner_present"):        layout_flags.append("hero_banner")
        if a.get("category_nav_visible"):       layout_flags.append("category_nav")

        creatives_index.append({
            "id": rec.ad_id,
            "comp": rec.competitor_id,
            "compName": rec.competitor_name,
            "adId": rec.ad_archive_id,
            "imgPath": str(rec.asset_path),  # absolute; relativized in JS at render time
            "firstSeen": rec.first_seen,
            "summary": a.get("summary_one_line"),
            "photo": a.get("photography_style"),
            "prod": a.get("production_style"),
            "emphasis": a.get("product_emphasis"),
            "hook": a.get("hook_style"),
            "appeal": a.get("emotional_vs_rational"),
            "logo": bool(a.get("logo_visible")),
            "logoBrand": a.get("logo_brand"),
            "products": a.get("products_visible") or [],
            "features": a.get("key_features") or [],
            "valueProps": a.get("value_props") or [],
            "seasonal": a.get("seasonal_tags") or [],
            "colors": a.get("dominant_colors_hex") or [],
            "ratio": a.get("aspect_ratio_guess"),
            # Phase A1 extended taxonomy:
            "bgColor": a.get("background_color"),
            "modelGender": a.get("model_gender"),
            "productInUse": a.get("product_in_use"),
            "context": a.get("creative_context"),
            "productGrouping": a.get("product_grouping"),
            "scene": a.get("scene_description"),
            "modelDemo": a.get("model_demo"),
            "ctaText": a.get("cta_verbatim_text"),
            "certifications": a.get("certifications_visible") or [],
            "awards": a.get("awards_or_rankings") or [],
            "layoutFlags": layout_flags,
        })

    # Wider ad index (for delta view) — past 60 days, lightweight columns only.
    wide_since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    ads_index = []
    for r in conn.execute(
        "SELECT competitor_id, ad_archive_id, first_seen, body_text, cta_type, link_url "
        "FROM ads WHERE first_seen >= ? ORDER BY first_seen DESC LIMIT 2000",
        (wide_since,),
    ).fetchall():
        ads_index.append({
            "comp": r["competitor_id"],
            "adId": r["ad_archive_id"],
            "firstSeen": r["first_seen"],
            "body": (r["body_text"] or "")[:240],
            "cta": r["cta_type"],
            "link": r["link_url"],
        })

    # Brand-vs-brand tallies span ALL analyzed creatives (Meta ads + homepage +
    # brand-store), not just paid-ad creatives. This lets the bvb selector show
    # every brand with any creative footprint — important for verticals where
    # only a subset of brands run direct Meta ads (e.g. decking, where Trex
    # advertises but TimberTech/Fiberon/Deckorators do not). Cross-set views
    # (distinctiveness, whitespace) still use `comp_tallies` (ad_recs only) to
    # keep their "paid-ad signal" semantics intact.
    all_by_comp_for_bvb: dict[str, list] = {}
    for rec in all_recs:
        all_by_comp_for_bvb.setdefault(rec.competitor_id, []).append(rec)
    bvb_comp_tallies = {cid: _tally(recs) for cid, recs in all_by_comp_for_bvb.items()}

    # Tallies serialized for brand-vs-brand JS rendering.
    def _ser(t):
        return {
            "n": t.n_total,
            "scalar": {k: dict(v) for k, v in t.scalar.items()},
            "boolean": {k: dict(v) for k, v in t.boolean.items()},
            "listed": {k: dict(v) for k, v in t.listed.items()},
        }
    brand_tallies_ser = {cid: _ser(t) for cid, t in bvb_comp_tallies.items()}
    set_tally_ser = _ser(set_tally)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "brands": brands,
        "by_comp_recs": by_comp,
        "set_tally": set_tally,
        "comp_tallies": comp_tallies,
        "distinct": distinct,
        "whitespace": whitespace,
        "recent_ads": recent_ads,
        "brand_store_by_brand": brand_store_by_brand,
        "homepage_by_brand": homepage_by_brand,
        "latest_briefing": dict(latest_briefing) if latest_briefing else None,
        # client-side payloads
        "client_creatives": creatives_index,
        "client_ads": ads_index,
        "client_tallies": brand_tallies_ser,
        "client_set_tally": set_tally_ser,
    }


# ----- HTML rendering -------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       margin: 0; color: #1c1c1c; background: #fafbfc; }
header { background: linear-gradient(135deg,#1f2c4a,#2a5fb0); color: white;
         padding: 22px 28px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
header .org { font-size: 12px; letter-spacing: 1.4px; text-transform: uppercase;
              opacity: 0.7; font-weight: 600; margin-bottom: 4px; }
header h1 { margin: 0 0 6px; font-size: 22px; font-weight: 600; }
header .meta { opacity: 0.85; font-size: 13px; }
/* Left-rail nav */
nav { background: #1f2c4a; color: white; position: fixed; left: 0; top: 0; bottom: 0;
      width: 240px; padding: 18px 0; overflow-y: auto; z-index: 10;
      box-shadow: 2px 0 6px rgba(0,0,0,0.08); }
nav .nav-org { padding: 0 20px 14px; border-bottom: 1px solid rgba(255,255,255,0.1);
                margin-bottom: 10px; }
nav .nav-org .eyebrow { font-size: 10px; letter-spacing: 1.4px; text-transform: uppercase;
                         opacity: 0.6; font-weight: 600; }
nav .nav-org .product { font-size: 13px; font-weight: 600; margin-top: 2px; line-height: 1.3; }
nav .nav-group { padding: 0 20px; font-size: 10px; letter-spacing: 1.2px; text-transform: uppercase;
                  opacity: 0.5; font-weight: 600; margin: 14px 0 4px; }
nav a { color: white; text-decoration: none; display: block; font-size: 13px;
        opacity: 0.78; padding: 7px 20px; border-left: 3px solid transparent;
        transition: all .12s ease; }
nav a:hover { opacity: 1; background: rgba(255,255,255,0.05); border-left-color: #5a9fdf; }
nav a.active { opacity: 1; background: rgba(90,159,223,0.15); border-left-color: #5a9fdf;
               font-weight: 600; }
main { margin-left: 240px; padding: 24px 28px 80px; max-width: 1320px; }
header { margin-left: 240px; }  /* header sits right of the rail */
section { margin: 36px 0; background: white; border: 1px solid #e6e8ec; border-radius: 8px;
          padding: 22px 26px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }
section h2 { margin: 0 0 14px; font-size: 18px; border-bottom: 2px solid #e6e8ec; padding-bottom: 8px; }
section h3 { margin-top: 22px; font-size: 14px; text-transform: uppercase; color: #555;
             letter-spacing: 0.5px; }

/* Stats strip */
.stats { display: grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap: 14px; }
.stat { background: #f3f5f8; padding: 14px; border-radius: 6px; border-left: 4px solid #2a5fb0; }
.stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: #666; }
.stat .value { font-size: 26px; font-weight: 600; color: #1f2c4a; margin-top: 4px; }

/* Brand cards */
.brand-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 14px; }
.brand-card { border: 1px solid #e6e8ec; border-radius: 6px; padding: 14px;
              transition: all .15s ease; background: white; }
.brand-card:hover { border-color: #2a5fb0; box-shadow: 0 2px 8px rgba(42,95,176,0.12); }
.brand-card a { color: #2a5fb0; text-decoration: none; font-weight: 600; font-size: 15px; }
.brand-card .vertical { font-size: 11px; color: #999; text-transform: uppercase; margin-left: 6px; }
.brand-card dl { display: grid; grid-template-columns: auto auto; gap: 3px 12px; margin: 10px 0 0;
                 font-size: 12px; }
.brand-card dt { color: #777; }
.brand-card dd { margin: 0; font-weight: 500; }
.priority-high { border-left: 4px solid #d9534f; }
.priority-medium { border-left: 4px solid #f0ad4e; }
.priority-low { border-left: 4px solid #999; }

/* Tables (heatmap) */
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12px; }
th, td { padding: 7px 9px; text-align: left; border: 1px solid #e6e8ec; }
th { background: #f3f5f8; font-weight: 600; }
td.heat { text-align: center; font-variant-numeric: tabular-nums; }
td.brand-label { font-family: ui-monospace,Menlo,Consolas,monospace; font-size: 11px; background:#f9fafc; }
tr.set-row td { background: #1f2c4a !important; color: white; font-weight: 600; }

/* Distinctiveness chips */
.distinct-row { display: grid; grid-template-columns: 130px 140px 1fr 120px; gap: 10px;
                padding: 8px 10px; margin: 4px 0; border-radius: 4px; background: #f3f5f8;
                font-size: 12px; align-items: center; }
.delta { color: white; background: #2a5fb0; padding: 2px 8px; border-radius: 12px;
         font-weight: 600; text-align: center; }

/* Creative gallery */
.gallery { display: grid; grid-template-columns: repeat(auto-fill,minmax(180px,1fr)); gap: 12px;
           margin-top: 12px; }
.creative { border: 1px solid #e6e8ec; border-radius: 6px; overflow: hidden; background: white;
            display: flex; flex-direction: column; }
.creative img { width: 100%; aspect-ratio: 1; object-fit: cover; background:#f3f5f8; cursor: zoom-in; }
.creative .body { padding: 8px 10px; font-size: 11px; }
/* Brand-store / homepage thumbnails (lane-bs, lane-hp). The img inside lacks
   any intrinsic constraint, so without these rules the natural-resolution
   product photos (often 1500+ px) blow past their grid cell and spill out
   of the lane container. Force a uniform 140px square crop. */
.bs-thumb { overflow: hidden; border-radius: 4px; background: #fff;
            border: 1px solid #e6e8ec; min-width: 0; }
.bs-thumb img { display: block; width: 100%; height: 140px; object-fit: cover;
                background: #f3f5f8; cursor: zoom-in; }
.bs-thumb .muted { padding: 4px 6px; line-height: 1.3; max-height: 32px;
                   overflow: hidden; text-overflow: ellipsis; }
/* Landing screenshot preview tile (lane-hp / lane-bs). Stitched full-page
   captures can be 5000+ px tall, which would otherwise stretch the lane row
   and leave a giant blank column next to the thumbnails. Show a fixed-size
   preview tile that's click-through to the full image. */
.landing-tile { display: block; width: 280px; max-width: 100%; height: 360px;
                overflow: hidden; border: 1px solid #e6e8ec; border-radius: 6px;
                background: #f3f5f8; position: relative; }
.landing-tile img { width: 100%; height: auto; display: block;
                    transition: transform .3s ease; }
.landing-tile:hover img { transform: translateY(-8px); }
.landing-tile::after { content: 'click to open full page'; position: absolute;
                       bottom: 0; left: 0; right: 0; background: rgba(28,28,28,0.78);
                       color: white; font-size: 11px; padding: 4px 8px; text-align: center;
                       opacity: 0; transition: opacity .15s ease; }
.landing-tile:hover::after { opacity: 1; }
.lane-hp img, .lane-bs img { max-width: 100%; }
.creative .summary { color: #444; line-height: 1.35; margin-bottom: 4px; }
.creative .tags { margin-top: 6px; }
.tag { display: inline-block; background: #e8eef9; color: #1f2c4a; font-size: 10px;
       padding: 1px 6px; border-radius: 3px; margin: 1px 2px 0 0; }
.tag.kf { background: #fbeede; color: #7a4a1c; }
.tag.prod { background: #e7f4ea; color: #2a6c3a; }

/* Whitespace */
.ws { margin: 8px 0; padding: 10px 12px; border-left: 3px solid #d9534f;
      background: #fdf4f4; border-radius: 0 4px 4px 0; }
.ws-brand { font-family: ui-monospace,Menlo,Consolas,monospace; font-weight: 600;
            font-size: 13px; margin-bottom: 4px; }
.ws-item { font-size: 12px; margin-left: 12px; }

/* Ads list */
.ad { display: grid; grid-template-columns: 90px 1fr 110px; gap: 12px; padding: 10px 0;
      border-bottom: 1px solid #eef0f3; font-size: 12px; align-items: start; }
.ad .id { font-family: ui-monospace,Menlo,Consolas,monospace; color: #666; }
.ad .body { color: #1c1c1c; }
.ad .cta { color: #2a5fb0; font-weight: 600; text-align: right; }

/* Briefing */
.briefing { white-space: pre-wrap; font-size: 13px; line-height: 1.6; }
.briefing h1,.briefing h2,.briefing h3 { font-weight: 600; }

/* Lightbox */
#lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 100;
            align-items: center; justify-content: center; cursor: zoom-out; }
#lightbox.open { display: flex; }
#lightbox img { max-width: 92%; max-height: 92%; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }

/* Filter UI — dropdown style */
.filter-bar { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 8px;
              margin: 10px 0 16px; }
.filter-dropdown { position: relative; }
.filter-dropdown > button { width: 100%; background: #f7f8fa; border: 1px solid #cfd5dd;
                            border-radius: 6px; padding: 7px 26px 7px 11px; font-size: 12px;
                            text-align: left; cursor: pointer; color: #1c1c1c;
                            position: relative; transition: border-color .12s ease; }
.filter-dropdown > button:hover { border-color: #2a5fb0; }
.filter-dropdown.open > button { border-color: #2a5fb0; background: white; }
.filter-dropdown > button .label { font-weight: 600; color: #444; font-size: 11px;
                                    text-transform: uppercase; letter-spacing: 0.4px; }
.filter-dropdown > button .badge { background: #2a5fb0; color: white; border-radius: 10px;
                                    padding: 1px 7px; font-size: 10px; margin-left: 6px;
                                    font-weight: 600; }
.filter-dropdown > button::after { content: '▾'; position: absolute; right: 10px; top: 50%;
                                    transform: translateY(-50%); font-size: 10px; color: #888; }
.filter-dropdown.open > button::after { transform: translateY(-50%) rotate(180deg); color: #2a5fb0; }
.filter-menu { display: none; position: absolute; left: 0; right: 0; top: calc(100% + 4px);
               background: white; border: 1px solid #cfd5dd; border-radius: 6px; padding: 6px 0;
               box-shadow: 0 4px 12px rgba(0,0,0,0.08); z-index: 50;
               max-height: 320px; overflow-y: auto; min-width: 200px; }
.filter-dropdown.open .filter-menu { display: block; }
.filter-menu .menu-head { display: flex; justify-content: space-between; align-items: center;
                          padding: 4px 12px 6px; border-bottom: 1px solid #eef0f3; margin-bottom: 4px; }
.filter-menu .menu-head .group-clear { background: none; border: none; color: #2a5fb0;
                                        cursor: pointer; font-size: 11px; padding: 0; }
.filter-menu .menu-head .group-clear:hover { text-decoration: underline; }
.filter-menu .menu-head .group-clear:disabled { color: #aaa; cursor: default; text-decoration: none; }
.filter-menu label { display: flex; align-items: center; gap: 8px; padding: 5px 12px;
                     font-size: 12px; cursor: pointer; user-select: none; }
.filter-menu label:hover { background: #f3f5f8; }
.filter-menu label input { margin: 0; cursor: pointer; }
.filter-menu label .name { flex: 1; }
.filter-menu label .count { color: #888; font-size: 10px; }
.filter-status { font-size: 12px; color: #555; padding: 6px 0; margin-bottom: 8px;
                 display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.filter-status button { background: white; border: 1px solid #cfd5dd; border-radius: 4px;
                        padding: 4px 10px; font-size: 11px; cursor: pointer; }
.filter-status button:hover { background: #f3f5f8; }

/* Brand-vs-brand */
.bvb-controls { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 12px 0 18px; }
.bvb-controls label { display: block; font-size: 11px; color: #666; text-transform: uppercase;
                       letter-spacing: 0.5px; margin-bottom: 4px; font-weight: 600; }
.bvb-controls select { width: 100%; padding: 8px 10px; font-size: 14px; border: 1px solid #cfd5dd;
                       border-radius: 4px; background: white; }
.bvb-split { display: grid; grid-template-columns: 1fr 80px 1fr; gap: 14px; align-items: start; }
.bvb-col { background: #f7f8fa; border: 1px solid #e6e8ec; border-radius: 6px; padding: 14px; }
.bvb-col h3 { margin: 0 0 12px; font-size: 14px; color: #1f2c4a; }
.bvb-row { display: grid; grid-template-columns: 170px 1fr 40px; gap: 10px; font-size: 12px;
           padding: 3px 0; align-items: center; }
.bvb-row .val { background: #2a5fb0; color: white; padding: 1px 6px; border-radius: 3px;
                font-size: 11px; text-align: center; min-width: 32px; }
.bvb-row .lab { font-family: ui-monospace,Menlo,Consolas,monospace; font-size: 11px; color: #444;
                min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bvb-vs { font-size: 14px; color: #888; text-align: center; padding-top: 40px; font-weight: 600; }
.bvb-attr-block { margin-bottom: 14px; }
.bvb-attr-block h4 { margin: 0 0 6px; font-size: 11px; text-transform: uppercase;
                      color: #666; letter-spacing: 0.5px; }

/* Delta view */
.delta-controls { display: flex; gap: 16px; align-items: center; margin: 10px 0 16px;
                  flex-wrap: wrap; }
.delta-controls label { font-size: 12px; color: #555; }
.delta-controls input[type=date] { padding: 6px 10px; font-size: 14px;
                                    border: 1px solid #cfd5dd; border-radius: 4px; }
.delta-stats { display: grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr));
               gap: 12px; margin-bottom: 16px; }
.delta-brand-bar { display: grid; grid-template-columns: 160px 1fr 50px; gap: 10px;
                   padding: 5px 0; font-size: 12px; align-items: center; }
.delta-brand-bar .bar { height: 14px; background: #e6e8ec; border-radius: 7px; overflow: hidden; }
.delta-brand-bar .bar > div { height: 100%; background: linear-gradient(90deg,#2a5fb0,#5a9fdf);
                               border-radius: 7px; }
.delta-brand-bar .n { text-align: right; font-weight: 600; font-family: ui-monospace; }

/* Misc */
.muted { color: #888; font-size: 12px; }
[hidden] { display: none !important; }
"""

JS = """
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
function bindLightbox(scope) {
  (scope || document).querySelectorAll('.creative img').forEach(img => {
    img.addEventListener('click', e => {
      const lb = document.getElementById('lightbox');
      lb.querySelector('img').src = e.target.src;
      lb.classList.add('open');
    });
  });
}
bindLightbox(document);
document.getElementById('lightbox').addEventListener('click', () => {
  document.getElementById('lightbox').classList.remove('open');
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
};

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

function renderFilterGallery() {
  const container = document.getElementById('filter-gallery-grid');
  if (!container) return;
  const matched = DATA.client_creatives.filter(matchesFilters);
  container.innerHTML = matched.map(c => `
    <div class="creative" data-comp="${c.comp}">
      <img src="${relativize(c.imgPath)}" loading="lazy" alt="${c.adId}">
      <div class="body">
        <div class="summary">${escapeHTML(c.summary || '(no summary)')}</div>
        <div class="muted"><code>${escapeHTML(c.comp)}</code> · ad <code>${c.adId}</code></div>
        <div class="tags">
          ${c.photo ? `<span class="tag">${c.photo}</span>` : ''}
          ${c.hook ? `<span class="tag">${c.hook}</span>` : ''}
          ${(c.products || []).slice(0,2).map(p => `<span class="tag prod">${escapeHTML(p)}</span>`).join('')}
          ${(c.features || []).slice(0,3).map(f => `<span class="tag kf">${escapeHTML(f)}</span>`).join('')}
        </div>
      </div>
    </div>
  `).join('');
  document.getElementById('filter-status-count').textContent =
    `${matched.length} of ${DATA.client_creatives.length} creatives`;
  bindLightbox(container);
}

function buildFilterDropdowns() {
  const root = document.getElementById('filter-bar');
  if (!root) return;
  const groups = [
    {key: 'comp', label: 'Brand', getter: c => [c.comp]},
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
        ${g.items.map(([v, n]) => `
          <label>
            <input type="checkbox" data-grp="${g.key}" data-val="${escapeHTML(v)}">
            <span class="name">${escapeHTML(v)}</span>
            <span class="count">${n}</span>
          </label>
        `).join('')}
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
        <div class="bar" style="background:#e6e8ec;height:8px;border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${share}%;background:#2a5fb0;border-radius:4px"></div>
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
  const tA = DATA.client_tallies[a], tB = DATA.client_tallies[b];
  if (!tA || !tB) return;
  document.getElementById('bvb-left').innerHTML = renderBvbCol(a, tA, tB);
  document.getElementById('bvb-right').innerHTML = renderBvbCol(b, tB, tA);
}
populateBrandSelectors();
document.getElementById('bvb-a')?.addEventListener('change', renderBvb);
document.getElementById('bvb-b')?.addEventListener('change', renderBvb);
renderBvb();

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
(function setupNavObserver() {
  const navLinks = new Map();
  document.querySelectorAll('nav a[href^="#"]').forEach(a => {
    navLinks.set(a.getAttribute('href').slice(1), a);
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
      navLinks.get(best).classList.add('active');
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


def _render_stats(data: dict) -> str:
    n_brands = len(data["brands"])
    n_ads = sum(b["ads_total"] for b in data["brands"])
    n_active = sum(b["ads_active"] for b in data["brands"])
    n_creatives = sum(b["creatives_total"] for b in data["brands"])
    n_analyzed = sum(b["creatives_analyzed"] for b in data["brands"])
    n_new = sum(b["new_ads"] for b in data["brands"])
    return f"""
    <div class="stats">
      <div class="stat"><div class="label">Brands tracked</div><div class="value">{n_brands}</div></div>
      <div class="stat"><div class="label">Ads (total)</div><div class="value">{n_ads}</div></div>
      <div class="stat"><div class="label">Active ads</div><div class="value">{n_active}</div></div>
      <div class="stat"><div class="label">Creatives stored</div><div class="value">{n_creatives}</div></div>
      <div class="stat"><div class="label">Creatives analyzed</div><div class="value">{n_analyzed}</div></div>
      <div class="stat"><div class="label">New ads ({data['window_days']}d)</div><div class="value">{n_new}</div></div>
    </div>
    """


def _render_brand_cards(data: dict) -> str:
    items = []
    for b in data["brands"]:
        pri = b["priority"] or "medium"
        items.append(f"""
        <div class="brand-card priority-{_esc(pri)}">
          <a href="#brand-{_esc(b['id'])}">{_esc(b['name'])}</a>
          <span class="vertical">{_esc(b['vertical'])}</span>
          <dl>
            <dt>Ads (total / active)</dt><dd>{b['ads_total']} / {b['ads_active']}</dd>
            <dt>New in {data['window_days']}d</dt><dd>{b['new_ads']}</dd>
            <dt>Creatives (analyzed)</dt><dd>{b['creatives_total']} ({b['creatives_analyzed']})</dd>
            <dt>Top CTA</dt><dd>{_esc(b['top_cta'] or '—')}</dd>
          </dl>
        </div>
        """)
    return f'<div class="brand-grid">{"".join(items)}</div>'


def _render_heatmap_table(attr_label: str, comp_tallies: dict, set_tally, *, attr_kind: str) -> str:
    """attr_kind: 'scalar' or 'listed'."""
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
            bg = _heatmap_bg(share)
            fg = _txt_color_for_bg(share)
            cells.append(f'<td class="heat" style="background:{bg};color:{fg}">{share:.0%}</td>')
        body_rows.append(f'<tr><td class="brand-label">{_esc(cid)}</td>{"".join(cells)}<td class="heat">{t.n_total}</td></tr>')
    # set totals row
    set_cells = []
    for v in top_vals:
        c = (set_tally.scalar if attr_kind == "scalar" else set_tally.listed).get(attr_label, Counter()).get(v, 0)
        share = c / set_tally.n_total if set_tally.n_total else 0
        set_cells.append(f'<td class="heat">{share:.0%}</td>')
    body_rows.append(
        f'<tr class="set-row"><td>set</td>{"".join(set_cells)}<td class="heat">{set_tally.n_total}</td></tr>'
    )
    return f"<h3>{_esc(attr_label)}</h3><table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_distinctiveness(data: dict) -> str:
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


def _render_whitespace(data: dict) -> str:
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


def _render_brand_store_block(bs_data: dict | None, dashboard_dir: Path) -> str:
    """Per-brand 'Amazon Brand Store' subsection. Returns empty string if the
    brand has no brand-store activity. Visually distinct from the Meta Ads
    lane via a tinted background + accent border."""
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
    # Show up to 12 thumbnails (was 6). This lane is the home for brand-store
    # creatives, so it should surface meaningfully.
    cre_html_parts = []
    for cre in creatives[:12]:
        rel = _relpath(Path(cre["asset_path"]), dashboard_dir)
        cap = _esc((cre.get("summary") or "")[:80])
        cre_html_parts.append(
            f'<div class="bs-thumb"><img src="{rel}" loading="lazy" alt="brand store image">'
            f'<div class="muted" style="font-size:11px">{cap}</div></div>'
        )
    cre_html = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));'
        f'gap:10px;margin-top:12px">{"".join(cre_html_parts)}</div>'
    ) if cre_html_parts else (
        '<p class="muted">Images captured but not yet analyzed. Run '
        '<code>intel analyze-creatives</code>.</p>'
    )
    observed = bs_data.get("latest_observed_at") or "—"
    # Tinted Amazon-orange-leaning accent so the brand-store lane is visually
    # distinct from the Meta-Ads lane above it.
    return f"""
    <div class="lane lane-bs" style="margin-top:28px;background:#fff8f0;border-left:4px solid #ff9900;
                                      border-radius:6px;padding:18px 20px">
      <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
        <span style="background:#ff9900;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                     text-transform:uppercase;padding:3px 8px;border-radius:3px">Amazon</span>
        <h3 style="margin:0;font-size:18px">Brand Store</h3>
      </div>
      <div class="stats" style="margin-bottom:12px">
        <div class="stat"><div class="label">Pages captured</div><div class="value">{bs_data.get('pages_count', 0)}</div></div>
        <div class="stat"><div class="label">Images captured</div><div class="value">{bs_data.get('image_count_total', 0)}</div></div>
        <div class="stat"><div class="label">Analyzed</div><div class="value">{bs_data.get('analyzed_count', 0)}/{len(creatives)}</div></div>
        <div class="stat"><div class="label">Last captured</div><div class="value" style="font-size:14px">{_esc(observed[:16].replace('T', ' '))}</div></div>
      </div>
      <div style="display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:start">
        <div>{screenshot_html}</div>
        <div>{cre_html}</div>
      </div>
    </div>
    """


def _render_homepage_block(hp_data: dict | None, dashboard_dir: Path) -> str:
    """Per-brand 'Website / Homepage' subsection. Returns empty string for brands
    with no website-source activity. Green accent visually distinguishes this
    lane from META (blue) and AMAZON (orange)."""
    if not hp_data:
        return ""

    # Anti-bot wall (PerimeterX/Cloudflare/etc.) — show a clear BLOCKED state
    # instead of pretending the captcha page is the brand's homepage. The
    # ingest run still recorded the attempt (so we know it's not just stale),
    # but no images / promo are surfaced.
    if hp_data.get("blocked"):
        vendor = (hp_data.get("block_vendor") or "unknown").replace("_", " ").title()
        observed = (hp_data.get("latest_observed_at") or "—")[:16].replace("T", " ")
        return f"""
        <div class="lane lane-hp lane-hp-blocked" style="margin-top:28px;background:#fff5f5;
                    border-left:4px solid #d33;border-radius:6px;padding:18px 20px">
          <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="background:#d33;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                         text-transform:uppercase;padding:3px 8px;border-radius:3px">Website</span>
            <h3 style="margin:0;font-size:18px">Homepage — blocked</h3>
          </div>
          <div style="font-size:13px;color:#444;line-height:1.55">
            <strong>Anti-bot wall detected:</strong> <code>{_esc(vendor)}</code>.
            The scraper hit a captcha / human-verification screen instead of the brand's homepage.
            <em>No images or promo data extracted — this brand is excluded from analysis until we can route around the block.</em>
          </div>
          <div class="muted" style="font-size:12px;margin-top:8px">Last attempted: {_esc(observed)}</div>
          <details style="margin-top:10px;font-size:12px">
            <summary style="cursor:pointer;color:#555">Mitigation options</summary>
            <ul style="margin:8px 0 0 18px;padding:0;color:#444;line-height:1.55">
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
    creatives = hp_data.get("creatives") or []
    cre_html_parts = []
    for cre in creatives[:12]:
        rel = _relpath(Path(cre["asset_path"]), dashboard_dir)
        cap = _esc((cre.get("summary") or "")[:80])
        cre_html_parts.append(
            f'<div class="bs-thumb"><img src="{rel}" loading="lazy" alt="homepage image">'
            f'<div class="muted" style="font-size:11px">{cap}</div></div>'
        )
    cre_html = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));'
        f'gap:10px;margin-top:12px">{"".join(cre_html_parts)}</div>'
    ) if cre_html_parts else (
        '<p class="muted">Images captured but not yet analyzed. Run '
        '<code>intel analyze-creatives</code>.</p>'
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
        cache = " <span style='color:#6e7781;font-size:11px'>(cached)</span>" if promo.get("cache_hit") else ""
        conf_pill = (f'<span style="background:#e6f4ea;color:#1e7e34;font-size:10px;'
                     f'font-weight:700;padding:2px 7px;border-radius:3px;letter-spacing:0.5px">'
                     f'CONFIDENCE {int(round(confidence*100))}%</span>') if isinstance(confidence, (int, float)) else ""
        offer_badge = ""
        if claim:
            offer_badge = (
                f'<div style="display:inline-block;background:#fff3bf;color:#7a5a00;'
                f'padding:4px 10px;border-radius:4px;font-weight:600;font-size:12px;'
                f'margin-bottom:8px">{claim}</div>'
            )

        # Strategist one-liner (top of card, distinct treatment)
        one_liner = _esc(raw.get("strategist_one_liner") or "")
        one_liner_html = (f'<div style="background:#0a3818;color:#d4f057;font-style:italic;'
                          f'padding:14px 18px;border-radius:4px;margin-bottom:14px;line-height:1.5;'
                          f'font-size:14px">{one_liner}</div>') if one_liner else ""

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
                    f'<span style="background:#f0f4f7;color:#1f2c4a;font-size:11px;'
                    f'font-weight:600;padding:3px 9px;border-radius:3px;'
                    f'border:1px solid #d8dee5"><span style="color:#6c757d;'
                    f'font-weight:500">{label_key}:</span> {_esc(value)}</span>')
        stance_html = (f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 14px">'
                       f'{"".join(stance_tags)}</div>') if stance_tags else ""

        # CTAs — primary + secondary
        primary_cta_html = (f'<div style="margin-top:6px"><span class="muted" style="font-size:11px;'
                            f'text-transform:uppercase;letter-spacing:0.8px;font-weight:600">Primary CTA</span><br>'
                            f'<code style="background:#0a3818;color:#d4f057;padding:3px 8px;'
                            f'border-radius:3px;font-weight:600">{cta}</code></div>') if cta else ""
        secondary_ctas = raw.get("secondary_ctas") or []
        secondary_html = ""
        if secondary_ctas:
            chips = "".join(
                f'<code style="background:#f6f8fa;color:#24292e;padding:2px 7px;border-radius:3px;'
                f'border:1px solid #d8dee5;font-size:11px;margin-right:5px;margin-bottom:5px;'
                f'display:inline-block">{_esc(s)}</code>'
                for s in secondary_ctas[:8]
            )
            secondary_html = (f'<div style="margin-top:8px"><span class="muted" style="font-size:11px;'
                              f'text-transform:uppercase;letter-spacing:0.8px;font-weight:600">'
                              f'Secondary CTAs ({len(secondary_ctas)})</span><br>'
                              f'<div style="margin-top:5px">{chips}</div></div>')

        # Positioning + design critique (expandable)
        positioning = _esc(raw.get("positioning_statement") or "")
        positioning_html = (f'<div style="margin-top:14px;padding-top:14px;border-top:1px solid #e6e8ec">'
                            f'<div class="muted" style="font-size:11px;text-transform:uppercase;'
                            f'letter-spacing:0.8px;font-weight:600;margin-bottom:6px">Positioning read</div>'
                            f'<div style="font-size:14px;line-height:1.55;color:#1f2c4a">'
                            f'{positioning}</div></div>') if positioning else ""

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
                        f'<div style="display:grid;grid-template-columns:130px 1fr;'
                        f'gap:10px;padding:6px 0;border-bottom:1px solid #f0f2f5;font-size:13px;'
                        f'line-height:1.5"><span style="font-weight:600;color:#1f2c4a">'
                        f'{label}</span><span style="color:#444">{_esc(v)}</span></div>')
            notable = design.get("notable_design_choices") or []
            notable_html = ""
            if notable:
                items = "".join(f'<li style="margin:4px 0">{_esc(n)}</li>' for n in notable)
                notable_html = (f'<div style="margin-top:8px"><span style="font-weight:600;color:#1f2c4a;'
                                f'font-size:13px">Notable choices</span>'
                                f'<ul style="margin:4px 0 0 18px;padding:0;font-size:13px;color:#444">'
                                f'{items}</ul></div>')
            design_html = (f'<details style="margin-top:10px"><summary style="cursor:pointer;'
                           f'font-size:11px;text-transform:uppercase;letter-spacing:0.8px;'
                           f'font-weight:600;color:#1f2c4a">Design critique</summary>'
                           f'<div style="margin-top:10px">{"".join(critique_rows)}'
                           f'{notable_html}</div></details>')

        # What works / What misses (two-column)
        works = raw.get("what_this_homepage_does_well") or []
        misses = raw.get("what_it_misses") or []
        ww_html = ""
        if works or misses:
            works_html = "".join(f'<li style="margin:5px 0">{_esc(w)}</li>' for w in works[:5])
            misses_html = "".join(f'<li style="margin:5px 0">{_esc(m)}</li>' for m in misses[:5])
            ww_html = f"""
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px;
                        padding-top:14px;border-top:1px solid #e6e8ec">
              <div>
                <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.8px;
                            font-weight:600;color:#1e7e34;margin-bottom:6px">What works</div>
                <ul style="margin:0 0 0 16px;padding:0;font-size:13px;color:#444;line-height:1.5">{works_html}</ul>
              </div>
              <div>
                <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.8px;
                            font-weight:600;color:#c8242b;margin-bottom:6px">What it misses</div>
                <ul style="margin:0 0 0 16px;padding:0;font-size:13px;color:#444;line-height:1.5">{misses_html}</ul>
              </div>
            </div>
            """

        exp_html = f'<div class="muted" style="margin-top:4px;font-size:12px">expires: {exp}</div>' if exp else ""
        promo_html = f"""
        <div style="background:white;border:1px solid #d8e7d3;border-radius:6px;
                    padding:18px 22px;margin-bottom:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;
                      margin-bottom:10px;gap:10px">
            <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:1px;
                                      font-weight:600">Site Content Analysis (latest){cache}</div>
            {conf_pill}
          </div>
          {one_liner_html}
          {offer_badge}
          <div style="font-size:17px;font-weight:600;color:#1f2c4a;line-height:1.3">{head}</div>
          <div class="muted" style="margin-top:3px;font-size:14px;line-height:1.45">{sub}</div>
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
    <div class="lane lane-hp" style="margin-top:28px;background:#f1faf3;border-left:4px solid #2da44e;
                                      border-radius:6px;padding:18px 20px">
      <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
        <span style="background:#2da44e;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                     text-transform:uppercase;padding:3px 8px;border-radius:3px">Website</span>
        <h3 style="margin:0;font-size:18px">Homepage</h3>
      </div>
      {promo_html}
      <div class="stats" style="margin-bottom:12px">
        <div class="stat" style="border-left-color:#2da44e">
          <div class="label">Images captured</div><div class="value">{hp_data.get('image_count_total', 0)}</div></div>
        <div class="stat" style="border-left-color:#2da44e">
          <div class="label">Analyzed</div><div class="value">{hp_data.get('analyzed_count', 0)}/{len(creatives)}</div></div>
        <div class="stat" style="border-left-color:#2da44e">
          <div class="label">Last captured</div><div class="value" style="font-size:14px">{_esc(observed[:16].replace('T', ' '))}</div></div>
      </div>
      <div style="display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:start">
        <div>{screenshot_html}</div>
        <div>{cre_html}</div>
      </div>
    </div>
    """


def _render_brand_section(brand: dict, recs: list, recent_ads: list, dashboard_dir: Path,
                           bs_data: dict | None = None,
                           hp_data: dict | None = None) -> str:
    # Creative gallery
    gallery_items = []
    for rec in recs[:36]:
        a = rec.analysis
        img_rel = _relpath(Path(rec.asset_path), dashboard_dir)
        summary = _esc(a.get("summary_one_line") or "")
        tags_html = []
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
          <img src="{img_rel}" loading="lazy" alt="ad {rec.ad_archive_id}">
          <div class="body">
            <div class="summary">{summary}</div>
            <div class="muted">ad <code>{_esc(rec.ad_archive_id)}</code></div>
            <div class="tags">{''.join(tags_html)}</div>
          </div>
        </div>
        """)
    if not gallery_items:
        gallery_html = '<p class="muted">No analyzed creatives. Run <code>intel analyze-creatives --competitor ' + _esc(brand['id']) + '</code>.</p>'
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
    brand_store_html = _render_brand_store_block(bs_data, dashboard_dir)
    homepage_html = _render_homepage_block(hp_data, dashboard_dir)
    return f"""
    <section id="brand-{_esc(brand['id'])}">
      <h2>{_esc(brand['name'])} <span class="muted">— {_esc(brand['vertical'])} · priority {_esc(pri)}</span></h2>

      <div class="lane lane-meta" style="background:#f4f7fb;border-left:4px solid #2a5fb0;
                                          border-radius:6px;padding:18px 20px">
        <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
          <span style="background:#2a5fb0;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                       text-transform:uppercase;padding:3px 8px;border-radius:3px">Meta</span>
          <h3 style="margin:0;font-size:18px">Ads Library</h3>
        </div>
        <div class="stats" style="margin-bottom:18px">
          <div class="stat"><div class="label">Active ads</div><div class="value">{brand['ads_active']}</div></div>
          <div class="stat"><div class="label">New in window</div><div class="value">{brand['new_ads']}</div></div>
          <div class="stat"><div class="label">Ad creatives analyzed</div><div class="value">{brand['creatives_analyzed']}/{brand['creatives_total']}</div></div>
          <div class="stat"><div class="label">Top CTA</div><div class="value" style="font-size:16px">{_esc(brand['top_cta'] or '—')}</div></div>
        </div>
        <h4 style="margin:14px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#555">Creative gallery</h4>
        {gallery_html}
        <h4 style="margin:18px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#555">Recent ads (last {{days}} days)</h4>
        {ads_html}
      </div>

      {homepage_html}
      {brand_store_html}
    </section>
    """


def _render_briefing(data: dict) -> str:
    b = data["latest_briefing"]
    if not b:
        return '<p class="muted">No briefings yet. Run <code>intel brief</code>.</p>'
    # render markdown crudely (we want zero external deps for the HTML)
    body = b["body_md"]
    return f"""
    <div class="muted">{_esc(b['title'])} — generated {_esc(b['created_at'])} (briefing id {b['id']})</div>
    <pre class="briefing">{_esc(body)}</pre>
    """


DEFAULT_ORG = "Horizon Commerce"
DEFAULT_PRODUCT = "Creative & Competitive Intelligence"


def build_dashboard(
    out_dir: Path | str,
    *,
    days: int = 30,
    org_name: str = DEFAULT_ORG,
    product_name: str = DEFAULT_PRODUCT,
) -> dict[str, Any]:
    """Generate the dashboard at out_dir/index.html. Returns summary metadata."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        data = _collect(conn, days=days)

    sections_html = []

    # Brand cards
    sections_html.append(f"""
    <section id="overview">
      <h2>Overview</h2>
      {_render_stats(data)}
      <h3 style="margin-top:22px">Brands</h3>
      {_render_brand_cards(data)}
    </section>
    """)

    # ---- NEW: delta view (what's new since X) ----
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
        cross_html.append(_render_heatmap_table(attr, data["comp_tallies"], data["set_tally"], attr_kind="scalar"))
    for attr in _LIST_ATTRS:
        cross_html.append(_render_heatmap_table(attr, data["comp_tallies"], data["set_tally"], attr_kind="listed"))
    sections_html.append(f"""
    <section id="comparison">
      <h2>Cross-competitor creative comparison</h2>
      <p class="muted">{data['set_tally'].n_total} analyzed creatives. Cell color = share of brand's creatives with that attribute value (darker = higher).</p>
      {''.join(cross_html)}
    </section>
    """)

    # ---- NEW: brand-vs-brand ----
    sections_html.append("""
    <section id="bvb">
      <h2>Brand vs Brand</h2>
      <p class="muted">Pick any two brands to see their attribute distributions side by side. Bars show % of each brand's analyzed creatives with that value.</p>
      <div class="bvb-controls">
        <div><label>Brand A</label><select id="bvb-a"></select></div>
        <div><label>Brand B</label><select id="bvb-b"></select></div>
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
      {_render_distinctiveness(data)}
    </section>
    """)

    # Whitespace
    sections_html.append(f"""
    <section id="whitespace">
      <h2>Whitespace — what each brand is NOT using that the set is</h2>
      {_render_whitespace(data)}
    </section>
    """)

    # ---- NEW: filterable all-brand gallery ----
    sections_html.append("""
    <section id="browse">
      <h2>Browse all creatives</h2>
      <p class="muted">Open a dropdown to select values. Multiple values within a group are OR; across groups are AND.</p>
      <div class="filter-bar" id="filter-bar"></div>
      <div class="filter-status">
        <span id="filter-status-count"></span>
        <button id="clear-filters">Clear all filters</button>
      </div>
      <div class="gallery" id="filter-gallery-grid"></div>
    </section>
    """)

    # Per-brand
    for brand in data["brands"]:
        recs = data["by_comp_recs"].get(brand["id"], [])
        recent = data["recent_ads"].get(brand["id"], [])
        bs_data = data["brand_store_by_brand"].get(brand["id"])
        hp_data = data["homepage_by_brand"].get(brand["id"])
        sections_html.append(
            _render_brand_section(brand, recs, recent, out_dir, bs_data=bs_data, hp_data=hp_data)
            .replace("{days}", str(days))
        )

    # Briefing
    sections_html.append(f"""
    <section id="briefing">
      <h2>Latest briefing</h2>
      {_render_briefing(data)}
    </section>
    """)

    # Left-rail nav: grouped sections. Each tuple = (group label or None, [(label, href), ...]).
    nav_groups: list[tuple[str | None, list[tuple[str, str]]]] = [
        (None, [('Overview', '#overview')]),
        ('Analysis', [
            ("What's new", '#delta'),
            ('Comparison', '#comparison'),
            ('Brand vs Brand', '#bvb'),
            ('Distinctiveness', '#distinctiveness'),
            ('Whitespace', '#whitespace'),
            ('Browse all creatives', '#browse'),
        ]),
        ('Brands', [(brand["name"], f"#brand-{brand['id']}") for brand in data["brands"]]),
        ('Reporting', [('Latest briefing', '#briefing')]),
    ]
    nav_html_parts = [
        f'<div class="nav-org">'
        f'<div class="eyebrow">{_esc(org_name)}</div>'
        f'<div class="product">{_esc(product_name)}</div>'
        f'</div>'
    ]
    for group_label, items in nav_groups:
        if group_label:
            nav_html_parts.append(f'<div class="nav-group">{_esc(group_label)}</div>')
        for label, href in items:
            nav_html_parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
    nav_html = "".join(nav_html_parts)

    # ---- embedded JSON payload for client-side features ----
    brands_meta = {b["id"]: b["name"] for b in data["brands"]}
    payload = {
        "client_creatives": data["client_creatives"],
        "client_ads": data["client_ads"],
        "client_tallies": data["client_tallies"],
        "client_set_tally": data["client_set_tally"],
        "brands_meta": brands_meta,
        "window_days": days,
    }
    payload_json = json.dumps(payload, default=str)

    page_title = f"{org_name} — {product_name}"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(page_title)} — {data['generated_at'][:10]}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="org">{_esc(org_name)}</div>
  <h1>{_esc(product_name)}</h1>
  <div class="meta">Generated {_esc(data['generated_at'])} · window: last {days}d · {len(data['brands'])} brands</div>
</header>
<nav>{nav_html}</nav>
<main>
{''.join(sections_html)}
</main>
<div id="lightbox"><img src="" alt=""></div>
<script id="intel-data" type="application/json">{payload_json}</script>
<script>{JS}</script>
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
