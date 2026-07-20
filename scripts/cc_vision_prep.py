#!/usr/bin/env python3
"""cc_vision_prep.py — select creatives for the Claude Code subagent vision pass.

The no-ANTHROPIC-key analysis path: `intel analyze-creatives` hard-returns
without a key (creative_batch.py), so vision analysis is produced instead by
Claude Code subagents. This script enumerates the same `analyzed_at IS NULL`
creatives, but adds the cost controls a keyed run gets for free:

  * phash dedup — one representative per distinct image; its analysis is later
    propagated to all visually-identical siblings (see cc_vision_write.py).
  * popularity ranking — representatives are ranked by the project's own
    storage.popularity_score (rank + longevity + active bonus).
  * per-brand caps — --max-image / --max-video bound the fan-out so a 200-ad
    brand doesn't dominate the run. 0 = unlimited.

Each representative carries its analysis *context* + schema family (image /
video / landing) so subagent output matches what the dashboards expect in
`creative_context`. Honors INTEL_DB_PATH (works for any deployment).

Usage:
    INTEL_DB_PATH=data/philo.db python scripts/cc_vision_prep.py /tmp/tasks.json \
        --max-image 25 --max-video 10
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from intel.storage import popularity_score  # noqa: E402

ASSET_TYPE_TO_CONTEXT = {
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
    # Owned-account lane: a rendered screenshot of the ad exactly as served
    # (creative + copy + social proof composited). Same analysis schema as a
    # scraped meta ad — the surrounding Facebook chrome is the only difference.
    "ad_preview": "meta_ad",
}


def schema_family(asset_type: str) -> str:
    if asset_type in ("video", "video_evicted"):
        return "video"
    if asset_type == "landing_page_image":
        return "landing"
    if asset_type == "text_ad":
        return "text"
    return "image"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="/tmp/cc_vision_tasks.json")
    ap.add_argument("--competitor", default=None)
    ap.add_argument("--max-image", type=int, default=0, help="cap image reps per brand (0=all)")
    ap.add_argument("--max-video", type=int, default=0, help="cap video reps per brand (0=all)")
    args = ap.parse_args()

    db = os.environ.get("INTEL_DB_PATH", "data/intel.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Per-brand max serp rank, to feed popularity_score.
    brand_max_rank = {
        r["cid"]: (r["mx"] or 0)
        for r in conn.execute(
            "SELECT competitor_id cid, MAX(serp_position_rank) mx FROM ads GROUP BY competitor_id"
        )
    }

    q = (
        "SELECT cr.id, cr.ad_id, cr.asset_path, cr.phash, cr.asset_type, "
        "       COALESCE(a.competitor_id, cr.competitor_id) AS competitor_id, "
        "       a.serp_position_rank, a.start_date, a.last_seen, a.active "
        "FROM creatives cr LEFT JOIN ads a ON a.id = cr.ad_id "
        "WHERE cr.analyzed_at IS NULL "
    )
    params: list = []
    if args.competitor:
        q += "AND COALESCE(a.competitor_id, cr.competitor_id) = ? "
        params.append(args.competitor)
    rows = conn.execute(q, params).fetchall()

    # Group by (competitor, phash); pick the highest-popularity representative.
    groups: dict[tuple, dict] = {}
    skipped_missing = 0
    for r in rows:
        path = Path(r["asset_path"])
        if not path.exists():
            skipped_missing += 1
            continue
        cid = r["competitor_id"]
        phash = r["phash"] or f"_nohash_{r['id']}"
        score = popularity_score(
            r["serp_position_rank"], r["start_date"], r["last_seen"],
            r["active"] or 0, brand_max_rank.get(cid, 0) or 0,
        )
        key = (cid, schema_family(r["asset_type"] or "image"), phash)
        g = groups.get(key)
        if g is None or score > g["score"]:
            groups[key] = {"row": r, "score": score, "path": str(path)}

    # Rank within brand+family and apply caps.
    by_bucket: dict[tuple, list] = {}
    for (cid, fam, _phash), g in groups.items():
        by_bucket.setdefault((cid, fam), []).append(g)

    # Caps apply ONLY to Meta ad creatives. Website screenshots, landing pages,
    # brand-store assets, etc. are few and always kept.
    caps = {"image": args.max_image, "video": args.max_video}
    tasks = []
    for (cid, fam), reps in by_bucket.items():
        reps.sort(key=lambda g: g["score"], reverse=True)
        is_meta_ad = all(g["row"]["context"] == "meta_ad" if "context" in g["row"].keys() else True for g in reps)
        cap = caps.get(fam, 0)
        # Only cap when these are meta_ad creatives (have an ad_id / popularity).
        meta_reps = [g for g in reps if g["row"]["ad_id"] is not None]
        if cap and fam in caps and len(meta_reps) > 0 and len(reps) > cap:
            # keep the top `cap` ad creatives; always keep any non-ad assets.
            non_ad = [g for g in reps if g["row"]["ad_id"] is None]
            reps = meta_reps[:cap] + non_ad
        for g in reps:
            r = g["row"]
            atype = r["asset_type"] or "image"
            task = {
                "creative_id": r["id"],
                "competitor_id": cid,
                "asset_type": atype,
                "context": ASSET_TYPE_TO_CONTEXT.get(atype, "unknown"),
                "schema": fam,
                "asset_path": g["path"],
                "phash": r["phash"],
                "popularity": round(g["score"], 3),
            }
            if fam == "video":
                sidecar = Path(g["path"]).parent / "video_meta.json"
                task["sidecar_path"] = str(sidecar)
                if sidecar.exists():
                    try:
                        meta = json.loads(sidecar.read_text())
                        task["frames"] = [
                            {"path": f["path"], "t_sec": f.get("t_sec")}
                            for f in (meta.get("frames") or [])
                            if Path(f["path"]).exists()
                        ]
                        task["duration_sec"] = meta.get("duration_sec")
                        task["transcript_full"] = meta.get("transcript_full")
                        task["transcript_quality"] = meta.get("transcript_quality")
                    except Exception as e:
                        task["sidecar_error"] = str(e)
            elif fam == "landing":
                sidecar = Path(g["path"]).parent / "landing_meta.json"
                if sidecar.exists():
                    try:
                        meta = json.loads(sidecar.read_text())
                        task["landing_url"] = meta.get("clean_url") or meta.get("url") or ""
                        task["landing_section"] = meta.get("section")
                    except Exception as e:
                        task["sidecar_error"] = str(e)
            tasks.append(task)

    Path(args.out).write_text(json.dumps(tasks, indent=2))
    by_fam: dict[str, int] = {}
    per_brand: dict[str, int] = {}
    for t in tasks:
        by_fam[t["schema"]] = by_fam.get(t["schema"], 0) + 1
        per_brand[t["competitor_id"]] = per_brand.get(t["competitor_id"], 0) + 1
    print(f"wrote {len(tasks)} representative tasks → {args.out}  (db={db})")
    print(f"  by schema: {by_fam}")
    print(f"  by brand:  {per_brand}")
    print(f"  skipped (missing file): {skipped_missing}")


if __name__ == "__main__":
    main()
