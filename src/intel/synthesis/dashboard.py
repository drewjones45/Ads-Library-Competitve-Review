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

from ..analysis.landing import (
    aggregate_landing_pages,
    brand_host_for,
    classify_url,
    parse_url,
)
from ..config import DATA_DIR, get_competitor
from ..storage import connect, popularity_score
from .creative_readout import (
    _LIST_ATTRS,
    _SCALAR_ATTRS,
    _distinctiveness,
    _tally,
    _whitespace_for_brand,
    pull_analyzed_creatives,
)


# Asset types surfaced in their own dashboard sections rather than the
# ad-creative gallery/tallies: page-level screenshots (homepage lane,
# landing-pages section) and Google text ads (dedicated Text-ads lane — they
# have no image and lack the visual taxonomy fields, so they'd pollute heatmaps).
EXCLUDED_GALLERY_TYPES = {"homepage_image", "landing_page_image", "text_ad"}


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


def _format_duration(sec: float | int | None) -> str:
    """Format seconds → 'M:SS' (or '' on missing/zero)."""
    if not sec:
        return ""
    s = int(round(float(sec)))
    return f"{s // 60}:{s % 60:02d}"


def _thumb_src(asset_path: str | Path, asset_type: str | None, dashboard_dir: Path) -> str:
    """For video / video_evicted creatives, the "thumbnail" must be an image
    (browsers can't render an mp4 as a static thumbnail). Pick the first frame
    file in the same directory; fall back to the asset path if no frame exists
    or the asset is already a frame."""
    p = Path(asset_path)
    if asset_type in ("video", "video_evicted") and p.suffix.lower() == ".mp4":
        frames = sorted(p.parent.glob("frame_*_t*.jpg"))
        if frames:
            return _relpath(frames[0], dashboard_dir)
    return _relpath(p, dashboard_dir)


def _video_meta_for_render(analysis_json: str | dict | None) -> dict:
    """Extract the video_meta sub-block (or {}) from an analysis JSON blob."""
    if not analysis_json:
        return {}
    if isinstance(analysis_json, str):
        try:
            analysis_json = json.loads(analysis_json)
        except Exception:
            return {}
    if not isinstance(analysis_json, dict):
        return {}
    return analysis_json.get("video_meta") or {}


def _meta_ad_url(ad_archive_id: str | None) -> str:
    """The Meta Ad Library public URL for an ad — used as the lightbox fallback
    for video_evicted creatives (no local mp4 to play)."""
    if not ad_archive_id:
        return ""
    return f"https://www.facebook.com/ads/library/?id={ad_archive_id}"


# ----- data collection ------------------------------------------------------

