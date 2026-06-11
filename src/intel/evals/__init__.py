"""Evaluation harness for the intel agent — hill-climbing infrastructure.

Bible §6: "If you can't measure it, you can't improve it."

Usage:
    from intel.evals import run_suite, discover_tasks
    run_suite(discover_tasks())
"""
from .base import Task, TaskContext, GraderOutcome, TaskRunResult
from .runner import run_suite, run_task, discover_tasks

__all__ = [
    "Task", "TaskContext", "GraderOutcome", "TaskRunResult",
    "run_suite", "run_task", "discover_tasks",
]
