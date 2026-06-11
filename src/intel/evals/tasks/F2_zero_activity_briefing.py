"""F2 — briefing with zero activity should produce graceful 'no activity' text,
not hallucinated ads.

We accomplish 'zero activity' by passing days=0 so the corpus window covers
no time. Tests the LLM doesn't invent things to fill the void.
"""
import re

from ..base import GraderOutcome, Task, TaskContext
from ..graders import output_is_dict_no_error, wall_budget_from_elapsed


def _no_hallucinated_ad_ids(out, _ctx) -> GraderOutcome:
    """Briefing should not contain ad_archive_id-like numbers when corpus is empty."""
    if not isinstance(out, dict):
        return GraderOutcome(name="no hallucinated ad ids", passed=False, detail="not a dict")
    body = out.get("body_md", "") or ""
    # ad_archive_ids are typically 14+ digit strings.
    suspicious = re.findall(r"\b\d{14,}\b", body)
    return GraderOutcome(
        name="no hallucinated ad ids in zero-activity briefing",
        passed=not suspicious,
        detail=f"found {len(suspicious)}: {suspicious[:2]}" if suspicious else "ok",
    )


def _states_no_activity(out, _ctx) -> GraderOutcome:
    body = (out.get("body_md") if isinstance(out, dict) else "") or ""
    triggers = ("no material", "quiet", "no activity", "no new ads",
                "no offers", "no significant", "no changes")
    ok = any(t.lower() in body.lower() for t in triggers)
    return GraderOutcome(
        name="explicitly states quiet window",
        passed=ok,
        detail="ok" if ok else f"body[:100]={body[:100]!r}",
    )


def _run(_ctx: TaskContext):
    from intel.synthesis.briefing import generate_briefing
    return generate_briefing(days=0, scope="eval_F2")


task = Task(
    id="F2",
    title="Zero-activity briefing degrades gracefully (no hallucinated ads)",
    category="failure_mode",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        _no_hallucinated_ad_ids,
        _states_no_activity,
        wall_budget_from_elapsed(20.0),
    ],
)
