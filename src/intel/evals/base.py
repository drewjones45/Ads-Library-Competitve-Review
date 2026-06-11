"""Core types for the eval harness.

A Task = (runner function) + (list of graders). Runner gets a TaskContext with
the fixture paths + run id; returns whatever the function under test returns.
Each grader inspects that output and emits a GraderOutcome.

Speed graders are special: a FAIL on a speed grader (only) is reported as
PASS_SLOW (bible: "correct but slow is still a failure" — partial credit 0.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


@dataclass
class TaskContext:
    """Handed to each task's runner. Holds fixture paths + run identifiers."""
    fixtures_dir: Path
    seed_db_path: Path
    eval_run_id: int | None = None
    task_id: str = ""
    has_anthropic_key: bool = False

    def fixture(self, *parts: str) -> Path:
        return self.fixtures_dir.joinpath(*parts)


@dataclass
class GraderOutcome:
    name: str
    passed: bool
    is_speed: bool = False        # speed graders: FAIL → PASS_SLOW (not full FAIL)
    detail: str = ""              # short explanation surfaced in dashboard

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "is_speed": self.is_speed, "detail": self.detail}


# A grader takes the task output + context, returns an outcome.
Grader = Callable[[Any, TaskContext], GraderOutcome]

# A runner produces the output to be graded. Should be deterministic for fixed seed.
Runner = Callable[[TaskContext], Any]


@dataclass
class Task:
    id: str
    title: str
    category: Literal["regression", "failure_mode"]
    runner: Runner
    graders: list[Grader]
    timeout_sec: int = 120
    requires_anthropic_key: bool = False
    # Estimated $ cost ceiling — used for "don't accidentally spend $50" sanity check
    cost_ceiling_usd: float = 0.50


@dataclass
class TaskRunResult:
    task_id: str
    title: str
    category: str
    status: Literal["PASS", "PASS_SLOW", "FAIL", "SKIP", "ERROR"]
    elapsed_ms: int
    grader_outcomes: list[GraderOutcome] = field(default_factory=list)
    output: Any = None
    error: str | None = None
    skipped_reason: str | None = None

    def score_value(self) -> float:
        """Partial-credit scoring per bible §6."""
        if self.status == "PASS":
            return 1.0
        if self.status == "PASS_SLOW":
            return 0.5
        return 0.0  # FAIL, ERROR, SKIP all worth 0
