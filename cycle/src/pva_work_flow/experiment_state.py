"""
Layer 1.1 — Structured Experiment State Table (docs/design/hydrogel_agent_optimization_plan.md)

Provides typed data structures, validation, and summarisation for wet-lab
experiment rounds so that every formula record is machine-readable and
traceable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from .wetlab_outcomes import has_failure, outcome_status, performance_rank, rank_key


# ---- TypedDict schemas ----
class CompositionDict(TypedDict, total=False):
    PVA: Dict[str, Any]
    water: Dict[str, Any]
    crosslinker: Dict[str, Any]
    additives: List[Dict[str, Any]]


class FormulaRecordDict(TypedDict, total=False):
    formula_id: str
    parent_formula_id: Optional[str]
    design_type: str
    changed_variables: List[Any]
    fixed_variables: List[str]
    audit_status: str
    experimental_status: str
    wet_experiment_completed: bool
    total_mass_g: float
    composition: Dict[str, Any]
    processing: Dict[str, Any]
    observations: Dict[str, Any]
    interpretation: Dict[str, Any]


class ExperimentRoundDict(TypedDict, total=False):
    project_goal: str
    round_id: str
    load_condition: str
    evaluation_metrics: List[str]
    allowed_materials: List[str]
    forbidden_materials: List[str]
    formula_records: List[FormulaRecordDict]


REQUIRED_FORMULA_FIELDS = [
    "formula_id",
    "design_type",
    "composition",
    "processing",
    "observations",
]

REQUIRED_OBSERVATION_FIELDS = [
    "friction_coefficient",
    "gelation_status",
    "sample_integrity",
]

REQUIRED_INTERPRETATION_FIELDS = [
    "performance_rank",
    "main_advantage",
    "main_problem",
]


def _float_or_default(value: Any, default: float = 999.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ---- I/O functions ----
def load_experiment_round(path: str | Path) -> ExperimentRoundDict:
    """Read an experiment round from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_experiment_round(round_obj: ExperimentRoundDict, path: str | Path) -> None:
    """Write an experiment round to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(round_obj, f, ensure_ascii=False, indent=2)


# ---- Validation ----
def validate_formula_record(record: Dict[str, Any]) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errors: list[str] = []

    for field in REQUIRED_FORMULA_FIELDS:
        if not record.get(field):
            errors.append(f"missing required field: {field}")

    observations = record.get("observations") or {}
    if isinstance(observations, dict):
        for field in REQUIRED_OBSERVATION_FIELDS:
            if field not in observations or observations.get(field) in (None, ""):
                errors.append(f"missing observation field: {field}")

    interpretation = record.get("interpretation") or {}
    if isinstance(interpretation, dict):
        if not interpretation.get("performance_rank"):
            errors.append("missing interpretation.performance_rank")

    if not record.get("parent_formula_id") and not record.get("source_conclusion"):
        dt = record.get("design_type", "")
        if dt not in ("initial_exploration",):
            errors.append("missing parent_formula_id or source_conclusion for non-initial formula")

    return errors


def validate_experiment_round(round_obj: Dict[str, Any]) -> dict:
    """Full round validation: returns {passed, errors, warnings}."""
    errors: list[str] = []
    warnings: list[str] = []

    if not round_obj.get("round_id"):
        errors.append("round missing round_id")

    records = round_obj.get("formula_records") or []
    if not records:
        errors.append("round has no formula_records")

    for rec in records:
        rec_errors = validate_formula_record(rec)
        for e in rec_errors:
            errors.append(f"{rec.get('formula_id', '?')}: {e}")

    dt_counts: dict[str, int] = {}
    for rec in records:
        dt = rec.get("design_type", "unknown")
        dt_counts[dt] = dt_counts.get(dt, 0) + 1

    if "baseline_reproduction" not in dt_counts:
        warnings.append("no baseline_reproduction formula found")
    if dt_counts.get("limited_exploration", 0) > len(records) * 0.25:
        warnings.append("limited_exploration exceeds 25% of total")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---- Summarisation (for LLM prompt injection) ----
def summarize_round(round_obj: Dict[str, Any]) -> str:
    """Convert structured experiment state into a concise LLM-readable summary."""
    lines: list[str] = []
    round_id = round_obj.get("round_id", "?")
    lines.append(f"=== Round {round_id} Experiment Summary ===")

    records = round_obj.get("formula_records") or []
    if not records:
        return f"Round {round_id}: no records."

    # Best formulas: failed/unusable rows are ranked after valid measurements.
    ranked = sorted(
        records,
        key=lambda r: (
            2 if r.get("experimental_status") == "experimental_failed" else 0,
            _float_or_default(r.get("observations", {}).get("friction_coefficient")),
        ),
    )
    lines.append(f"\nBest performers (failure-aware, then by COF):")
    for r in ranked[:3]:
        obs = r.get("observations", {}) or {}
        interp = r.get("interpretation", {}) or {}
        lines.append(
            f"  {r.get('formula_id', '?')}: "
            f"COF={obs.get('friction_coefficient', '?')}, "
            f"gel={obs.get('gelation_status', '?')}, "
            f"integrity={obs.get('sample_integrity', '?')}, "
            f"rank={interp.get('performance_rank', '?')}"
        )

    # Failures
    failures = [
        r for r in records
        if r.get("experimental_status") == "experimental_failed"
        or (r.get("observations", {}).get("failure_notes") or "").strip()
    ]
    if failures:
        lines.append(f"\nFormulas with failures:")
        for r in failures:
            obs = r.get("observations", {}) or {}
            lines.append(
                f"  {r.get('formula_id', '?')}: "
                f"{obs.get('failure_notes', '?')}"
            )

    # Variable summary
    positive_factors: set[str] = set()
    negative_factors: set[str] = set()
    for r in records:
        interp = r.get("interpretation", {}) or {}
        for pf in interp.get("possible_positive_factors", []) or []:
            positive_factors.add(str(pf))
        for nf in interp.get("possible_negative_factors", []) or []:
            negative_factors.add(str(nf))

    if positive_factors:
        lines.append(f"\nPossible positive factors: {', '.join(sorted(positive_factors))}")
    if negative_factors:
        lines.append(f"\nPossible negative factors: {', '.join(sorted(negative_factors))}")

    return "\n".join(lines)


# ---- Build experiment round from pipeline data ----
def build_experiment_round_from_pipeline(
    round_id: str,
    candidates: list[dict],
    results_rows: list[dict] | None = None,
    project_goal: str = "Develop low-friction PVA-based hydrogel under 10N load while maintaining gel integrity and reproducibility.",
) -> ExperimentRoundDict:
    """Convert pipeline candidates + results into a structured ExperimentRound."""
    by_id: dict[str, dict] = {}
    if results_rows:
        for r in results_rows:
            cid = r.get("candidate_id", "")
            if cid:
                by_id[cid] = r

    records: list[FormulaRecordDict] = []
    for c in candidates:
        cid = c.get("candidate_id", "?")
        f = c.get("formulation", {}) or {}
        p = c.get("processing", {}) or {}
        r = by_id.get(cid, {})

        # Build composition
        composition: dict[str, Any] = {
            "PVA": {
                "wt_percent": f.get("pva_wt_percent"),
            },
        }
        cl = f.get("crosslinker") or {}
        if isinstance(cl, dict) and cl.get("name"):
            composition["crosslinker"] = {
                "name": cl.get("name"),
                "wt_percent": cl.get("wt_percent"),
            }
        adds = f.get("additives", []) or []
        if adds:
            composition["additives"] = [
                {"name": a.get("name"), "wt_percent": a.get("wt_percent"), "role": a.get("role")}
                for a in adds if isinstance(a, dict)
            ]

        # Build observations and keep experimental failures out of success ranking.
        cof = None
        try:
            cof = float(r.get("cof_steady_mean", 0))
        except (ValueError, TypeError):
            pass
        exp_status = outcome_status(r) if r else c.get("experimental_status", "not_measured")
        failure_note = r.get("notes", "") if r else ""
        if r and has_failure(r):
            failure_bits = [x for x in (r.get("failure_type", ""), r.get("notes", "")) if str(x).strip()]
            failure_note = "; ".join(str(x) for x in failure_bits)

        observations: dict[str, Any] = {
            "friction_coefficient": cof,
            "load": "10 N",
            "friction_pattern": r.get("friction_pattern", ""),
            "wear_proxy": r.get("wear_proxy", ""),
            "compression_modulus_MPa": r.get("compression_modulus_MPa", ""),
            "failure_notes": failure_note,
        }

        # Build interpretation (heuristic)
        rank = performance_rank(r) if r else "unknown"

        interpretation: dict[str, Any] = {
            "performance_rank": rank,
            "main_advantage": f"COF={cof}" if cof is not None else "unknown",
            "main_problem": failure_note or r.get("friction_pattern", "unknown"),
            "failure_aware_rank_key": list(rank_key(r)) if r else [3, 999.0, 999.0],
            "next_round_suggestion": "",
        }

        records.append({
            "formula_id": cid,
            "parent_formula_id": c.get("parent_candidate_id"),
            "design_type": c.get("design_type", "local_optimization"),
            "changed_variables": c.get("changed_variables", []),
            "fixed_variables": c.get("fixed_variables", []),
            "audit_status": c.get("audit_status", "not_audited"),
            "experimental_status": exp_status,
            "wet_experiment_completed": bool(r),
            "total_mass_g": 20.0,
            "composition": composition,
            "processing": {
                "freeze_thaw_cycles": p.get("freeze_thaw_cycles"),
                "post_soak_hours": p.get("post_soak_hours"),
                "crosslink_or_phys_method": f.get("crosslink_or_phys_method"),
            },
            "observations": observations,
            "interpretation": interpretation,
        })

    return {
        "project_goal": project_goal,
        "round_id": round_id,
        "load_condition": "10 N",
        "evaluation_metrics": [
            "friction_coefficient", "gelation_status", "sample_integrity",
            "mechanical_stability", "transparency", "uniformity", "reproducibility",
        ],
        "allowed_materials": [],
        "forbidden_materials": [],
        "formula_records": records,
    }
