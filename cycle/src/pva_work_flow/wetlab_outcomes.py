"""Shared wet-lab outcome classification and ranking helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .io_artifacts import aggregate_cof_from_row


NO_FAILURE_VALUES = {"", "none", "na", "n/a", "null", "no", "0"}
CRITICAL_NOTE_CODES = {"ERROR1", "ERROR2"}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def failure_type(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    return _norm(row.get("failure_type")).lower()


def has_failure(row: Mapping[str, Any] | None) -> bool:
    """Return True when a result row should not be ranked as a valid success."""
    if not row:
        return False
    failure = failure_type(row)
    if failure and failure not in NO_FAILURE_VALUES:
        return True
    notes = _norm(row.get("notes")).upper()
    cof_raw = _norm(row.get("cof_steady_mean")).upper()
    return any(code in notes or cof_raw == code for code in CRITICAL_NOTE_CODES)


def outcome_status(row: Mapping[str, Any] | None) -> str:
    """Classify one wet-lab row without mixing it with audit status."""
    if not row:
        return "not_measured"
    if has_failure(row):
        return "experimental_failed"
    cof, _std = aggregate_cof_from_row(dict(row))
    if cof is None:
        return "measured_without_cof"
    return "measured_valid"


def rank_key(row: Mapping[str, Any] | None) -> tuple[int, float, float]:
    """Lower is better; failed/missing rows are penalized before COF ranking."""
    if not row:
        return (3, 999.0, 999.0)
    cof, std = aggregate_cof_from_row(dict(row))
    cof_val = 999.0 if cof is None else float(cof)
    std_val = 999.0 if std is None else float(std)
    if has_failure(row):
        return (2, cof_val, std_val)
    if cof is None:
        return (1, 999.0, std_val)
    return (0, cof_val, std_val)


def performance_rank(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "unknown"
    if has_failure(row):
        return "failed"
    cof, _std = aggregate_cof_from_row(dict(row))
    if cof is None:
        return "unknown"
    if cof < 0.01:
        return "excellent"
    if cof < 0.02:
        return "good"
    if cof < 0.03:
        return "moderate"
    return "poor"
