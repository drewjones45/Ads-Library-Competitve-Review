"""Grader library — deterministic checks + LLM-judge stub.

All graders return a GraderOutcome. Compose with `composite()` or use directly
in a Task's graders list. LLM judge is stubbed in Phase 0 (returns score=1.0)
and becomes real in Phase 3 when the judge sub-agent ships.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

from .base import GraderOutcome, TaskContext


# ---------- deterministic ----------

def exact_match(path: str, expected: Any, *, name: str | None = None):
    """JSON-path style: dotted key access into output dict.
    Example: exact_match("photography_style", "lifestyle")"""
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        ok = actual == expected
        return GraderOutcome(
            name=name or f"{path} == {expected!r}",
            passed=ok,
            detail=f"got {actual!r}",
        )
    return _g


def in_set(path: str, allowed: set, *, name: str | None = None):
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        ok = actual in allowed
        return GraderOutcome(
            name=name or f"{path} in {sorted(allowed)}",
            passed=ok,
            detail=f"got {actual!r}",
        )
    return _g


def at_least(path: str, minimum: float, *, name: str | None = None):
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        try:
            ok = float(actual) >= minimum
        except (TypeError, ValueError):
            ok = False
        return GraderOutcome(
            name=name or f"{path} >= {minimum}",
            passed=ok,
            detail=f"got {actual!r}",
        )
    return _g


def list_non_empty(path: str, *, name: str | None = None):
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        ok = isinstance(actual, list) and len(actual) > 0
        return GraderOutcome(
            name=name or f"{path} non-empty",
            passed=ok,
            detail=f"len={len(actual) if isinstance(actual, list) else 'n/a'}",
        )
    return _g


def list_has_kind(path: str, kind: str, *, name: str | None = None):
    """For lists of dicts with a 'kind' field — assert at least one has the given kind."""
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        if not isinstance(actual, list):
            return GraderOutcome(name=name or f"{path} contains kind={kind}", passed=False, detail="not a list")
        kinds = [item.get("kind") for item in actual if isinstance(item, dict)]
        ok = kind in kinds
        return GraderOutcome(
            name=name or f"{path} contains kind={kind}",
            passed=ok,
            detail=f"kinds={kinds}",
        )
    return _g


def regex_present(path: str, pattern: str, *, name: str | None = None, flags: int = 0):
    rx = re.compile(pattern, flags)
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        text = str(actual or "")
        ok = bool(rx.search(text))
        return GraderOutcome(
            name=name or f"regex /{pattern}/ in {path}",
            passed=ok,
            detail=f"text[:80]={text[:80]!r}",
        )
    return _g


def regex_absent(path: str, pattern: str, *, name: str | None = None, flags: int = 0):
    rx = re.compile(pattern, flags)
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        actual = _dotted(out, path)
        text = str(actual or "")
        m = rx.search(text)
        return GraderOutcome(
            name=name or f"regex /{pattern}/ absent in {path}",
            passed=m is None,
            detail=f"match={m.group(0)[:40]!r}" if m else "ok",
        )
    return _g


def schema_valid(validator: Callable[[Any], tuple[bool, str]], *, name: str = "schema_valid"):
    """validator(out) -> (ok, detail). Phase 1 will swap in pydantic models."""
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        try:
            ok, detail = validator(out)
        except Exception as e:
            ok, detail = False, f"validator raised: {type(e).__name__}: {e}"
        return GraderOutcome(name=name, passed=ok, detail=detail)
    return _g


def output_is_dict_no_error(*, name: str = "no_error"):
    """Common case: function returned a dict and didn't set an 'error' key."""
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        if not isinstance(out, dict):
            return GraderOutcome(name=name, passed=False, detail=f"not a dict: {type(out).__name__}")
        if "error" in out:
            return GraderOutcome(name=name, passed=False, detail=f"error: {str(out['error'])[:80]}")
        return GraderOutcome(name=name, passed=True, detail="ok")
    return _g


def db_row_exists(table: str, where: str, params: tuple = (), *, name: str | None = None):
    """Side-effect grader: verify a row was written to the DB (action_taken)."""
    def _g(_out: Any, ctx: TaskContext) -> GraderOutcome:
        with sqlite3.connect(ctx.seed_db_path) as c:
            row = c.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params).fetchone()
        ok = row is not None
        return GraderOutcome(
            name=name or f"row exists in {table} where {where}",
            passed=ok,
            detail="found" if ok else "missing",
        )
    return _g


# ---------- speed graders (failure here → PASS_SLOW) ----------

def wall_budget(budget_sec: float, elapsed_provider: Callable[[TaskContext], float], *, name: str | None = None):
    """budget_sec: max acceptable wall time. elapsed_provider reads from ctx."""
    def _g(_out: Any, ctx: TaskContext) -> GraderOutcome:
        elapsed = elapsed_provider(ctx)
        ok = elapsed <= budget_sec
        return GraderOutcome(
            name=name or f"wall_time <= {budget_sec}s",
            passed=ok,
            is_speed=True,
            detail=f"elapsed={elapsed:.2f}s",
        )
    return _g


def wall_budget_from_elapsed(budget_sec: float, *, name: str | None = None):
    """Convenience: reads ctx._elapsed_sec set by the runner."""
    return wall_budget(budget_sec, lambda c: getattr(c, "_elapsed_sec", 0.0), name=name)


# ---------- LLM-judge (Phase 3 will replace stub) ----------

def llm_judge(rubric: str, *, min_score: float = 0.7, name: str | None = None):
    """Stub: returns score=1.0 until Phase 3 ships intel.agent.judge."""
    def _g(out: Any, _ctx: TaskContext) -> GraderOutcome:
        try:
            from ..agent.judge import judge_artifact  # type: ignore
            res = judge_artifact(out, rubric=rubric)
            score = float(res.get("score", 0.0))
            ok = score >= min_score
            return GraderOutcome(
                name=name or f"llm_judge[{rubric}] >= {min_score}",
                passed=ok,
                detail=f"score={score:.2f} · violations={res.get('violations', [])[:2]}",
            )
        except ImportError:
            return GraderOutcome(
                name=name or f"llm_judge[{rubric}] (stub)",
                passed=True,
                detail="phase-3 judge not built yet — passing as stub",
            )
    return _g


# ---------- helpers ----------

def _dotted(obj: Any, path: str) -> Any:
    """Walk a dotted path through dicts/lists. 'a.b.0.c' → obj['a']['b'][0]['c']."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur
