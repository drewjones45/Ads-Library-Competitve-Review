"""Batch processor for vision creative analysis.

Iterates over creatives where `analyzed_at IS NULL`, runs the vision taxonomy,
persists the JSON result + timestamp. Idempotent — safe to re-run any time.

Cost-control levers (Section 24 of the checklist):
- `dedupe_by_phash`: if another creative with the same perceptual hash has already
  been analyzed, reuse its analysis without calling the model again.
- `limit`: cap the number of new model calls per run.
- `competitor_id`: scope to one brand.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .creative import analyze_creative_image
from .landing_analysis import analyze_landing_page
from .text_ad_analysis import analyze_text_ad
from .video import analyze_creative_video
from ..config import load_settings
from ..storage import audit, connect, utcnow


@dataclass
class BatchReport:
    examined: int = 0
    analyzed: int = 0
    reused_phash: int = 0
    failed: int = 0
    skipped_missing_file: int = 0
    errors: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"examined={self.examined}  analyzed={self.analyzed}  "
            f"reused_phash={self.reused_phash}  failed={self.failed}  "
            f"missing_file={self.skipped_missing_file}"
        )


_ASSET_TYPE_TO_CONTEXT = {
    "image": "meta_ad",
    "video": "meta_ad",
    "video_evicted": "meta_ad",
    "video_thumb": "meta_ad",
    "amazon_store_image": "brand_store",
    "amazon_store_hero": "brand_store_hero",
    "website_screenshot": "website",
    "homepage_image": "website",
    "landing_page_image": "landing_page",
    "text_ad": "google_text_ad",
    "tv_spot": "tv_ad",
}


def _context_for(asset_type: str | None) -> str:
    return _ASSET_TYPE_TO_CONTEXT.get(asset_type or "", "unknown")


def _pending(
    conn: sqlite3.Connection,
    competitor_id: str | None,
    limit: int | None,
    *,
    force_reanalyze: bool = False,
) -> list[sqlite3.Row]:
    """Load creatives needing analysis. LEFT JOIN ads so brand-store assets
    (no ad_id, competitor_id directly on creatives) are included.
    Effective competitor_id = COALESCE(ad.competitor_id, creative.competitor_id)."""
    q = (
        "SELECT cr.id, cr.ad_id, cr.asset_path, cr.phash, cr.asset_type, "
        "       COALESCE(a.competitor_id, cr.competitor_id) AS competitor_id "
        "FROM creatives cr LEFT JOIN ads a ON a.id = cr.ad_id "
    )
    where: list[str] = []
    params: list = []
    if not force_reanalyze:
        where.append("cr.analyzed_at IS NULL")
    if competitor_id:
        where.append("COALESCE(a.competitor_id, cr.competitor_id) = ?")
        params.append(competitor_id)
    if where:
        q += "WHERE " + " AND ".join(where) + " "
    q += "ORDER BY cr.id ASC"
    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(q, params).fetchall()


def _existing_phash_analysis(conn: sqlite3.Connection, phash: str, asset_type: str) -> dict | None:
    """If we've already analyzed a creative with this perceptual hash AND the same
    asset_type, return its JSON. The asset_type match is required because a
    video's first-frame phash can collide with an unrelated still image's phash,
    and the two analyses follow different schemas (video has a video_meta block,
    image does not)."""
    # 'video_evicted' shares schema with 'video' — both have video_meta — so we
    # allow cross-reuse between them.
    if asset_type in ("video", "video_evicted"):
        types = ("video", "video_evicted")
        row = conn.execute(
            "SELECT analysis_json FROM creatives "
            "WHERE phash = ? AND asset_type IN (?, ?) "
            "AND analyzed_at IS NOT NULL AND analysis_json IS NOT NULL LIMIT 1",
            (phash, types[0], types[1]),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT analysis_json FROM creatives "
            "WHERE phash = ? AND asset_type = ? "
            "AND analyzed_at IS NOT NULL AND analysis_json IS NOT NULL LIMIT 1",
            (phash, asset_type),
        ).fetchone()
    if row and row["analysis_json"]:
        try:
            return json.loads(row["analysis_json"])
        except json.JSONDecodeError:
            return None
    return None


def _store(
    conn: sqlite3.Connection,
    creative_id: int,
    analysis: dict,
    *,
    reused: bool,
) -> None:
    conn.execute(
        "UPDATE creatives SET analysis_json = ?, analyzed_at = ? WHERE id = ?",
        (json.dumps(analysis), utcnow(), creative_id),
    )


def analyze_pending(
    *,
    competitor_id: str | None = None,
    limit: int | None = 50,
    dedupe_by_phash: bool = True,
    model: str | None = None,
    force_reanalyze: bool = False,
) -> BatchReport:
    """Analyze creatives whose `analyzed_at IS NULL`. Returns BatchReport.

    `force_reanalyze` re-runs analysis on all matching creatives (use after a
    taxonomy expansion to backfill new fields on existing rows).
    """
    settings = load_settings()
    model = model or settings.vision_model
    report = BatchReport()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        report.errors.append({
            "where": "preflight",
            "message": "ANTHROPIC_API_KEY not set — add to .env to enable vision analysis.",
        })
        return report

    with connect() as conn:
        rows = _pending(conn, competitor_id, limit, force_reanalyze=force_reanalyze)
        report.examined = len(rows)

        for row in rows:
            path = Path(row["asset_path"])
            if not path.exists():
                report.skipped_missing_file += 1
                continue

            context = _context_for(row["asset_type"])

            # Dedup: if we've already classified an identical image (same phash)
            # AND that analysis was produced with the current taxonomy version,
            # reuse it. With force_reanalyze=True we skip dedup to ensure every
            # row gets the new schema applied.
            phash = row["phash"]
            if dedupe_by_phash and phash and not force_reanalyze:
                prior = _existing_phash_analysis(conn, phash, row["asset_type"] or "image")
                if prior is not None:
                    _store(conn, row["id"], prior, reused=True)
                    report.reused_phash += 1
                    continue

            try:
                asset_type = row["asset_type"]
                if asset_type in ("video", "video_evicted"):
                    # Video pipeline: read sidecar next to the mp4 (or first
                    # frame, for evicted ones) and analyze the frame sequence.
                    sidecar = path.parent / "video_meta.json"
                    if not sidecar.exists():
                        report.failed += 1
                        report.errors.append({
                            "creative_id": row["id"], "path": str(path),
                            "message": f"video sidecar not found at {sidecar}",
                        })
                        continue
                    result = analyze_creative_video(sidecar, model=model)
                elif asset_type == "landing_page_image":
                    # Landing pages get a dedicated page-level analysis (offer,
                    # CTA, page intent, trust signals, what works/misses) rather
                    # than the ad-creative taxonomy. The URL + classified section
                    # live in a sidecar written at capture time.
                    sidecar = path.parent / "landing_meta.json"
                    meta = {}
                    if sidecar.exists():
                        try:
                            meta = json.loads(sidecar.read_text())
                        except Exception:
                            meta = {}
                    result = analyze_landing_page(
                        path,
                        url=meta.get("clean_url") or meta.get("url") or "",
                        section=meta.get("section"),
                        model=model,
                    )
                elif asset_type == "text_ad":
                    # Google text ad: the asset_path IS the text_ad_meta.json
                    # sidecar (headlines/descriptions). Pass model=None so the
                    # analyzer picks the cheap text model — this batch's default
                    # `model` is the vision model, wrong for a text-only task.
                    meta = {}
                    try:
                        meta = json.loads(path.read_text())
                    except Exception:
                        meta = {}
                    result = analyze_text_ad(meta, model=None)
                else:
                    result = analyze_creative_image(path, model=model, context=context)
                if "error" in result:
                    report.failed += 1
                    report.errors.append({
                        "creative_id": row["id"], "path": str(path), "message": result["error"],
                    })
                    continue
                _store(conn, row["id"], result, reused=False)
                report.analyzed += 1
            except Exception as e:
                report.failed += 1
                report.errors.append({
                    "creative_id": row["id"], "path": str(path), "message": f"{type(e).__name__}: {e}",
                })

        audit(
            conn,
            actor="vision",
            action="creative_batch_analyze",
            details={
                "competitor_id": competitor_id, "limit": limit, "model": model,
                "force_reanalyze": force_reanalyze,
                "examined": report.examined, "analyzed": report.analyzed,
                "reused_phash": report.reused_phash, "failed": report.failed,
            },
        )
    return report
