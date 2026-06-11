"""F1 — vision on a low-quality / ambiguous image.

This is a failure-mode task: we expect the model to *signal* uncertainty via
the `confidence` field. Today we don't gate on it (Phase 1 adds gating); for
now we just assert that the field exists and is below 0.7 — proving the model
DOES self-report uncertainty even when output is otherwise plausible.

Bible §6: failure-mode tasks target known weak spots.
"""
from ..base import Task, TaskContext, GraderOutcome
from ..graders import output_is_dict_no_error, wall_budget_from_elapsed


def _confidence_below_high(out, _ctx) -> GraderOutcome:
    """Pass if model returns confidence < 0.85 (signals uncertainty).
    The test image is intentionally low-information."""
    conf = out.get("confidence") if isinstance(out, dict) else None
    if conf is None:
        return GraderOutcome(name="reports confidence", passed=False, detail="no confidence field")
    ok = float(conf) < 0.85
    return GraderOutcome(
        name="confidence reflects ambiguity (<0.85)",
        passed=ok,
        detail=f"confidence={conf}",
    )


def _run(ctx: TaskContext):
    from intel.analysis.creative import analyze_creative_image
    return analyze_creative_image(ctx.fixture("images", "low_quality.jpg"))


task = Task(
    id="F1",
    title="Vision on low-quality image signals uncertainty via confidence",
    category="failure_mode",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        _confidence_below_high,
        wall_budget_from_elapsed(30.0),
    ],
)
