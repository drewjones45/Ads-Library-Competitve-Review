#!/usr/bin/env python3
"""cc_vision_write.py — write subagent vision results into creatives.analysis_json.

Reads a results JSON produced from the Claude Code subagent vision pass — either
a list of {"creative_id": int, "analysis": {...}} objects, or a flat mapping
{"<creative_id>": {...analysis...}}. For each, stores the analysis dict on the
matching creative and stamps analyzed_at (UTC, matching storage.utcnow()'s
format), exactly as creative_batch._store does — so the rows flow through the
dashboards/readouts identically to a keyed run.

Honors INTEL_DB_PATH. Usage:
    INTEL_DB_PATH=data/philo.db python scripts/cc_vision_write.py results.json
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    # Matches src/intel/storage.utcnow(): ISO8601, seconds, trailing Z.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_pairs(blob) -> list[tuple[int, dict]]:
    pairs: list[tuple[int, dict]] = []
    if isinstance(blob, dict):
        for k, v in blob.items():
            pairs.append((int(k), v))
    elif isinstance(blob, list):
        for item in blob:
            cid = item.get("creative_id")
            analysis = item.get("analysis", item)
            if cid is not None:
                pairs.append((int(cid), analysis))
    return pairs


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: cc_vision_write.py results.json [results2.json ...]", file=sys.stderr)
        sys.exit(2)

    db = os.environ.get("INTEL_DB_PATH", "data/intel.db")
    conn = sqlite3.connect(db)

    pairs: list[tuple[int, dict]] = []
    for fp in sys.argv[1:]:
        blob = json.loads(Path(fp).read_text())
        pairs.extend(load_pairs(blob))

    # Asset-type families that may share a phash-derived analysis. A video's
    # first-frame phash can collide with a still image, and their schemas differ
    # (video has a video_meta block) — so propagation is gated within-family.
    VIDEO_TYPES = ("video", "video_evicted")

    written = 0
    errors = 0
    propagated = 0
    ts = utcnow()
    for cid, analysis in pairs:
        if not isinstance(analysis, dict) or "error" in analysis:
            errors += 1
            continue
        blob = json.dumps(analysis)
        conn.execute(
            "UPDATE creatives SET analysis_json = ?, analyzed_at = ? WHERE id = ?",
            (blob, ts, cid),
        )
        written += 1

        # Propagate to visually-identical, still-unanalyzed siblings (same phash,
        # same schema family) — the phash-dedup reuse a keyed analyze_pending
        # would have done for free.
        row = conn.execute(
            "SELECT phash, asset_type FROM creatives WHERE id = ?", (cid,)
        ).fetchone()
        if not row or not row[0]:
            continue
        phash, atype = row[0], row[1]
        if atype in VIDEO_TYPES:
            cur = conn.execute(
                "UPDATE creatives SET analysis_json = ?, analyzed_at = ? "
                "WHERE phash = ? AND asset_type IN (?, ?) AND analyzed_at IS NULL",
                (blob, ts, phash, VIDEO_TYPES[0], VIDEO_TYPES[1]),
            )
        else:
            cur = conn.execute(
                "UPDATE creatives SET analysis_json = ?, analyzed_at = ? "
                "WHERE phash = ? AND asset_type = ? AND analyzed_at IS NULL",
                (blob, ts, phash, atype),
            )
        propagated += cur.rowcount
    conn.commit()
    print(f"wrote analysis for {written} creatives (db={db}); "
          f"propagated to {propagated} phash-identical siblings; "
          f"skipped {errors} error/empty")


if __name__ == "__main__":
    main()
