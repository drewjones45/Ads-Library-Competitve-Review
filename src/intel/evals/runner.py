"""Runner: discover tasks, run them against a frozen seed.db, score, persist.

Reproducibility: seed.db is copied to a tmp file per run; INTEL_DB_PATH is
pointed at that copy so functions under test hit the fixture, never the live
db. The eval bookkeeping (eval_runs, task results) writes to the REAL db
under data/intel.db so history persists across runs.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from rich.console import Console

from .base import GraderOutcome, Task, TaskContext, TaskRunResult

console = Console()

EVALS_PKG = Path(__file__).parent
FIXTURES_DIR = EVALS_PKG / "fixtures"
TASKS_DIR = EVALS_PKG / "tasks"
SEED_DB = FIXTURES_DIR / "seed.db"


def _real_db_path() -> Path:
    """The persistent production db. Eval bookkeeping always writes here."""
    # Resolve from the project root, NOT from the (mutable) env var.
    return Path(__file__).resolve().parents[3] / "data" / "intel.db"


def discover_tasks(task_dir: Path | None = None) -> list[Task]:
    """Import every tasks/*.py module that exposes a `task` symbol."""
    d = task_dir or TASKS_DIR
    tasks: list[Task] = []
    if not d.exists():
        return tasks
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"intel.evals.tasks.{f.stem}", f)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        t = getattr(mod, "task", None)
        if isinstance(t, Task):
            tasks.append(t)
    return tasks


def filter_tasks(tasks: list[Task], task_filter: str | None) -> list[Task]:
    if not task_filter:
        return tasks
    wanted = {x.strip() for x in task_filter.split(",")}
    return [t for t in tasks if t.id in wanted or t.category in wanted]


@contextmanager
def _seeded_db() -> Iterator[Path]:
    """Copy seed.db to a tmp path; point INTEL_DB_PATH there for tasks under test.

    Tasks call `intel.storage.connect()` which reads INTEL_DB_PATH at call time
    (it does — see storage.connect re-reads p = Path(db_path or DB_PATH) and
    DB_PATH is module-level). To make this work without reloading config we
    pass the path explicitly via env, and reload the cached config.
    """
    if not SEED_DB.exists():
        raise FileNotFoundError(
            f"seed.db missing at {SEED_DB}. Run `intel evals build-seed` first."
        )
    prior_db = os.environ.get("INTEL_DB_PATH")
    with tempfile.TemporaryDirectory(prefix="intel_eval_") as td:
        tmp_db = Path(td) / "intel.db"
        shutil.copy2(SEED_DB, tmp_db)
        os.environ["INTEL_DB_PATH"] = str(tmp_db)
        # storage.py & config.py snapshot DB_PATH at import; reload so tasks see the tmp path.
        for mod_name in ("intel.config", "intel.storage"):
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
        try:
            yield tmp_db
        finally:
            if prior_db is None:
                os.environ.pop("INTEL_DB_PATH", None)
            else:
                os.environ["INTEL_DB_PATH"] = prior_db
            for mod_name in ("intel.config", "intel.storage"):
                if mod_name in sys.modules:
                    importlib.reload(sys.modules[mod_name])


def _git_sha() -> str | None:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_real_db_path().parents[1]),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def run_task(task: Task, ctx: TaskContext) -> TaskRunResult:
    """Run a single task. Catches exceptions; reports as ERROR status."""
    ctx.task_id = task.id

    if task.requires_anthropic_key and not ctx.has_anthropic_key:
        return TaskRunResult(
            task_id=task.id, title=task.title, category=task.category,
            status="SKIP", elapsed_ms=0,
            skipped_reason="ANTHROPIC_API_KEY not set",
        )

    t0 = time.perf_counter()
    output: object = None
    err: str | None = None
    try:
        output = task.runner(ctx)
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1000:]}"
    elapsed = time.perf_counter() - t0
    elapsed_ms = int(elapsed * 1000)
    setattr(ctx, "_elapsed_sec", elapsed)

    if err is not None:
        return TaskRunResult(
            task_id=task.id, title=task.title, category=task.category,
            status="ERROR", elapsed_ms=elapsed_ms, error=err,
        )

    outcomes: list[GraderOutcome] = []
    for g in task.graders:
        try:
            outcomes.append(g(output, ctx))
        except Exception as e:
            outcomes.append(GraderOutcome(
                name=getattr(g, "__name__", "grader"),
                passed=False,
                detail=f"raised: {type(e).__name__}: {e}",
            ))

    correctness = [o for o in outcomes if not o.is_speed]
    speed = [o for o in outcomes if o.is_speed]
    if not all(o.passed for o in correctness):
        status = "FAIL"
    elif not all(o.passed for o in speed):
        status = "PASS_SLOW"
    else:
        status = "PASS"

    return TaskRunResult(
        task_id=task.id, title=task.title, category=task.category,
        status=status, elapsed_ms=elapsed_ms,
        grader_outcomes=outcomes, output=output,
    )


def run_suite(tasks: list[Task], *, notes: str | None = None) -> tuple[int, list[TaskRunResult]]:
    """Run all tasks against the seeded fixture db. Returns (run_id, results)."""
    from . import _stub_llm
    stub_on = _stub_llm.stub_enabled()
    # In stub mode the LLM-backed tasks must NOT skip — they need to exercise
    # the real schema + parsing + grader path against the canned responses.
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY")) or stub_on
    real_db = _real_db_path()

    # Ensure the real db has the latest schema (eval_runs etc. live there).
    # We import storage WITHOUT swapping INTEL_DB_PATH first.
    from intel.storage import init_db as _init_db
    _init_db(real_db)

    # Open a long-lived connection on the REAL db for bookkeeping. This is
    # independent of the seeded db that tasks run against.
    book = sqlite3.connect(real_db)
    book.row_factory = sqlite3.Row

    from intel.storage import start_eval_run, finish_eval_run, record_task_result

    run_id = start_eval_run(book, git_sha=_git_sha(), notes=notes)
    book.commit()

    results: list[TaskRunResult] = []
    stub_saved: list[tuple[str, object]] = []
    prior_api_key: str | None = None
    if stub_on:
        console.print("[yellow]INTEL_LLM_STUB=1 → using stub LLM client for this run[/yellow]")
        stub_saved = _stub_llm.install_stub()
        # Some modules (briefing.generate_briefing, anything that calls
        # MissingApiKey._require_key) gate on os.environ['ANTHROPIC_API_KEY']
        # directly rather than the runner's has_key flag. Plant a placeholder
        # so the real code paths run; the stub client intercepts the actual
        # API call so no network traffic happens.
        prior_api_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "stub-eval-key"
    try:
        with _seeded_db() as tmp_db:
            ctx = TaskContext(
                fixtures_dir=FIXTURES_DIR,
                seed_db_path=tmp_db,
                eval_run_id=run_id,
                has_anthropic_key=has_key,
            )
            for task in tasks:
                console.print(f"  → [bold]{task.id}[/bold] {task.title}", end="")
                result = run_task(task, ctx)
                color = {"PASS": "green", "PASS_SLOW": "yellow", "FAIL": "red",
                         "SKIP": "dim", "ERROR": "red"}[result.status]
                console.print(f"  [{color}]{result.status}[/{color}] ({result.elapsed_ms}ms)")
                results.append(result)
                record_task_result(
                    book, run_id,
                    task_id=result.task_id, title=result.title,
                    category=result.category, status=result.status,
                    elapsed_ms=result.elapsed_ms,
                    graders=[o.to_dict() for o in result.grader_outcomes],
                    output=result.output,
                    error=result.error,
                    skipped_reason=result.skipped_reason,
                )
                book.commit()
    finally:
        n_pass = sum(1 for r in results if r.status == "PASS")
        n_slow = sum(1 for r in results if r.status == "PASS_SLOW")
        n_fail = sum(1 for r in results if r.status in ("FAIL", "ERROR"))
        n_skip = sum(1 for r in results if r.status == "SKIP")
        scoreable = n_pass + n_slow + n_fail
        headline = (n_pass + 0.5 * n_slow) / scoreable if scoreable else 0.0
        finish_eval_run(
            book, run_id,
            task_count=len(results), pass_count=n_pass,
            pass_slow_count=n_slow, fail_count=n_fail,
            skip_count=n_skip, headline_score=headline,
        )
        book.commit()
        book.close()
        if stub_saved:
            _stub_llm.restore_stub(stub_saved)
        if stub_on:
            if prior_api_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prior_api_key

    return run_id, results
