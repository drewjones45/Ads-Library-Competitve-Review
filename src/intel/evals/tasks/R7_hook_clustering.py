"""R7 — hook clustering on seeded ads produces valid cluster shapes."""
from ..base import Task, TaskContext
from ..graders import list_non_empty, output_is_dict_no_error, wall_budget_from_elapsed


def _run(ctx: TaskContext):
    import sqlite3
    from intel.analysis.themes import cluster_hooks
    with sqlite3.connect(ctx.seed_db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ad_archive_id, body_text FROM ads "
            "WHERE body_text IS NOT NULL AND length(body_text) > 20 "
            "ORDER BY first_seen DESC LIMIT 30"
        ).fetchall()
    ads = [{"ad_archive_id": r["ad_archive_id"], "body_text": r["body_text"]} for r in rows]
    return cluster_hooks(ads, vertical="furniture")


task = Task(
    id="R7",
    title="Hook clustering produces ≥1 cluster on furniture ads",
    category="regression",
    runner=_run,
    requires_anthropic_key=True,
    graders=[
        output_is_dict_no_error(),
        list_non_empty("clusters"),
        wall_budget_from_elapsed(45.0),
    ],
)
