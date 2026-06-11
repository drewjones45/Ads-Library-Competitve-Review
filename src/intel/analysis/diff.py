from __future__ import annotations

import difflib
import json
from typing import Any


def diff_text(before: str | None, after: str | None, *, max_lines: int = 80) -> str:
    """Return a compact unified-style diff string. Both inputs may be None."""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    if a == b:
        return ""
    diff = list(difflib.unified_diff(a, b, lineterm="", n=2))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more diff lines truncated)"]
    return "\n".join(diff)


def diff_parsed_observations(
    before_parsed: dict | None, after_parsed: dict | None
) -> dict[str, Any]:
    """Field-level diff for parsed observation payloads (Section 17 structured diff)."""
    if before_parsed is None:
        return {"first_seen": True, "fields_changed": list((after_parsed or {}).keys())}
    out: dict[str, Any] = {"first_seen": False, "fields_changed": [], "details": {}}
    keys = set((before_parsed or {}).keys()) | set((after_parsed or {}).keys())
    for k in sorted(keys):
        b = (before_parsed or {}).get(k)
        a = (after_parsed or {}).get(k)
        if b == a:
            continue
        out["fields_changed"].append(k)
        if isinstance(a, str) or isinstance(b, str):
            out["details"][k] = {"before": b, "after": a}
        else:
            out["details"][k] = {"before": b, "after": a}
    return out


def classify_change_severity(diff_details: dict, weights: dict[str, float]) -> tuple[float, str]:
    """Crude severity = sum of per-field weights, capped at 1.0.
    Returns (severity, reason). Used to suppress trivial changes from alerting."""
    if not diff_details.get("fields_changed"):
        return 0.0, "no fields changed"
    score = 0.0
    reasons: list[str] = []
    field_to_weight = {
        "regions": weights.get("hero", 0.7),
        "meta": weights.get("hero", 0.5),
        "visible_text_excerpt": weights.get("hero", 0.6),
        "active_archive_ids": weights.get("creative", 1.0),
        "ad_count": weights.get("creative", 0.4),
    }
    for f in diff_details.get("fields_changed", []):
        w = field_to_weight.get(f, 0.2)
        score += w
        reasons.append(f"{f} (+{w:.1f})")
    return min(1.0, score), ", ".join(reasons)


def summarize_diff_for_log(diff: dict) -> str:
    """One-line human summary for storage.change_summary."""
    if diff.get("first_seen"):
        return "first observation"
    changed = diff.get("fields_changed") or []
    return f"{len(changed)} field(s) changed: {', '.join(changed[:6])}" + (
        " …" if len(changed) > 6 else ""
    )


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return str(obj)
