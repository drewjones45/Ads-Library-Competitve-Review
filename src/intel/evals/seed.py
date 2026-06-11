"""Build the frozen seed.db fixture from the live data/intel.db.

The seed includes all competitors, observations, offers, and analyzed creatives,
plus the ads they belong to. Volatile tables (briefings, audit_log, llm_calls,
eval_runs, eval_task_results) are dropped so eval runs against the seed don't
inherit prior state.

Run via `intel evals build-seed` whenever the fixture should be refreshed.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from .runner import SEED_DB, _real_db_path


# Tables that must NOT carry state into the seed (per-run/per-call telemetry).
VOLATILE_TABLES = ("briefings", "audit_log", "llm_calls", "eval_runs", "eval_task_results")


def build_seed(*, source: Path | None = None, dest: Path | None = None) -> tuple[Path, dict]:
    """Snapshot the live db, clear volatile tables, return path + summary."""
    src = source or _real_db_path()
    dst = dest or SEED_DB
    if not src.exists():
        raise FileNotFoundError(f"source db missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

    conn = sqlite3.connect(dst)
    try:
        for t in VOLATILE_TABLES:
            try:
                conn.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError:
                # table may not yet exist on older snapshots — fine
                pass
        conn.commit()
        # VACUUM must run outside a transaction; isolation_level=None enables autocommit.
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = ""

        counts: dict[str, int] = {}
        for t in ("competitors", "sources", "observations", "ads", "creatives", "offers"):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            counts[t] = n
        counts["creatives_analyzed"] = conn.execute(
            "SELECT COUNT(*) FROM creatives WHERE analyzed_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    summary = {
        "dest": str(dst),
        "size_bytes": dst.stat().st_size,
        "counts": counts,
    }
    return dst, summary
