"""R3 — cross-set comparison computes distinctiveness from analyzed creatives.

No API key needed; this exercises the Python aggregation in
src/intel/synthesis/creative_readout.py against the seeded db.
"""
from ..base import Task, TaskContext
from ..graders import output_is_dict_no_error, regex_present, wall_budget_from_elapsed


def _run(_ctx: TaskContext):
    from intel.synthesis.creative_readout import cross_set_comparison
    return cross_set_comparison(window_days=60)


task = Task(
    id="R3",
    title="Cross-set comparison surfaces distinctiveness + whitespace",
    category="regression",
    runner=_run,
    graders=[
        output_is_dict_no_error(),
        regex_present("body_md", r"Distinctiveness", name="has distinctiveness section"),
        regex_present("body_md", r"whitespace", name="has whitespace section"),
        regex_present("body_md", r"bobs|wayfair|ashley", name="references real brands"),
        wall_budget_from_elapsed(5.0),
    ],
)