def _collect(conn: sqlite3.Connection, *, days: int,
             brand_ids: set[str] | None = None,
             sources: set[str] | None = None) -> dict[str, Any]:
    """Pull every shape of data the dashboard needs in one pass.

    brand_ids, when given, restricts the dashboard to that allow-list of
    competitor ids — used to split a deployment into separate dashboards (e.g.
    keep a control brand out of the main set and give it its own page). None
    means every competitor in the db (the default, unchanged behavior).

    sources, when given, scopes ads/creatives to those ad platforms
    ('meta'/'google'). The existing Meta reports pass {"meta"} so they stay
    byte-identical once Google ads share the DB; the with-Google report passes
    {"meta","google"}. None means all platforms (back-compat for direct callers).
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Platform (ad source) scope. Values are whitelisted to a fixed set, so the
    # literal SQL fragments below are safe (no user free-text). src_ads scopes
    # queries on `ads` (unaliased); src_a scopes those joining `ads a`.
    src_filter = sorted(s for s in (sources or set()) if s in ("meta", "google", "tv")) or None
    google_in_scope = (src_filter is None) or ("google" in src_filter)
    tv_in_scope = (src_filter is None) or ("tv" in src_filter)

    def _src_clause(col: str) -> str:
        if not src_filter:
            return ""
        return " AND " + col + " IN (" + ",".join("'" + s + "'" for s in src_filter) + ")"
    src_ads = _src_clause("source")
    src_a = _src_clause("a.source")

    # Optional brand allow-list. Sorted for stable SQL params; applied to the
    # competitors query plus every global (non-per-brand) pull below so the
    # whole dashboard is scoped. Per-brand loops iterate `brands` and so are
    # scoped automatically.
    brand_filter = sorted(brand_ids) if brand_ids else None

    # Brand summary
    brands = []
    brand_where = ""
    brand_where_params: list = []
    if brand_filter:
        placeholders = ",".join("?" for _ in brand_filter)
        brand_where = f" WHERE id IN ({placeholders})"
        brand_where_params = list(brand_filter)
    for r in conn.execute(
        f"SELECT id, name, vertical, priority FROM competitors{brand_where} "
        "ORDER BY priority DESC, name",
        brand_where_params,
    ).fetchall():
        cid = r["id"]
        ads_total = conn.execute(
            f"SELECT COUNT(*) c FROM ads WHERE competitor_id=?{src_ads}", (cid,)
        ).fetchone()["c"]
        ads_active = conn.execute(
            f"SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND active=1{src_ads}", (cid,)
        ).fetchone()["c"]
        # Per-platform ad totals (NOT source-scoped — the true counts, used for the
        # overview Meta-vs-Google stat and to gate the Google-only UI).
        ads_meta = conn.execute(
            "SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND source='meta'", (cid,)
        ).fetchone()["c"]
        ads_google = conn.execute(
            "SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND source='google'", (cid,)
        ).fetchone()["c"]
        ads_tv = conn.execute(
            "SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND source='tv'", (cid,)
        ).fetchone()["c"]
        # Counts the brand's PAID AD creatives only (excludes brand-store assets,
        # which get their own counts in the brand-store sub-section). Paid ads
        # are creatives where ad_id IS NOT NULL.
        creatives_total = conn.execute(
            "SELECT COUNT(*) c FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
            f"WHERE a.competitor_id=?{src_a}",
            (cid,),
        ).fetchone()["c"]
        creatives_analyzed = conn.execute(
            "SELECT COUNT(*) c FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
            f"WHERE a.competitor_id=? AND cr.analyzed_at IS NOT NULL{src_a}",
            (cid,),
        ).fetchone()["c"]
        new_ads = conn.execute(
            f"SELECT COUNT(*) c FROM ads WHERE competitor_id=? AND first_seen >= ?{src_ads}",
            (cid, since),
        ).fetchone()["c"]
        top_cta = conn.execute(
            f"SELECT cta_type, COUNT(*) n FROM ads WHERE competitor_id=? AND cta_type IS NOT NULL{src_ads} "
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
            "ads_meta": ads_meta,
            "ads_google": ads_google,
            "ads_tv": ads_tv,
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
    all_recs = pull_analyzed_creatives(sources=sources)
    if brand_filter:
        keep = set(brand_filter)
        all_recs = [r for r in all_recs if r.competitor_id in keep]
    # Paid-ad creatives, minus non-taxonomy page assets. text_ad has an ad_id
    # (so ad_id>0 alone wouldn't drop it) but points at a JSON sidecar, not an
    # image, and lacks the visual taxonomy — excluding it keeps the per-brand
    # gallery and cross-set heatmaps image/video-only. (homepage/landing assets
    # are ad_id NULL, already excluded.)
    ad_recs = [r for r in all_recs if (r.ad_id or 0) > 0 and r.asset_type not in EXCLUDED_GALLERY_TYPES]
    # Page-level assets (whole-page screenshots) don't follow the ad-creative
    # taxonomy and have their own dashboard homes (homepage lane, landing-pages
    # section), so keep them out of the "Browse all creatives" gallery and the
    # brand-vs-brand tallies. Cross-set/distinctiveness/whitespace already
    # exclude them via `ad_recs` (ad_id NULL).
    gallery_recs = [r for r in all_recs if r.asset_type not in EXCLUDED_GALLERY_TYPES]

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
            f"FROM ads WHERE competitor_id=? AND first_seen >= ?{src_ads} "
            "ORDER BY first_seen DESC LIMIT 50",
            (cid, since),
        ).fetchall()
        recent_ads[cid] = [dict(r) for r in rows]

    # Top-served ads per brand — ranked by the popularity_score proxy
    # (SERP rank + run duration + active bonus). See storage.popularity_score
    # for the formula + honesty limits. Window is the entire competitor history,
    # not the last N days — the popularity signal is most informative when
    # comparing across an ad's lifetime, not just the recency window.
    top_ads: dict[str, list] = {}
    for cid in [b["id"] for b in brands]:
        # brand_max_rank = the largest rank seen for this brand across all
        # observations. Used to normalize so rank-N is "bottom of the listing"
        # regardless of how many cards Meta returned.
        max_rank_row = conn.execute(
            "SELECT MAX(serp_position_rank) FROM ads WHERE competitor_id=? "
            "AND serp_position_rank IS NOT NULL",
            (cid,),
        ).fetchone()
        brand_max_rank = (max_rank_row[0] or 0) if max_rank_row else 0
        # Per-brand candidate ads with a preferred creative joined in. Prefer a
        # video creative if one exists (richer; the thumbnail logic picks the
        # first frame); fall back to the first image creative.
        rows = conn.execute(
            "SELECT a.id, a.ad_archive_id, a.first_seen, a.last_seen, a.start_date, "
            "a.active, a.body_text, a.cta_type, a.link_url, a.page_name, "
            "a.serp_position_rank, "
            "COALESCE(c_video.asset_path, c_img.asset_path) AS thumb_path, "
            "COALESCE(c_video.asset_type, c_img.asset_type) AS thumb_asset_type, "
            "COALESCE(c_video.analysis_json, c_img.analysis_json) AS thumb_analysis_json "
            "FROM ads a "
            "LEFT JOIN creatives c_video ON c_video.id = ("
            "  SELECT id FROM creatives WHERE ad_id=a.id "
            "  AND asset_type IN ('video','video_evicted') ORDER BY id LIMIT 1) "
            "LEFT JOIN creatives c_img ON c_img.id = ("
            "  SELECT id FROM creatives WHERE ad_id=a.id "
            "  AND asset_type='image' ORDER BY id LIMIT 1) "
            f"WHERE a.competitor_id=?{src_a}",
            (cid,),
        ).fetchall()
        scored = []
        for r in rows:
            d = dict(r)
            d["popularity_score"] = popularity_score(
                d.get("serp_position_rank"),
                d.get("start_date"),
                d.get("last_seen"),
                d.get("active") or 0,
                brand_max_rank,
            )
            # Run duration in days, for the dashboard chip.
            d["run_days"] = 0
            if d.get("start_date") and d.get("last_seen"):
                try:
                    start = datetime.fromisoformat(d["start_date"][:10])
                    end = datetime.fromisoformat(d["last_seen"][:10])
                    d["run_days"] = max((end - start).days, 0)
                except (ValueError, TypeError):
                    pass
            scored.append(d)
        scored.sort(key=lambda d: d["popularity_score"], reverse=True)
        top_ads[cid] = scored[:12]

    # Landing-page distribution per brand — "where do this brand's ads send
    # traffic?" Reuses popularity_score (rank × duration × active bonus) as the
    # per-section weight so the "By popularity" toggle in the dashboard can hot-
    # swap section widths against the unweighted ad-count view. The classifier
    # buckets template_unfilled and off_brand_tracker count as findings, not
    # noise — surface them so the campaign-ops bugs they represent stay visible.
    # `ad_landing_by_id` is a side-table used a few blocks below to enrich
    # creatives_index with utm/section fields per ad-linked creative.
    landing_by_brand: dict[str, dict] = {}
    ad_landing_by_id: dict[int, dict] = {}
    for cid in [b["id"] for b in brands]:
        comp = get_competitor(cid)
        brand_host = brand_host_for(comp) if comp else ""
        rows = conn.execute(
            "SELECT a.id, a.ad_archive_id, a.link_url, a.serp_position_rank, "
            "a.start_date, a.last_seen, a.active "
            f"FROM ads a WHERE a.competitor_id=? AND a.link_url IS NOT NULL{src_a}",
            (cid,),
        ).fetchall()
        max_rank_row = conn.execute(
            "SELECT MAX(serp_position_rank) FROM ads WHERE competitor_id=? "
            "AND serp_position_rank IS NOT NULL",
            (cid,),
        ).fetchone()
        brand_max_rank = (max_rank_row[0] or 0) if max_rank_row else 0
        enriched: list[dict] = []
        for r in rows:
            parsed = parse_url(r["link_url"])
            bucket = classify_url(parsed, brand_host)
            score = popularity_score(
                r["serp_position_rank"], r["start_date"], r["last_seen"],
                r["active"] or 0, brand_max_rank,
            )
            enriched.append({
                "id": r["id"],
                "ad_archive_id": r["ad_archive_id"],
                "link_url": r["link_url"],
                "section": bucket,
                "popularity_score": score,
                **parsed,
            })
            ad_landing_by_id[r["id"]] = {
                "section": bucket,
                "utm": parsed["utm"],
                "clean_url": parsed["clean_url"],
            }
        landing_by_brand[cid] = aggregate_landing_pages(enriched, brand_host)

    # Landing-page screenshots (captured by `intel capture-landing-pages`): join
    # each to its destination URL + analysis so the "where ads send traffic"
    # rows can show the screenshot + a one-line read. The clean_url lives in the
    # screenshot's `landing_meta.json` sidecar (creatives has no url column).
    landing_screens_by_brand: dict[str, dict[str, dict]] = {}
    for cid in [b["id"] for b in brands]:
        rows = conn.execute(
            "SELECT asset_path, analysis_json FROM creatives "
            "WHERE competitor_id=? AND asset_type='landing_page_image'",
            (cid,),
        ).fetchall()
        by_url: dict[str, dict] = {}
        for r in rows:
            sidecar = Path(r["asset_path"]).parent / "landing_meta.json"
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:
                continue
            clean_url = meta.get("clean_url")
            if not clean_url:
                continue
            try:
                analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else {}
            except Exception:
                analysis = {}
            by_url[clean_url] = {
                "asset_path": r["asset_path"],
                "analyzed": bool(r["analysis_json"]),
                "summary": analysis.get("summary_one_line"),
                "analysis": analysis,
            }
        if by_url:
            landing_screens_by_brand[cid] = by_url
            # Enrich the aggregated top_url rows in place so both v1 and v2
            # renderers get the screenshot + analysis with no extra params.
            for section in (landing_by_brand.get(cid, {}).get("by_section") or []):
                for tu in section.get("top_urls") or []:
                    hit = by_url.get(tu.get("clean_url"))
                    if hit:
                        tu["screenshot_path"] = hit["asset_path"]
                        tu["summary"] = hit["summary"]
                        tu["analysis"] = hit["analysis"]

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

    # Homepage data per brand: latest whole-page screenshot + latest hero promo
    # (Site Content Analysis). Individual homepage page-images are no longer
    # collected — only the single whole-page screenshot is surfaced. Empty dict
    # for brands with no website-source activity yet.
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
        if not latest_obs and not latest_promo:
            continue
        screenshot_path = None
        hero_image_path = None
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
            except Exception:
                pass
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
            "latest_promo": promo_dict,
            "blocked": blocked,
            "block_vendor": block_vendor,
        }

    # Google text ads (asset_type='text_ad') get a dedicated per-brand "Text ads"
    # lane — no image, just classified headlines + descriptions. Built ONLY when
    # google is in scope, so the Meta-only builds emit no Text-ads lane and stay
    # byte-identical. Prefers the classified analysis_json, falling back to the
    # raw text_ad_meta.json sidecar for any not-yet-analyzed ad.
    text_ads_by_brand: dict[str, list] = {}
    if google_in_scope:
        for cid in [b["id"] for b in brands]:
            rows = conn.execute(
                "SELECT cr.asset_path, cr.analysis_json, cr.analyzed_at, "
                "a.ad_archive_id, a.link_url, a.start_date, a.last_seen, a.page_name "
                "FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
                "WHERE a.competitor_id=? AND cr.asset_type='text_ad' AND a.source='google' "
                "ORDER BY a.last_seen DESC LIMIT 60",
                (cid,),
            ).fetchall()
            items = []
            for r in rows:
                try:
                    analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else {}
                except Exception:
                    analysis = {}
                meta = {}
                try:
                    meta = json.loads(Path(r["asset_path"]).read_text())
                except Exception:
                    pass
                headlines = analysis.get("headlines") or [{"text": h} for h in (meta.get("headlines") or [])]
                descriptions = analysis.get("descriptions") or [{"text": d} for d in (meta.get("descriptions") or [])]
                items.append({
                    "ad_archive_id": r["ad_archive_id"],
                    "link_url": r["link_url"],
                    "start_date": r["start_date"],
                    "last_seen": r["last_seen"],
                    "headlines": headlines,
                    "descriptions": descriptions,
                    "sale_status": analysis.get("sale_status"),
                    "offer_kind": analysis.get("offer_kind"),
                    "offer_value": analysis.get("offer_value"),
                    "hook_style": analysis.get("hook_style"),
                    "emotional_vs_rational": analysis.get("emotional_vs_rational"),
                    "value_props": analysis.get("value_props") or [],
                    "primary_cta": analysis.get("primary_cta"),
                    "summary": analysis.get("summary_one_line"),
                    "analyzed": bool(r["analyzed_at"]),
                })
            if items:
                text_ads_by_brand[cid] = items

    # iSpot TV spots (asset_type='tv_spot') get a dedicated per-brand "TV ads"
    # lane: the spot thumbnail + (when analyzed) creative classification, plus a
    # brand-level media-weight scorecard. Built ONLY when tv is in scope, so
    # Meta/Google builds emit no TV lane and stay byte-identical. The per-ad and
    # brand metrics ride in the ad's raw_json (written by the runner).
    tv_ads_by_brand: dict[str, list] = {}
    tv_metrics_by_brand: dict[str, dict] = {}
    if tv_in_scope:
        for cid in [b["id"] for b in brands]:
            rows = conn.execute(
                "SELECT cr.asset_path, cr.analysis_json, cr.analyzed_at, "
                "a.ad_archive_id, a.link_url, a.first_seen, a.last_seen, a.raw_json "
                "FROM creatives cr JOIN ads a ON a.id=cr.ad_id "
                "WHERE a.competitor_id=? AND cr.asset_type='tv_spot' AND a.source='tv' "
                "ORDER BY a.last_seen DESC LIMIT 60",
                (cid,),
            ).fetchall()
            items = []
            for r in rows:
                try:
                    analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else {}
                except Exception:
                    analysis = {}
                try:
                    raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
                except Exception:
                    raw = {}
                if not tv_metrics_by_brand.get(cid) and raw.get("brand_metrics"):
                    tv_metrics_by_brand[cid] = raw["brand_metrics"]
                items.append({
                    "ad_archive_id": r["ad_archive_id"],
                    "asset_path": r["asset_path"],
                    "ispot_url": raw.get("ispot_url") or r["link_url"],
                    "video_url": raw.get("video_url"),
                    "title": raw.get("title"),
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "summary": analysis.get("summary_one_line") or raw.get("title"),
                    "hook_style": analysis.get("hook_style"),
                    "products": analysis.get("products_visible") or [],
                    "key_features": analysis.get("key_features") or [],
                    "analyzed": bool(r["analyzed_at"]),
                })
            if items:
                tv_ads_by_brand[cid] = items

    # Latest briefing
    latest_briefing = conn.execute(
        "SELECT id, title, body_md, created_at FROM briefings "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    # --- payloads for client-side features (filter, brand-vs-brand, delta view) ---
    creatives_index: list[dict] = []
    for rec in gallery_recs:
        a = rec.analysis
        # Derive a 'layoutFlags' list from the brand-store-ish booleans so they
        # can be filtered as chips alongside other arrays.
        layout_flags = []
        if a.get("before_after_present"):       layout_flags.append("before_after")
        if a.get("shoppable_imagery"):          layout_flags.append("shoppable_imagery")
        if a.get("hero_banner_present"):        layout_flags.append("hero_banner")
        if a.get("category_nav_visible"):       layout_flags.append("category_nav")

        vmeta = a.get("video_meta") or {}
        landing_info = ad_landing_by_id.get(rec.ad_id or 0) or {}
        utm_info = landing_info.get("utm") or {}
        creatives_index.append({
            "id": rec.ad_id,
            "comp": rec.competitor_id,
            "compName": rec.competitor_name,
            "adId": rec.ad_archive_id,
            "imgPath": str(rec.asset_path),  # absolute; relativized in JS at render time
            "assetType": rec.asset_type or "image",
            "platform": rec.source,
            "videoDurationSec": vmeta.get("duration_sec"),
            # No Meta Ad Library link for Google creatives (it would be wrong).
            "metaAdUrl": _meta_ad_url(rec.ad_archive_id) if rec.source != "google" else "",
            "landingSection": landing_info.get("section"),
            "landingCleanUrl": landing_info.get("clean_url"),
            "utmSource": utm_info.get("utm_source"),
            "utmMedium": utm_info.get("utm_medium"),
            "utmCampaign": utm_info.get("utm_campaign"),
            "utmContent": utm_info.get("utm_content"),
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
    ads_q = ("SELECT competitor_id, ad_archive_id, first_seen, body_text, cta_type, link_url "
             "FROM ads WHERE first_seen >= ?")
    ads_params: list = [wide_since]
    if brand_filter:
        placeholders = ",".join("?" for _ in brand_filter)
        ads_q += f" AND competitor_id IN ({placeholders})"
        ads_params += list(brand_filter)
    ads_q += src_ads
    ads_q += " ORDER BY first_seen DESC LIMIT 2000"
    for r in conn.execute(ads_q, ads_params).fetchall():
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
    for rec in gallery_recs:
        all_by_comp_for_bvb.setdefault(rec.competitor_id, []).append(rec)
    bvb_comp_tallies = {cid: _tally(recs) for cid, recs in all_by_comp_for_bvb.items()}

    # WEIGHTED variant: tally only paid-ad creatives, weighted by popularity_score.
    # Homepage / brand-store / website creatives have ad_id == 0 and drop out — the
    # popularity signal is undefined for them. UI label this "By popularity
    # (Meta ads only)" so users know what they're looking at.
    bvb_comp_tallies_weighted = {}
    for cid, recs in all_by_comp_for_bvb.items():
        ad_recs_only = [r for r in recs if r.ad_id]
        if not ad_recs_only:
            bvb_comp_tallies_weighted[cid] = _tally([])  # empty tally with n_total=0
            continue
        # brand_max_rank derived from the same ad pool we're weighting over.
        ranks = [r.serp_position_rank for r in ad_recs_only if r.serp_position_rank is not None]
        brand_max_rank = max(ranks) if ranks else 0

        def _weight(rec, _max=brand_max_rank):
            return popularity_score(
                rec.serp_position_rank, rec.start_date, rec.last_seen,
                rec.active, _max,
            )
        bvb_comp_tallies_weighted[cid] = _tally(ad_recs_only, weight_fn=_weight)

    # Tallies serialized for brand-vs-brand JS rendering.
    def _ser(t):
        return {
            "n": t.n_total,
            "scalar": {k: dict(v) for k, v in t.scalar.items()},
            "boolean": {k: dict(v) for k, v in t.boolean.items()},
            "listed": {k: dict(v) for k, v in t.listed.items()},
        }
    brand_tallies_ser = {cid: _ser(t) for cid, t in bvb_comp_tallies.items()}
    brand_tallies_weighted_ser = {cid: _ser(t) for cid, t in bvb_comp_tallies_weighted.items()}
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
        "top_ads": top_ads,
        "landing_by_brand": landing_by_brand,
        "landing_screens_by_brand": landing_screens_by_brand,
        "brand_store_by_brand": brand_store_by_brand,
        "homepage_by_brand": homepage_by_brand,
        "text_ads_by_brand": text_ads_by_brand,
        "google_in_scope": google_in_scope,
        "tv_ads_by_brand": tv_ads_by_brand,
        "tv_metrics_by_brand": tv_metrics_by_brand,
        "tv_in_scope": tv_in_scope,
        "latest_briefing": dict(latest_briefing) if latest_briefing else None,
        # client-side payloads
        "client_creatives": creatives_index,
        "client_ads": ads_index,
        "client_tallies": brand_tallies_ser,
        "client_tallies_weighted": brand_tallies_weighted_ser,
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

/* Landing pages — "where ads send traffic" */
.lp-toolbar { display: flex; align-items: center; gap: 12px; margin: 4px 0 14px;
              font-size: 12px; color: #666; }
.lp-toolbar .lp-mode { display: inline-flex; gap: 0; border: 1px solid #cfd5dd;
              border-radius: 6px; overflow: hidden; }
.lp-toolbar .lp-mode label { padding: 5px 11px; cursor: pointer;
              background: #f7f8fa; color: #444; transition: background .12s ease; }
.lp-toolbar .lp-mode label:hover { background: #eef0f3; }
.lp-toolbar .lp-mode label:has(input:checked) { background: #1f2c4a; color: #fff; }
.lp-toolbar .lp-mode input { position: absolute; opacity: 0; pointer-events: none; }
.lp-brand-card { border: 1px solid #e6e8ec; border-radius: 6px; padding: 14px 16px;
              margin: 10px 0 16px; background: #fff; }
.lp-brand-card.empty { background: #fafafa; color: #888; font-style: italic; }
.lp-brand-head { display: flex; align-items: baseline; justify-content: space-between;
              gap: 14px; margin-bottom: 10px; }
.lp-brand-head .lp-brand { font-weight: 600; font-size: 14px; color: #1c1c1c; }
.lp-brand-head .lp-stats { color: #777; font-size: 12px; font-variant-numeric: tabular-nums; }
.lp-bar { width: 100%; height: 22px; background: #f0f1f4; border-radius: 4px;
              overflow: hidden; display: block; }
.lp-bar rect { transition: width .25s ease, x .25s ease; }
.lp-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px;
              font-size: 11px; color: #555; }
.lp-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.lp-legend-swatch { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
.lp-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
.lp-table th { text-align: left; color: #666; font-weight: 600; font-size: 11px;
              text-transform: uppercase; letter-spacing: 0.4px; padding: 5px 6px;
              border-bottom: 1px solid #e6e8ec; }
.lp-table th.num { text-align: right; }
.lp-table td { padding: 6px; border-bottom: 1px solid #f1f2f4; vertical-align: top; }
.lp-table td.num { text-align: right; font-variant-numeric: tabular-nums; color: #444; }
.lp-table td.url { font-family: ui-monospace,Menlo,Consolas,monospace; font-size: 11px;
              color: #2a5fb0; word-break: break-all; max-width: 320px; }
.lp-table tr.flagged td.section { color: #a93226; font-weight: 600; }
.lp-section-pill { display: inline-block; width: 8px; height: 8px; border-radius: 2px;
              vertical-align: middle; margin-right: 6px; }
.lp-findings { margin-top: 10px; padding: 9px 12px; background: #fff8e6;
              border-left: 3px solid #e08e00; border-radius: 0 4px 4px 0;
              font-size: 12px; color: #5a3e00; }
.lp-findings.danger { background: #fdf4f4; border-left-color: #d9534f; color: #6a1f1c; }
.lp-findings code { background: rgba(0,0,0,0.05); padding: 0 4px; border-radius: 2px; }

/* Ads list */
.ad { display: grid; grid-template-columns: 116px 1fr 110px; gap: 12px; padding: 10px 0;
      border-bottom: 1px solid #eef0f3; font-size: 12px; align-items: start; }
.ad .id { font-family: ui-monospace,Menlo,Consolas,monospace; color: #666; min-width: 0;
          overflow-wrap: anywhere; word-break: break-all; font-size: 11px; line-height: 1.5; }
.ad .body { color: #1c1c1c; min-width: 0; overflow-wrap: anywhere; }
.ad .cta { color: #2a5fb0; font-weight: 600; text-align: right; }

/* Briefing */
.briefing { white-space: pre-wrap; font-size: 13px; line-height: 1.6; }
.briefing h1,.briefing h2,.briefing h3 { font-weight: 600; }
/* Strategy-report link (replaces briefing when --strategy-doc is set) */
.strategy-cta { padding: 2px 0 4px; }
.strategy-links { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.strategy-btn { display: inline-block; padding: 10px 18px; border-radius: 6px; background: #2a5fb0;
                color: #fff; font-weight: 600; font-size: 13px; text-decoration: none; }
.strategy-btn:hover { background: #244f96; }
.strategy-btn.ghost { background: transparent; color: #2a5fb0; border: 1px solid #2a5fb0; }
.strategy-btn.ghost:hover { background: #f0f4fb; }

/* Lightbox */
#lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 100;
            align-items: center; justify-content: center; cursor: zoom-out; }
#lightbox.open { display: flex; }
#lightbox img, #lightbox video { max-width: 92%; max-height: 92%; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
#lightbox .meta-cta { position: absolute; bottom: 24px; right: 24px; background: #1f2c4a;
                      color: white; padding: 10px 16px; border-radius: 6px; font-size: 13px;
                      font-weight: 600; text-decoration: none; cursor: pointer; }
#lightbox .meta-cta:hover { background: #2a5fb0; }

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
  color: white; font-size: 10px; font-weight: 700; padding: 1px 5px;
  border-radius: 2px; letter-spacing: 0.3px; pointer-events: none;
}

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
  if ((c.assetType === 'video' || c.assetType === 'video_evicted')
      && c.imgPath && c.imgPath.endsWith('.mp4')) {
    // We don't know the exact frame timestamp from the client; the simplest
    // approach is to swap `video.mp4` → `frame_00_t00000.jpg`. Padding is
    // fixed-width in process_video (idx:02d, ms:05d) so this works for the
    // canonical first frame. If extraction shifted the leading frame, the
    // dashboard falls back to the imgPath (which is the mp4 — browsers
    // show a play-icon placeholder).
    return c.imgPath.replace(/video\\.mp4$/, 'frame_00_t00000.jpg');
  }
  return c.imgPath;
}

function renderFilterGallery() {
  const container = document.getElementById('filter-gallery-grid');
  if (!container) return;
  const matched = DATA.client_creatives.filter(matchesFilters);
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
    // Platform (Meta/Google) filter — only present in the with-Google report;
    // the Meta-only build sets google_in_scope=false so it never renders.
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


def _render_landing_screenshot(tu: dict, dashboard_dir: Path) -> str:
    """Thumbnail + one-line read + expandable analysis for a captured landing
    page, shown beneath its destination URL in the landing-pages table."""
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
            rows.append(f'<div style="margin:3px 0"><span style="color:#777">{label}:</span> {_esc(v)}</div>')
    for label, key in [("Trust", "trust_signals"), ("Friction", "friction_points")]:
        items = a.get(key) or []
        if items:
            rows.append(f'<div style="margin:3px 0"><span style="color:#777">{label}:</span> {_esc(", ".join(items[:4]))}</div>')

    def _ww(label, key, color):
        items = a.get(key) or []
        if not items:
            return ""
        lis = "".join(f"<li>{_esc(x)}</li>" for x in items[:3])
        return (f'<div style="margin-top:6px"><span style="font-weight:600;color:{color}">{label}</span>'
                f'<ul style="margin:2px 0 0 16px;padding:0">{lis}</ul></div>')
    detail = "".join(rows) + _ww("What works", "what_works", "#1e7e34") + _ww("What it misses", "what_misses", "#c8242b")
    if detail:
        parts.append(
            f'<details style="margin-top:6px"><summary style="cursor:pointer;color:#2a5fb0;'
            f'font-size:11px">Landing-page analysis</summary>'
            f'<div style="margin-top:6px;font-size:11px;line-height:1.5">{detail}</div></details>'
        )
    return f'<div style="margin-top:8px">{"".join(parts)}</div>'


def _render_landing_pages_section(data: dict, dashboard_dir: Path) -> str:
    """Per-brand 'where ads send traffic' breakdown — stacked horizontal bar +
    drill-down table. The Count/Popularity toggle hot-swaps section widths in
    JS using data attributes set on each <rect>. Server-side default is Count
    mode (visible widths reflect ad_share). See aggregate_landing_pages for the
    upstream shape; SECTION_PALETTE / SECTION_LABELS / SECTION_CALLOUTS for the
    display tables."""
    from ..analysis.landing import SECTION_CALLOUTS, SECTION_LABELS, SECTION_PALETTE
    landing_by_brand = data.get("landing_by_brand") or {}
    if not landing_by_brand:
        return '<p class="muted">No ad link_urls collected yet — run ingest.</p>'

    # Toolbar: count/popularity radios. Legend is per-brand (sections vary),
    # but the toolbar lives at the section level.
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
        # ad_share (count mode).
        viewbox_w = 1000
        rects = []
        legend_items = []
        x_count = 0.0
        x_pop = 0.0
        for s in by_section:
            sec = s["section"]
            color = SECTION_PALETTE.get(sec, "#9a9a9a")
            label = SECTION_LABELS.get(sec, sec)
            count_w = s["ad_share"] * viewbox_w
            pop_w = s["popularity_share"] * viewbox_w
            rects.append(
                f'<rect data-section="{_esc(sec)}" '
                f'data-count-x="{x_count:.2f}" data-count-w="{count_w:.2f}" '
                f'data-pop-x="{x_pop:.2f}" data-pop-w="{pop_w:.2f}" '
                f'x="{x_count:.2f}" y="0" width="{count_w:.2f}" height="22" '
                f'fill="{color}">'
                f'<title>{_esc(label)} — {s["ad_count"]} ads '
                f'({s["ad_share"]:.0%} of links · {s["popularity_share"]:.0%} pop-weighted)</title>'
                f'</rect>'
            )
            legend_items.append(
                f'<span class="lp-legend-item">'
                f'<span class="lp-legend-swatch" style="background:{color}"></span>'
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
            color = SECTION_PALETTE.get(sec, "#9a9a9a")
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
                    screenshot_html = _render_landing_screenshot(tu, dashboard_dir)
            elif s.get("example_raw_urls"):
                # Template-unfilled URLs have no clean_url — show the raw form.
                ex = _esc(s["example_raw_urls"][0])[:140]
                top_url_html = f'<span class="muted">{ex}</span>'
            rows_html.append(
                f'<tr class="lp-row{flagged_cls}" data-section="{_esc(sec)}">'
                f'<td class="section">'
                f'<span class="lp-section-pill" style="background:{color}"></span>'
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
          <div class="label">Last captured</div><div class="value" style="font-size:14px">{_esc(observed[:16].replace('T', ' '))}</div></div>
      </div>
      <div>{screenshot_html}</div>
    </div>
    """


