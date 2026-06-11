"""R5 — LLM briefing should cite real ad_archive_ids.

Phase 0 baseline expectation: this likely FAILS — current BRIEFING_SYSTEM
doesn't require [#ad:<id>] citations. Phase 1 adds citation enforcement.
The eval surfaces the gap.
"""
import re
import sqlite3

from ..base import GraderOutcome, Task, TaskContext
from ..graders import output_is_dict_no_error, wall_budget_from_elapsed


def _has_citations(out, _ctx) -> GraderOutcome:
    """Pass if briefing body contains at least 2 [#ad:N] citations OR
    bare ad_archive_id strings matching known seeded ads."""
    if not isinstance(out, dict) or not out.get("body_md"):
        return GraderOutcome(name="briefing has citations", passed=False, detail="no body_md")
    body = out["body_md"]
    explicit = re.findall(r"\[#ad:[\w-]+\]", body)
    if len(explicit) >= 2:
        return GraderOutcome(name="briefing has [#ad:N] citations (>=2)", passed=True,
                             detail=f"found {len(explicit)} explicit citations")
    return GraderOutcome(
        name="briefing has [#ad:N] citations (>=2)",
        passed=False,
        detail=f"found {len(explicit)} (need >=2; Phase 1 adds enforcement)",
    )


def _citations_resolve(out, ctx) -> GraderOutcome:
    """If there are citations, each one should resolve to a real ad in the seed db."""
    if not isinstance(out, dict) or not out.get("body_md"):
        return GraderOutcome(name="all citations resolve", passed=False, detail="no body_md")
    body = out["body_md"]
    cites = re.findall(r"\[#ad:([\w-]+)\]", body)
    if not cites:
        return GraderOutcome(name="all citations resolve", passed=True,
                             detail="no citations to verify (vacuously true)")
    with sqlite3.connect(ctx.seed_db_path) as c:
        c.row_factory = sqlite3.Row
        missing = []
        for cid in cites:
            row = c.execute("SELECT 1 FROM ads WHERE ad_archive_id=?", (cid,)).fetchone()
            if row is None:
                missing.append(cid)
    ok = not missing
    return GraderOutcome(
        name="all citations resolve to real ads",
        passed=ok,
        detail=f"missing={missing[:3]}" if missing else f"all {len(cites)} resolve",
    )


def _run(_ctx: TaskContext):
    from intel.synthesis.briefing import generate_briefing
    return generate_briefing(days=30, scope="eval_R5")


task = Task(
    id="R5",
    title="LLM briefing cites real ad_archive_ids",
    category="regression",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        _has_citations,
        _citations_resolve,
        wall_budget_from_elapsed(60.0),
    ],
)
