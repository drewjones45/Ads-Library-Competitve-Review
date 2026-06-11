"""R2 — offer extraction returns structured kind/value from a known input.

Requires API key (uses the cheap model).
"""
from ..base import Task, TaskContext
from ..graders import (
    list_has_kind, list_non_empty, regex_present, wall_budget_from_elapsed,
)

SAMPLE = (
    "Memorial Day Sale! Take 20% off your entire order with code SAVE20. "
    "Plus free shipping on orders over $50. Ends Monday."
)


def _run(_ctx: TaskContext):
    from intel.analysis.offers import extract_offers_from_text
    # Wrap in a dict so the dotted-path graders work.
    return {"offers": extract_offers_from_text(SAMPLE)}


task = Task(
    id="R2",
    title="Offer extraction finds percent_off + free_shipping + code",
    category="regression",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        list_non_empty("offers"),
        list_has_kind("offers", "percent_off"),
        list_has_kind("offers", "free_shipping"),
        regex_present("offers.0.value", r"\d", name="value contains a number"),
        wall_budget_from_elapsed(15.0),
    ],
)
