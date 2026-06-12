"""Orchestrates a single ingest+analyze pass.

Flow per source:
  1. adapter.fetch() — pull raw, persist payload
  2. compare content_hash to last observation → changed?
  3. structured diff (parsed fields) for change_summary + severity
  4. record observation row
  5. type-specific enrichment:
       - website + changed → run offer extractor on parsed page
       - meta_ads → upsert ads, flag new ones
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .adapters import get_adapter
from .adapters.base import IngestResult
from .analysis.diff import (
    classify_change_severity,
    diff_parsed_observations,
    summarize_diff_for_log,
)
from .analysis.homepage_hero import extract_hero_promo
from .analysis.offers import extract_offers_from_observation
from .config import Competitor, Settings, load_competitors, load_settings
from .storage import (
    audit,
    connect,
    fetch_prior_homepage_promo_by_hero_hash,
    get_source_id,
    latest_observation,
    record_homepage_promo,
    record_observation,
    record_offer,
    upsert_ad,
    upsert_competitor,
    upsert_creative,
)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_phash(path: str) -> str | None:
    """Compute a perceptual hash of an image file; None if the file can't be read."""
    try:
        from .analysis.creative import perceptual_hash
        from pathlib import Path
        return perceptual_hash(Path(path))
    except Exception:
        return None


def _sha256_file(path: str) -> str | None:
    """SHA-256 of a file's bytes. Used as the hero-crop cache key."""
    try:
        import hashlib
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


@dataclass
class RunReport:
    competitor_id: str
    source_key: str
    ok: bool
    changed: bool
    severity: float = 0.0
    change_summary: str = ""
    new_ads: list[str] = field(default_factory=list)
    offers_extracted: int = 0
    homepage_promos_extracted: int = 0
    homepage_promos_cached: int = 0
    error: str | None = None


