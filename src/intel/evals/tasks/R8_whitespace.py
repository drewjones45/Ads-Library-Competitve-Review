"""R8 — whitespace detection returns ≥1 testable hypothesis."""
from ..base import Task, TaskContext
from ..graders import (
    list_non_empty, output_is_dict_no_error, regex_present, wall_budget_from_elapsed,
)


def _run(_ctx: TaskContext):
    from intel.synthesis.whitespace import detect_whitespace
    return detect_whitespace(vertical="furniture")


task = Task(
    id="R8",
    title="Whitespace detection returns testable hypotheses",
    category="regression",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        list_non_empty("whitespace"),
        regex_present("whitespace.0.testable_hypothesis", r".+",
                      name="first hypothesis is non-empty"),
        wall_budget_from_elapsed(45.0),
    ],
)
