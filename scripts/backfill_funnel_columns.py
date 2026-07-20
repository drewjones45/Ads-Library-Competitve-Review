#!/usr/bin/env python3
"""backfill_funnel_columns.py — populate the funnel columns from stored actions.

`ad_performance.extra_json` has always held Meta's raw `actions` /
`action_values` blobs verbatim, which means every funnel step the dashboard
needs (landing page view, view content, add to cart, initiate checkout) was
already on disk before those columns existed. This promotes them into real
columns so the dashboard can aggregate in SQL — no API calls, no re-ingest, and
it works on windows that are now outside Meta's retention.

Ranking columns are NOT backfillable this way: they were never requested from
the API, so they aren't in extra_json. Those need a re-ingest.

Usage:
    INTEL_DB_PATH=data/jdsports.db python scripts/backfill_funnel_columns.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from intel.adapters.meta_account import _action_value  # noqa: E402

# Mirrors normalize_insight()'s funnel block — same alias order, so a backfilled
# row and a freshly-ingested one resolve to the same number. See the comment
# there for why the omni_* surface leads.
STEPS = {
    "landing_page_views": ("omni_landing_page_view", "landing_page_view"),
    "view_content": ("omni_view_content", "view_content"),
    "add_to_cart": ("omni_add_to_cart", "add_to_cart"),
    "initiate_checkout": ("omni_initiated_checkout", "initiate_checkout"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = os.environ.get("INTEL_DB_PATH", "data/intel.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    have = {r[1] for r in conn.execute("PRAGMA table_info(ad_performance)")}
    missing = [c for c in STEPS if c not in have]
    if missing:
        sys.exit(
            f"columns {missing} do not exist in {db}. Open the db through "
            "intel.storage.connect() once so the forward-migration runs, then retry."
        )

    rows = conn.execute(
        "SELECT id, extra_json FROM ad_performance "
        "WHERE extra_json IS NOT NULL AND extra_json != ''"
    ).fetchall()

    updates, totals, skipped = [], {k: 0.0 for k in STEPS}, 0
    for r in rows:
        try:
            actions = (json.loads(r["extra_json"]) or {}).get("actions")
        except (ValueError, TypeError):
            skipped += 1
            continue
        vals = {col: _action_value(actions, names) for col, names in STEPS.items()}
        for k, v in vals.items():
            totals[k] += v
        updates.append((*[vals[c] for c in STEPS], r["id"]))

    print(f"db={db}  rows with actions: {len(rows)}  unparseable: {skipped}")
    for k, v in totals.items():
        print(f"  {k:20} {v:>14,.0f}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    sets = ", ".join(f"{c}=?" for c in STEPS)
    conn.executemany(f"UPDATE ad_performance SET {sets} WHERE id=?", updates)
    conn.commit()
    print(f"\nbackfilled {len(updates)} rows")


if __name__ == "__main__":
    main()