def ingest_one(competitor: Competitor, source_idx: int, settings: Settings | None = None) -> RunReport:
    settings = settings or load_settings()
    source = competitor.sources[source_idx]
    AdapterCls = get_adapter(source.type)
    result: IngestResult = AdapterCls(competitor, source).fetch()

    if not result.ok:
        return RunReport(
            competitor_id=competitor.id,
            source_key=source.stable_key(),
            ok=False,
            changed=False,
            error=result.error,
        )

    with connect() as conn:
        upsert_competitor(conn, competitor)
        source_id = get_source_id(conn, competitor.id, source.stable_key())

        prior = latest_observation(conn, source_id)
        prior_parsed = json.loads(prior["parsed_json"]) if prior and prior["parsed_json"] else None
        changed = (prior is None) or (prior["content_hash"] != result.content_hash)

        diff = diff_parsed_observations(prior_parsed, result.parsed)
        change_summary = summarize_diff_for_log(diff) if changed else ""
        severity, reason = (
            classify_change_severity(diff, settings.weights) if changed else (0.0, "")
        )

        obs_id = record_observation(
            conn,
            source_id=source_id,
            content_hash_val=result.content_hash,
            raw_path=result.raw_path,
            parsed=result.parsed,
            changed=changed,
            change_summary=change_summary,
            severity=severity,
        )
        audit(
            conn,
            actor=f"adapter:{source.type}",
            action="ingest",
            details={
                "competitor_id": competitor.id,
                "source_key": source.stable_key(),
                "observation_id": obs_id,
                "changed": changed,
                "severity": severity,
                "severity_reason": reason,
            },
        )

        report = RunReport(
            competitor_id=competitor.id,
            source_key=source.stable_key(),
            ok=True,
            changed=changed,
            severity=severity,
            change_summary=change_summary,
        )

        # ---- type-specific enrichment ----
        if source.type == "website":
            if changed and settings.llm.get("enrich_only_on_change", True):
                try:
                    offers = extract_offers_from_observation(result.parsed)
                    for o in offers:
                        o.setdefault("source", "website")
                        o.setdefault("source_ref", str(obs_id))
                        record_offer(conn, competitor.id, o)
                    report.offers_extracted = len(offers)
                except Exception as e:
                    audit(conn, actor="enrichment", action="offer_extract_failed", details={"err": str(e)})

            # Note: individual homepage page-images are no longer persisted as
            # creatives. The whole-page screenshot (in the observation's parsed
            # JSON) and the hero crop below are the kept website artifacts; the
            # noisy per-<img> grid was removed from both capture and dashboard.

            # Structured hero/banner promo extraction (Phase H2). Runs every
            # observation; the hero-hash cache skips the LLM call when the
            # cropped hero is byte-identical to the prior captured one.
            hero_path = (result.parsed or {}).get("hero_image_path")
            if hero_path:
                hero_hash = _sha256_file(hero_path)
                prior = (
                    fetch_prior_homepage_promo_by_hero_hash(conn, competitor.id, hero_hash)
                    if hero_hash else None
                )
                try:
                    if prior:
                        record_homepage_promo(
                            conn, competitor.id, source_id, obs_id,
                            result.parsed.get("observed_at") or _utcnow_iso(),
                            prior, hero_hash, cache_hit=True,
                        )
                        report.homepage_promos_cached += 1
                    else:
                        promo = extract_hero_promo(result.parsed, hero_path)
                        if promo:
                            record_homepage_promo(
                                conn, competitor.id, source_id, obs_id,
                                _utcnow_iso(), promo, hero_hash, cache_hit=False,
                            )
                            report.homepage_promos_extracted += 1
                except Exception as e:
                    audit(conn, actor="enrichment",
                          action="hero_extract_failed",
                          details={"err": str(e)})

        if source.type == "meta_ads":
            ads = result.extras.get("ads", [])
            creative_inserts = 0
            video_inserts = 0
            for ad in ads:
                ad_row_id, is_new = upsert_ad(conn, competitor.id, ad)
                if is_new and ad.get("ad_archive_id"):
                    report.new_ads.append(ad["ad_archive_id"])
                # Persist any locally-downloaded creative assets (browser-scrape path).
                for asset_path in ad.get("local_creative_paths") or []:
                    phash = _safe_phash(asset_path)
                    _, inserted = upsert_creative(
                        conn,
                        ad_id=ad_row_id,
                        asset_type="image",
                        asset_path=asset_path,
                        phash=phash,
                    )
                    if inserted:
                        creative_inserts += 1
                # Persist the video creative row (mp4) — frames + transcript live
                # alongside in `data/creative/{comp}/{archive_id}/`. The analyzer
                # picks them up via the sidecar. phash is the first frame's
                # (better dedup signal than mp4 bytes).
                if ad.get("local_video_path"):
                    vmeta = ad.get("video_meta") or {}
                    first_frame = (vmeta.get("frames") or [{}])[0].get("path")
                    phash = _safe_phash(first_frame) if first_frame else None
                    _, inserted = upsert_creative(
                        conn,
                        ad_id=ad_row_id,
                        asset_type="video",
                        asset_path=ad["local_video_path"],
                        phash=phash,
                    )
                    if inserted:
                        video_inserts += 1
            # mp4 retention sweep — keep mp4 only for top-N by popularity. Frames
            # + transcripts are KEPT for all videos (analyzer inputs).
            try:
                from .storage import prune_videos
                evicted = prune_videos(conn, competitor.id, keep_n=12)
            except Exception as e:
                evicted = 0
                audit(conn, actor="enrichment", action="video_prune_failed",
                      details={"err": str(e), "competitor": competitor.id})
            audit(
                conn,
                actor="enrichment",
                action="ads_upserted",
                details={
                    "total": len(ads),
                    "new": len(report.new_ads),
                    "creatives_inserted": creative_inserts,
                    "video_creatives_inserted": video_inserts,
                    "videos_evicted_from_top12": evicted,
                },
            )

        if source.type == "amazon_brand_store":
            pages = result.extras.get("pages", [])
            creative_inserts = 0
            for sp in pages:
                # sp is a ScrapedPage dataclass. Persist each downloaded image
                # as a standalone creative (ad_id=None, competitor_id set).
                for asset_path_str in sp.image_local_paths:
                    phash = _safe_phash(asset_path_str)
                    _, inserted = upsert_creative(
                        conn,
                        ad_id=None,
                        asset_type="amazon_store_image",
                        asset_path=asset_path_str,
                        phash=phash,
                        competitor_id=competitor.id,
                    )
                    if inserted:
                        creative_inserts += 1
            audit(
                conn,
                actor="enrichment",
                action="amazon_brand_store_upserted",
                details={
                    "pages_captured": len(pages),
                    "creatives_inserted": creative_inserts,
                    "total_image_paths": sum(len(p.image_local_paths) for p in pages),
                },
            )

    return report


def ingest_competitor(competitor_id: str) -> list[RunReport]:
    settings = load_settings()
    reports: list[RunReport] = []
    for comp in load_competitors():
        if comp.id != competitor_id:
            continue
        for idx in range(len(comp.sources)):
            reports.append(ingest_one(comp, idx, settings))
    return reports


def ingest_all() -> list[RunReport]:
    settings = load_settings()
    reports: list[RunReport] = []
    for comp in load_competitors():
        for idx in range(len(comp.sources)):
            reports.append(ingest_one(comp, idx, settings))
    return reports
