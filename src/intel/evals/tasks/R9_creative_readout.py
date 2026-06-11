"""R9 — per-brand creative readout renders with expected sections.

No API key needed; this is pure Python aggregation over analyzed creatives.
"""
from ..base import Task, TaskContext
from ..graders import output_is_dict_no_error, regex_present, wall_budget_from_elapsed


def _run(_ctx: TaskContext):
    from intel.synthesis.creative_readout import per_brand_readout
    return per_brand_readout("bobs", window_days=60)


task = Task(
    id="R9",
    title="Per-brand creative readout (bobs) has expected sections",
    category="regression",
    runner=_run,
    graders=[
        output_is_dict_no_error(),
        regex_present("body_md", r"Bob", name="brand name in title"),
        regex_present("body_md", r"photography_style|production_style|hook_style",
                      name="attribute breakdowns present"),
        wall_budget_from_elapsed(3.0),
    ],
)
