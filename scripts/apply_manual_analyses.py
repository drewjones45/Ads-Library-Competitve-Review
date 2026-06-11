#!/usr/bin/env python
"""Apply hand-produced creative analyses to the DB.

When ANTHROPIC_API_KEY isn't available, we substitute the vision model with a
human (or Claude Code) reading each image and producing the taxonomy JSON.

Usage:
    python scripts/apply_manual_analyses.py path/to/analyses.json

Input JSON shape — a list of entries keyed by phash. Each entry's `analysis`
gets written to every creative sharing that phash:

    [
      {"phash": "e4f94b25671e03ca", "analysis": { ...full schema... }},
      {"phash": "aaddd5230bfc24d0", "analysis": { ... }}
    ]

The script:
  - Validates that each entry has a phash + analysis dict
  - Stamps `analyzed_at` so analyze_pending's idempotency holds
  - Reports how many creatives were updated per phash (phash dedup means
    a single analysis can update multiple rows)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(in_path: str) -> int:
    payload = json.loads(Path(in_path).read_text())
    if not isinstance(payload, list):
        print(f"error: expected a list, got {type(payload).__name__}", file=sys.stderr)
        return 1

    default_db = Path(__file__).resolve().parents[1] / "data" / "intel.db"
    db = Path(os.environ.get("INTEL_DB_PATH", default_db))
    print(f"applying to db: {db}")
    conn = sqlite3.connect(db)
    try:
        ts = utcnow()
        total_phashes = 0
        total_rows = 0
        for entry in payload:
            phash = entry.get("phash")
            analysis = entry.get("analysis")
            if not phash or not isinstance(analysis, dict):
                print(f"  skip: malformed entry {entry!r}", file=sys.stderr)
                continue
            analysis_json = json.dumps(analysis)
            cur = conn.execute(
                "UPDATE creatives SET analysis_json=?, analyzed_at=? WHERE phash=?",
                (analysis_json, ts, phash),
            )
            n = cur.rowcount
            total_phashes += 1
            total_rows += n
            print(f"  phash {phash[:12]}…  →  {n} row(s) updated")
        conn.commit()
        print(f"\nupdated {total_rows} creative row(s) across {total_phashes} unique phash(es)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: apply_manual_analyses.py <analyses.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