def _render_most_served_snapshot(brands: list, top_ads_by_brand: dict, dashboard_dir: Path) -> str:
    """A header-strip view showing the rank-1 ad per brand. Intentionally simple:
    one thumbnail, brand name, rank/days chips. Brands without ranked ads show '—'."""
    cards = []
    for b in brands:
        cid = b["id"]
        top = top_ads_by_brand.get(cid) or []
        first = top[0] if top else None
        if not first or first.get("popularity_score", 0) <= 0:
            cards.append(f"""
            <a href="#brand-{_esc(cid)}" style="background:#f4f7fb;border:1px solid #d8dee5;
                       border-radius:5px;padding:10px 12px;display:flex;flex-direction:column;
                       gap:6px;text-decoration:none;color:#1f2c4a;min-width:0">
              <div style="font-size:11px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;
                          color:#1f2c4a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {_esc(b['name'])}</div>
              <div style="background:#e6e8ec;color:#666;aspect-ratio:1.4/1;border-radius:3px;
                          display:flex;align-items:center;justify-content:center;font-size:24px">—</div>
              <div class="muted" style="font-size:10px">no ranked ads</div>
            </a>
            """)
            continue
        thumb_html = ''
        asset_type = first.get("thumb_asset_type") or "image"
        vmeta = _video_meta_for_render(first.get("thumb_analysis_json"))
        dur_chip = (
            f'<span class="dur-chip">{_format_duration(vmeta.get("duration_sec"))}</span>'
            if vmeta.get("duration_sec") else ''
        )
        if first.get("thumb_path"):
            thumb_rel = _thumb_src(first["thumb_path"], asset_type, dashboard_dir)
            thumb_html = (
                f'<span class="thumb-wrap" data-asset-type="{_esc(asset_type)}" '
                f'style="display:block">'
                f'<img src="{thumb_rel}" loading="lazy" alt="top served ad" '
                f'style="width:100%;aspect-ratio:1.4/1;object-fit:cover;border-radius:3px">'
                f'{dur_chip}'
                f'</span>'
            )
        else:
            thumb_html = (
                '<div style="background:#e6e8ec;color:#666;aspect-ratio:1.4/1;border-radius:3px;'
                'display:flex;align-items:center;justify-content:center;font-size:11px">'
                'no thumbnail</div>'
            )
        rank = first.get("serp_position_rank")
        rank_label = f"#{int(rank)+1}" if rank is not None else "—"
        days = int(first.get("run_days") or 0)
        cards.append(f"""
        <a href="#brand-{_esc(cid)}" style="background:white;border:1px solid #d8dee5;
                   border-radius:5px;padding:10px 12px;display:flex;flex-direction:column;
                   gap:6px;text-decoration:none;color:#1f2c4a;min-width:0">
          <div style="font-size:11px;font-weight:700;letter-spacing:0.6px;text-transform:uppercase;
                      color:#1f2c4a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            {_esc(b['name'])}</div>
          {thumb_html}
          <div style="display:flex;gap:4px;flex-wrap:wrap">
            <span style="background:#0a3818;color:#d4f057;font-size:10px;font-weight:700;
                         letter-spacing:0.5px;padding:2px 6px;border-radius:3px">RANK {rank_label}</span>
            <span style="background:#eaf2ff;color:#1f4a8a;font-size:10px;font-weight:700;
                         letter-spacing:0.5px;padding:2px 6px;border-radius:3px">{days}d</span>
          </div>
        </a>
        """)
    # margin-left:240px aligns with the fixed-position nav (see CSS for header/main).
    # max-width:1320px matches `main` so the strip aligns visually with sections below.
    return f"""
    <section id="most-served" style="margin-left:240px;background:#fafbfc;
                                     border-bottom:1px solid #e6e8ec;padding:14px 28px;
                                     max-width:1320px;margin-right:auto">
      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
                  color:#666;margin-bottom:10px">Most-served snapshot
        <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">
          — each brand's top-scoring ad by popularity proxy (SERP rank × run duration, intra-brand only)
        </span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px">
        {''.join(cards)}
      </div>
    </section>
    """


