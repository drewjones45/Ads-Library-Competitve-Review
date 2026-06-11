"""R1 — vision taxonomy on a lifestyle image.

Requires API key + a fixture image. Validates the output is a structured dict
with the taxonomy fields and reasonable confidence.
"""
from ..base import Task, TaskContext
from ..graders import (
    in_set, at_least, output_is_dict_no_error, list_non_empty, wall_budget_from_elapsed,
)


_PHOTO_STYLES = {
    "model_on_figure", "flat_lay", "lifestyle", "studio_product_only",
    "screenshot_ui", "text_only", "mixed",
}
_HOOK_STYLES = {
    "problem_solution", "social_proof", "urgency", "founder_story", "demo",
    "testimonial", "meme", "aesthetic", "unknown",
}


def _run(ctx: TaskContext):
    from intel.analysis.creative import analyze_creative_image
    return analyze_creative_image(ctx.fixture("images", "lifestyle.jpg"))


task = Task(
    id="R1",
    title="Vision taxonomy on lifestyle image produces structured output",
    category="regression",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        in_set("photography_style", _PHOTO_STYLES, name="valid photography_style enum"),
        in_set("hook_style", _HOOK_STYLES, name="valid hook_style enum"),
        in_set("production_style",
               {"polished_brand", "ugc_creator_style", "meme_graphic", "mixed"},
               name="valid production_style enum"),
        list_non_empty("dominant_colors_hex"),
        at_least("confidence", 0.5, name="confidence >= 0.5"),
        wall_budget_from_elapsed(30.0),
    ],
)
