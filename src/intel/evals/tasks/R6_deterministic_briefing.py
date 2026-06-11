"""R6 — deterministic (no-LLM) briefing renders and covers active competitors.

This is a critical baseline: when the API key is missing we still produce a
factual briefing. Tests the deterministic template.
"""
from ..base import Task, TaskContext
from ..graders import (
    output_is_dict_no_error, regex_present, at_least, wall_budget_from_elapsed,
)


def _run(_ctx: TaskContext):
    from intel.synthesis.briefing import generate_briefing
    # Force the deterministic path even if API key happens to be set.
    return generate_briefing(days=30, use_llm=False, scope="eval_R6")


task = Task(
    id="R6",
    title="Deterministic briefing covers active competitors",
    category="regression",
    runner=_run,
    graders=[
        output_is_dict_no_error(),
        at_least("briefing_id", 1, name="briefing persisted"),
        regex_present("body_md", r"Competitive Briefing", name="has header"),
        regex_present("body_md", r"TL;DR", name="has TL;DR section"),
        regex_present("body_md", r"new ad", name="references new ads"),
        wall_budget_from_elapsed(3.0),
    ],
)