def _render_top_served_panel(top_ads: list, dashboard_dir: Path) -> str:
    """Render the per-brand 'Top served ads' panel. See storage.popularity_score
    for the underlying signal. Intentionally intra-brand only — do not infer
    cross-brand ranking from these cards."""
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
        rank_chip = (
            f'<span style="background:#0a3818;color:#d4f057;font-size:10px;'
            f'font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px">'
            f'RANK #{int(rank)+1}</span>'
            if rank is not None
            else (
                '<span style="background:#f0f2f5;color:#666;font-size:10px;'
                'font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px">'
                'RANK —</span>'
            )
        )
        days_chip = (
            f'<span style="background:#eaf2ff;color:#1f4a8a;font-size:10px;'
            f'font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px">'
            f'{int(ad["run_days"])}d RUNNING</span>'
            if ad.get("run_days")
            else ''
        )
        active = ad.get("active") or 0
        active_chip = (
            '<span style="background:#e6f4ea;color:#1e7e34;font-size:10px;'
            'font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px">ACTIVE</span>'
            if active
            else '<span style="background:#fbe9e7;color:#a13b1e;font-size:10px;'
                 'font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px">INACTIVE</span>'
        )
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
                f'data-ad-id="{_esc(ad["ad_archive_id"])}" '
                f'style="display:block">'
                f'<img src="{thumb_rel}" loading="lazy" alt="ad {_esc(ad["ad_archive_id"])}" '
                f'style="width:100%;aspect-ratio:1.4/1;object-fit:cover;border-radius:3px;'
                f'background:#f0f2f5">'
                f'{dur_chip}'
                f'</a>'
            )
        cta_html = (
            f'<a href="{_esc(link)}" target="_blank" '
            f'style="color:#1f2c4a;text-decoration:none;font-weight:600">{_esc(cta)}</a>'
            if link
            else _esc(cta)
        )
        cards.append(f"""
        <div style="background:white;border:1px solid #e6e8ec;border-radius:4px;
                    padding:10px;display:flex;flex-direction:column;gap:8px">
          {thumb_html}
          <div style="display:flex;gap:4px;flex-wrap:wrap">{rank_chip}{days_chip}{active_chip}</div>
          <div style="font-size:12.5px;line-height:1.4;color:#1f2c4a;flex:1">{_esc(body)}</div>
          <div style="font-size:11px;color:#666"><span class="muted">CTA</span> {cta_html}</div>
          <div style="font-size:10px;color:#999;font-family:monospace">{_esc(ad['ad_archive_id'])}</div>
        </div>
        """)
    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));'
        f'gap:10px">{"".join(cards)}</div>'
    )


