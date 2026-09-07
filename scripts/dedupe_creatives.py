#!/usr/bin/env python3
"""dedupe_creatives.py — merge `creatives` rows that describe the same asset file.

`creatives.asset_path` is stored absolute, and until `storage.upsert_creative`
learned to key on `storage.asset_key`, re-ingesting a db first built on another
machine inserted a SECOND row for every asset rather than adopting the first.
The damage is quiet but real: the vision analysis stays on the original row, so
the dashboard's creative gallery shows each asset twice while only one copy
carries its attributes.

This merges each duplicate group down to one row, preferring the row that
actually holds an analysis, and repoints it at whichever path exists on this
machine. It then repoints any remaining row whose file is missing here but does
exist under --data-dir — rows written on another machine that were never
duplicated, which is the state that made every asset ref in the Spectrum
dashboard an unreachable Windows `C:\\Users\\...` path. Dry-run by default.

Usage:
  python3 scripts/dedupe_creatives.py --db data/spectrum.db --data-dir data/spectrum_assets
  python3 scripts/dedupe_creatives.py --db data/spectrum.db --data-dir data/spectrum_assets --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from intel.storage import asset_key  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--data-dir", default="",
                    help="asset tree to re-resolve foreign paths against, e.g. data/spectrum_assets")
    ap.add_argument("--apply", action="store_true", help="write the merge (default: report only)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ad_id, competitor_id, asset_type, asset_path, phash, "
        "       analysis_json, analyzed_at FROM creatives ORDER BY id"
    ).fetchall()

    groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[(r["ad_id"], r["competitor_id"], r["asset_type"], asset_key(r["asset_path"]))].append(r)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    print(f"{len(rows)} creative row(s), {len(groups)} distinct asset(s), "
          f"{len(dupes)} duplicated")

    # Second pass: rows whose file is not here, but whose stable key does resolve
    # under the local tree. Indexing the tree by key beats guessing at path
    # surgery — the same lookup works whatever machine wrote the row.
    local_by_key: dict[str, str] = {}
    if args.data_dir:
        root = Path(args.data_dir)
        for f in root.rglob("*"):
            if f.is_file():
                local_by_key[asset_key(str(f.resolve()))] = str(f.resolve())
    strays = []
    dup_ids = {m["id"] for v in dupes.values() for m in v}
    for r in rows:
        if r["id"] in dup_ids or Path(r["asset_path"]).is_file():
            continue
        hit = local_by_key.get(asset_key(r["asset_path"]))
        if hit:
            strays.append((r["id"], hit))
    if strays:
        print(f"  {len(strays)} row(s) point at a file that is not on this machine "
              f"but resolve under {args.data_dir}")
    if not dupes and not strays:
        return 0

    plan: list[tuple] = []
    for key, members in dupes.items():
        # Keep whichever row carries the analysis — that is the irreplaceable part;
        # the path is trivially recomputable, an analysis is not. Ties go to the
        # oldest row so referencing ids stay stable.
        keep = next((m for m in members if m["analysis_json"]), members[0])
        # Point it at a path that exists here, if any member has one.
        best = next((m["asset_path"] for m in members if Path(m["asset_path"]).is_file()),
                    keep["asset_path"])
        drop = [m["id"] for m in members if m["id"] != keep["id"]]
        plan.append((keep["id"], best, drop, key[3]))

    n_drop = sum(len(p[2]) for p in plan)
    by_id = {r["id"]: r["asset_path"] for r in rows}
    n_repoint = sum(1 for p in plan if p[1] != by_id[p[0]])
    if plan:
        print(f"  keep {len(plan)}, delete {n_drop}, repoint {n_repoint} path(s)")
    for keep_id, path, drop, key in plan[:5]:
        print(f"    keep #{keep_id} -> {path[-64:]}")
        print(f"      drop {drop}   [{key}]")
    if len(plan) > 5:
        print(f"    … and {len(plan) - 5} more group(s)")

    if not args.apply:
        print("\nnothing written — pass --apply to merge")
        return 0

    with conn:
        for keep_id, path, drop, _ in plan:
            conn.execute("UPDATE creatives SET asset_path=? WHERE id=?", (path, keep_id))
            conn.executemany("DELETE FROM creatives WHERE id=?", [(d,) for d in drop])
        for rid, path in strays:
            conn.execute("UPDATE creatives SET asset_path=? WHERE id=?", (path, rid))
    left = conn.execute("SELECT COUNT(*) FROM creatives").fetchone()[0]
    gone = sum(1 for r in conn.execute("SELECT asset_path FROM creatives")
               if not Path(r[0]).is_file())
    print(f"\nmerged — {left} creative row(s) remain, {len(strays)} repointed locally, "
          f"{gone} still pointing at a file that is not here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
