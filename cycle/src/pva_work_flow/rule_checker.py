"""
Layer 1.4 — Rule Checker (docs/design/hydrogel_agent_optimization_plan.md)

Standalone hard-constraint checker for DOE plans and candidate formulas.
Runs before final output; returns {passed, errors, warnings}.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .candidate_rules import changed_variable_names, validate_candidate_constraints


# ---- Individual rule checks ----
def check_allowed_materials(
    formula: Dict[str, Any],
    allowed_materials: list[str],
) -> list[str]:
    """Rule 8: every material must be in the allowed list."""
    errors: list[str] = []
    allowed_set = {m.strip().lower() for m in allowed_materials if m.strip()}
    if not allowed_set:
        return errors  # skip if no allowlist defined

    # Always-allowed roles: solvent=water, main_polymer=PVA
    _ALWAYS_ALLOWED = {"di water", "water", "pva", "polyvinyl alcohol", "pva (polyvinyl alcohol)"}
    for m in formula.get("materials", []) or []:
        name = str(m.get("name", "")).strip().lower()
        role = str(m.get("role", "")).strip().lower()
        if not name or name == "none":
            continue
        # Always allow water (universal solvent) and PVA (required main polymer)
        if name in _ALWAYS_ALLOWED or role in ("solvent",) and ("water" in name or "h2o" in name):
            continue
        if "pva" in name or "polyvinyl" in name:
            continue
        if name not in allowed_set:
            # fuzzy check
            if not any(name in a or a in name for a in allowed_set):
                errors.append(
                    f"{formula.get('candidate_id', '?')}: material '{name}' not in allowed_materials"
                )
    return errors


def check_required_lineage_fields(plan: Dict[str, Any]) -> list[str]:
    """Check that every lineage table entry has required fields."""
    errors: list[str] = []
    table = plan.get("inheritance_table", plan.get("formula_lineage_table", []))
    required = [
        "next_id", "design_type", "parent_id",
        "design_rationale", "black_box_risk",
    ]
    for entry in table:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        for field in required:
            if field not in entry or entry.get(field) in (None, "", []):
                errors.append(f"{eid}: missing required lineage field '{field}'")
    return errors


def check_design_type_distribution(
    lineage_table: list[Dict[str, Any]],
    total_n: int,
) -> list[str]:
    """Rules 4-7: enforce formula type distribution."""
    errors: list[str] = []
    counts: dict[str, int] = {}
    for entry in lineage_table:
        dt = entry.get("design_type", "unknown")
        counts[dt] = counts.get(dt, 0) + 1

    n = total_n or len(lineage_table)

    # Rule 4: baseline_reproduction is optional. Reproducibility checks should
    # be scheduled as separate validation work, not forced into every tree step.

    # Rule 5: at least 50% local_optimization or single_factor_perturbation
    local_count = counts.get("local_optimization", 0) + counts.get("single_factor_perturbation", 0)
    if n > 0 and local_count < n * 0.5:
        errors.append(f"Rule 5 FAIL: local_optimization + single_factor ({local_count}) < 50% of total ({n})")

    # Rule 6: failure verification is optional in the simplified constrained mode.
    fcv_count = counts.get("failure_cause_validation", counts.get("failure_verification", 0))
    if fcv_count > 1:
        errors.append(f"Rule 6 FAIL: failure_verification ({fcv_count}) exceeds 1 per round")

    # Rule 7: limited_exploration <= 1 per round
    expl_count = counts.get("limited_exploration", 0)
    if expl_count > 1:
        errors.append(f"Rule 7 FAIL: limited_exploration ({expl_count}) exceeds 1 per round")

    return errors


def check_black_box_risk(
    lineage_table: list[Dict[str, Any]],
    max_score: int = 3,
) -> list[str]:
    """Rule 9: black_box_risk_score must be <= max_score."""
    errors: list[str] = []
    for entry in lineage_table:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        risk = int(entry.get("black_box_risk", entry.get("black_box_risk_score", 0)))
        if risk > max_score:
            errors.append(f"{eid}: black_box_risk={risk} exceeds max={max_score}")
    return errors


def check_max_variable_changes(
    lineage_table: list[Dict[str, Any]],
    max_changed: int = 2,
) -> list[str]:
    """Rule 3: each formula changes at most max_changed key variables."""
    errors: list[str] = []
    for entry in lineage_table:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        dt = entry.get("design_type", "")
        changed = changed_variable_names(entry.get("changed_variables", entry.get("variables_changed", [])))
        limit = max_changed
        if dt in ("single_factor_perturbation",):
            if len(changed) != 1:
                errors.append(f"{eid}: single_factor_perturbation must change exactly 1 variable, got {len(changed)}")
            continue
        if dt in ("failure_verification", "baseline_reproduction"):
            limit = 1 if dt == "failure_verification" else 0
        if len(changed) > limit:
            errors.append(f"{eid}: changes {len(changed)} variables (max {limit})")
    return errors


def check_hypothesis_fields(lineage_table: list[Dict[str, Any]]) -> list[str]:
    """Rule 10: every entry must have hypothesis, expected, better/worse.
    if_better and if_worse are MANDATORY for all design types."""
    errors: list[str] = []
    for entry in lineage_table:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        for field in ("expected_outcome", "if_better", "if_worse"):
            val = entry.get(field, "")
            if not str(val).strip():
                errors.append(f"{eid}: missing '{field}' — this is MANDATORY for traceable iteration")
        if not entry.get("design_rationale") and not entry.get("hypothesis"):
            errors.append(f"{eid}: missing design_rationale/hypothesis")
    return errors


def check_parent_traceability(lineage_table: list[Dict[str, Any]]) -> list[str]:
    """Rules 1-2: every formula must have parent_id or source_conclusion, and a design_type."""
    errors: list[str] = []
    for entry in lineage_table:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        dt = entry.get("design_type", "")
        parent = entry.get("parent_id", entry.get("parent_formula_id", entry.get("parent_candidate_id", "")))
        source = entry.get("source_conclusion", "")

        if not dt:
            errors.append(f"{eid}: missing design_type")

        if not parent:
            errors.append(f"{eid}: missing parent_id for design_type={dt}")
        if dt == "limited_exploration" and not source and not entry.get("design_rationale"):
            errors.append(f"{eid}: limited_exploration without source_conclusion/design_rationale")

        # baseline_reproduction must have ZERO variables_changed
        if dt == "baseline_reproduction":
            changed = changed_variable_names(entry.get("changed_variables", entry.get("variables_changed", [])))
            if len(changed) > 0:
                errors.append(
                    f"{eid}: baseline_reproduction must have zero variables_changed, "
                    f"got {len(changed)}: {changed}"
                )

    return errors


# ---- Orchestrator ----
def run_all_rule_checks(
    plan: Dict[str, Any],
    allowed_materials: list[str] | None = None,
    formulas: list[Dict[str, Any]] | None = None,
    parent_by_id: dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Run all 10 rules. Returns {passed, errors, warnings}."""
    all_errors: list[str] = []
    all_warnings: list[str] = []

    table = plan.get("inheritance_table", plan.get("formula_lineage_table", []))
    total_n = plan.get("total_count", plan.get("accepted_count", len(table)))

    # Rule 1-2: parent traceability + design_type
    all_errors.extend(check_parent_traceability(table))

    # Rule 3: max variable changes
    all_errors.extend(check_max_variable_changes(table))

    # Rule 4-7: type distribution
    all_errors.extend(check_design_type_distribution(table, total_n))

    # Rule 8: allowed materials
    if formulas:
        limited_count = sum(1 for f in formulas if f.get("design_type") == "limited_exploration")
        for f in formulas:
            if allowed_materials:
                all_errors.extend(check_allowed_materials(f, allowed_materials))
            all_errors.extend(
                validate_candidate_constraints(
                    f,
                    parent_by_id=parent_by_id or {},
                    round_limited_exploration_count=limited_count,
                    require_parent=bool(parent_by_id),
                )
            )

    # Rule 9: black box risk
    all_errors.extend(check_black_box_risk(table))

    # Rule 10: hypothesis fields
    hyp_errors = check_hypothesis_fields(table)
    all_errors.extend(hyp_errors)

    # Warnings (non-blocking)
    if total_n > 0:
        expl_count = sum(1 for e in table if e.get("design_type") == "limited_exploration")
        if expl_count > 0:
            all_warnings.append(f"Contains {expl_count} limited_exploration formula(s) — ensure justification is provided")

    return {
        "passed": len(all_errors) == 0,
        "errors": all_errors,
        "warnings": all_warnings,
    }