def _render_text_ads_panel(text_ads: list, dashboard_dir: Path) -> str:
    """Per-brand Google text-ads cards: headlines (Titles) and descriptions
    (Body copy) with per-component classification chips + an ad-level rollup.
    Text ads have no image — this is their dedicated home."""
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
        <div class="ad" style="display:block;padding:12px 14px;margin-bottom:10px;border:1px solid #e3e3e3;border-radius:6px;background:white">
          <div class="summary" style="font-weight:600;margin-bottom:6px">{_esc(t.get("summary") or "(text ad)")}{unanalyzed}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
            <div>
              <div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Titles</div>
              <ul style="margin:0;padding-left:16px;font-size:12px">{"".join(h_rows) or '<li class="muted">—</li>'}</ul>
            </div>
            <div>
              <div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Body copy</div>
              <ul style="margin:0;padding-left:16px;font-size:12px">{"".join(d_rows) or '<li class="muted">—</li>'}</ul>
            </div>
          </div>
          <div class="tags" style="margin-top:8px">{"".join(rollup)}</div>
          <div class="muted" style="margin-top:4px">ad <code>{_esc(t.get("ad_archive_id"))}</code>{link_html}</div>
        </div>
        """)
    return "".join(cards)


def _fmt_metric(v) -> str:
    """Format a brand-level TV metric, or 'API only' when iSpot masks it for
    anonymous scraping (the value comes through as None)."""
    if v is None:
        return '<span class="muted" style="font-size:12px">API only</span>'
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}"


def _render_tv_spots_panel(tv_ads: list, dashboard_dir: Path) -> str:
    """Per-brand iSpot TV-spot cards: spot thumbnail (links to iSpot), title,
    and — once analyzed — creative classification chips. The full mp4 is linked
    when captured. Per-ad spend/airings are iSpot-API-only; brand-level media
    weight is shown in the lane header."""
    if not tv_ads:
        return ""
    cards = []
    for t in tv_ads[:24]:
        thumb_rel = _thumb_src(t.get("asset_path"), "image", dashboard_dir) if t.get("asset_path") else ""
        ispot = t.get("ispot_url")
        chips = []
        if t.get("hook_style"):
            chips.append(f'<span class="tag">{_esc(t["hook_style"])}</span>')
        for p in (t.get("products") or [])[:2]:
            chips.append(f'<span class="tag prod">{_esc(p)}</span>')
        for f in (t.get("key_features") or [])[:3]:
            chips.append(f'<span class="tag kf">{_esc(f)}</span>')
        unanalyzed = "" if t.get("analyzed") else ' <span class="muted">(unanalyzed)</span>'
        img_html = (
            f'<img src="{thumb_rel}" loading="lazy" alt="TV spot {_esc(t.get("ad_archive_id"))}" '
            f'style="width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px;background:#eee">'
            if thumb_rel else ""
        )
        if ispot:
            img_html = f'<a href="{_esc(ispot)}" target="_blank">{img_html}</a>'
        vid = t.get("video_url")
        links = []
        if ispot:
            links.append(f'<a href="{_esc(ispot)}" target="_blank">iSpot</a>')
        if vid:
            links.append(f'<a href="{_esc(vid)}" target="_blank">video</a>')
        links_html = (" · " + " · ".join(links)) if links else ""
        cards.append(f"""
        <div class="creative" style="border:1px solid #e3e3e3;border-radius:6px;overflow:hidden;background:white">
          {img_html}
          <div class="body" style="padding:8px 10px">
            <div class="summary" style="font-weight:600;font-size:13px">{_esc(t.get("title") or "(TV spot)")}{unanalyzed}</div>
            <div class="tags" style="margin-top:6px">{"".join(chips)}</div>
            <div class="muted" style="margin-top:4px;font-size:11px">spot <code>{_esc(t.get("ad_archive_id"))}</code>{links_html}</div>
          </div>
        </div>
        """)
    return f'<div class="gallery" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px">{"".join(cards)}</div>'


def _render_brand_section(brand: dict, recs: list, recent_ads: list, dashboard_dir: Path,
                           bs_data: dict | None = None,
                           hp_data: dict | None = None,
                           top_ads: list | None = None,
                           text_ads: list | None = None,
                           tv_ads: list | None = None,
                           tv_metrics: dict | None = None) -> str:
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
                data-ad-id="{_esc(rec.ad_archive_id)}"
                style="display:block">
            <img src="{thumb_rel}" loading="lazy" alt="ad {rec.ad_archive_id}">
            {dur_chip}
          </span>
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
    top_served_html = _render_top_served_panel(top_ads or [], dashboard_dir)
    # Google text-ads lane — only rendered when this brand has Google text ads
    # (so the Meta-only report shows no Google lane at all).
    text_ads_panel = _render_text_ads_panel(text_ads or [], dashboard_dir)
    google_lane_html = ""
    if text_ads_panel:
        google_lane_html = f"""
      <div class="lane lane-google" style="background:#fdf8ef;border-left:4px solid #e0992a;
                                            border-radius:6px;padding:18px 20px;margin-top:18px">
        <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
          <span style="background:#e0992a;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                       text-transform:uppercase;padding:3px 8px;border-radius:3px">Google</span>
          <h3 style="margin:0;font-size:18px">Text ads</h3>
          <span class="muted" style="font-size:11px">Transparency Center · classified by title vs. body copy</span>
        </div>
        <div class="stats" style="margin-bottom:14px">
          <div class="stat"><div class="label">Google ads</div><div class="value">{brand.get('ads_google', 0)}</div></div>
          <div class="stat"><div class="label">Text ads shown</div><div class="value">{len(text_ads or [])}</div></div>
        </div>
        {text_ads_panel}
      </div>"""
    # iSpot TV-ads lane — only rendered when this brand has TV spots, so non-TV
    # reports show no TV lane at all.
    tv_spots_panel = _render_tv_spots_panel(tv_ads or [], dashboard_dir)
    tv_lane_html = ""
    if tv_spots_panel:
        m = tv_metrics or {}
        tv_lane_html = f"""
      <div class="lane lane-tv" style="background:#fdf2f8;border-left:4px solid #c0398a;
                                        border-radius:6px;padding:18px 20px;margin-top:18px">
        <div class="lane-label" style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
          <span style="background:#c0398a;color:white;font-size:10px;font-weight:600;letter-spacing:1px;
                       text-transform:uppercase;padding:3px 8px;border-radius:3px">TV</span>
          <h3 style="margin:0;font-size:18px">TV ads</h3>
          <span class="muted" style="font-size:11px">iSpot.tv · national linear + streaming spots</span>
        </div>
        <div class="stats" style="margin-bottom:14px">
          <div class="stat"><div class="label">TV spots shown</div><div class="value">{len(tv_ads or [])}</div></div>
          <div class="stat"><div class="label">National airings</div><div class="value">{_fmt_metric(m.get('national_airings'))}</div></div>
          <div class="stat"><div class="label">Total creatives</div><div class="value">{_fmt_metric(m.get('total_creatives'))}</div></div>
          <div class="stat"><div class="label">Spend rank</div><div class="value">{('#' + str(m['spend_rank'])) if m.get('spend_rank') is not None else _fmt_metric(None)}</div></div>
          <div class="stat"><div class="label">Est. spend</div><div class="value">{_fmt_metric(m.get('national_spend'))}</div></div>
          <div class="stat"><div class="label">Impressions</div><div class="value">{_fmt_metric(m.get('impressions'))}</div></div>
        </div>
        {tv_spots_panel}
      </div>"""
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
        <h4 style="margin:14px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#555">
          Top served ads
          <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400;font-size:11px">
            — ranked by Meta's "most served" sort × run duration (intra-brand only, proxy not raw impressions)
          </span>
        </h4>
        {top_served_html}
        <h4 style="margin:18px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#555">Creative gallery</h4>
        {gallery_html}
        <h4 style="margin:18px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:#555">Recent ads (last {{days}} days)</h4>
        {ads_html}
      </div>
      {google_lane_html}
      {tv_lane_html}
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


def _render_strategy_link(strategy_doc: str) -> str:
    """When a --strategy-doc URL is supplied, the 'Latest briefing' section is
    replaced by a link to that standalone strategy report (+ its sibling PDF)."""
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


DEFAULT_ORG = "Horizon Commerce"
DEFAULT_PRODUCT = "Creative & Competitive Intelligence"


def build_dashboard(
    out_dir: Path | str,
    *,
    days: int = 30,
    org_name: str = DEFAULT_ORG,
    product_name: str = DEFAULT_PRODUCT,
    brand_ids: set[str] | None = None,
    sources: set[str] | None = None,
    strategy_doc: str | None = None,
) -> dict[str, Any]:
    """Generate the dashboard at out_dir/index.html. Returns summary metadata.

    brand_ids restricts the dashboard to an allow-list of competitor ids (see
    _collect); None renders every competitor. sources scopes ad platforms
    (e.g. {"meta"} for the unchanged Meta report, {"meta","google"} for the
    with-Google report); None means all platforms. strategy_doc, when set to a
    (relative) URL, replaces the 'Latest briefing' section with a link to that
    standalone strategy report + its PDF."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        data = _collect(conn, days=days, brand_ids=brand_ids, sources=sources)

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
        <div class="bvb-mode" style="display:flex;flex-direction:column;gap:4px">
          <label style="font-size:11px;font-weight:600;letter-spacing:0.6px;text-transform:uppercase;color:#666">Weight</label>
          <div style="display:flex;gap:10px;font-size:13px">
            <label style="display:flex;gap:5px;align-items:center;cursor:pointer">
              <input type="radio" name="bvb-mode" value="count" checked> By count
            </label>
            <label style="display:flex;gap:5px;align-items:center;cursor:pointer">
              <input type="radio" name="bvb-mode" value="popularity"> By popularity <span class="muted" style="font-size:11px">(Meta ads only)</span>
            </label>
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

    # Landing pages — "where ads send traffic"
    sections_html.append(f"""
    <section id="landing-pages">
      <h2>Where ads send traffic</h2>
      <p class="muted">Per-brand breakdown of ad <code>link_url</code> destinations. Sections in <span style="color:#a93226">red</span> / <span style="color:#e08e00">orange</span> are flagged findings, not traffic worth applauding — measurement redirects (DoubleClick), Dynamic Creative templates that never resolved, or opaque short-links.</p>
      {_render_landing_pages_section(data, out_dir)}
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
        top = data["top_ads"].get(brand["id"], [])
        bs_data = data["brand_store_by_brand"].get(brand["id"])
        hp_data = data["homepage_by_brand"].get(brand["id"])
        sections_html.append(
            _render_brand_section(brand, recs, recent, out_dir,
                                   bs_data=bs_data, hp_data=hp_data, top_ads=top,
                                   text_ads=data["text_ads_by_brand"].get(brand["id"], []),
                                   tv_ads=data["tv_ads_by_brand"].get(brand["id"], []),
                                   tv_metrics=data["tv_metrics_by_brand"].get(brand["id"], {}))
            .replace("{days}", str(days))
        )

    # Reporting: a strategy-report link (when --strategy-doc is set) replaces the
    # latest-briefing body; otherwise the briefing renders as before.
    if strategy_doc:
        sections_html.append(f"""
    <section id="briefing">
      <h2>Strategy report</h2>
      {_render_strategy_link(strategy_doc)}
    </section>
    """)
    else:
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
            ('Where ads send traffic', '#landing-pages'),
            ('Browse all creatives', '#browse'),
        ]),
        ('Brands', [(brand["name"], f"#brand-{brand['id']}") for brand in data["brands"]]),
        ('Reporting', [('Strategy report' if strategy_doc else 'Latest briefing', '#briefing')]),
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
        "google_in_scope": data["google_in_scope"],
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
{_render_most_served_snapshot(data['brands'], data['top_ads'], out_dir)}
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
