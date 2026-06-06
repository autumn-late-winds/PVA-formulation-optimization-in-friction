"""Materialize constrained next-round formulas from parent candidates."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .utils import read_json, canonicalize_candidate_materials


def set_planned_variable(candidate: dict, variable: str, value: Any) -> None:
    """Apply one semantic design variable change to a copied parent formula."""
    formulation = candidate.setdefault("formulation", {})
    processing = candidate.setdefault("processing", {})

    if variable == "pva_wt_percent":
        formulation["pva_wt_percent"] = value
    elif variable == "crosslinker_wt_percent":
        formulation.setdefault("crosslinker", {})["wt_percent"] = value
    elif variable == "initiator_or_catalyst_wt_percent":
        formulation.setdefault("initiator_or_catalyst", {})["wt_percent"] = value
    elif variable == "primary_additive_wt_percent":
        additives = formulation.setdefault("additives", [])
        target = None
        for additive in additives:
            if isinstance(additive, dict) and additive.get("name") not in ("", None, "none"):
                target = additive
                break
        if target is None:
            target = {"name": "parent_primary_additive", "role": "inherited additive"}
            additives.append(target)
        target["wt_percent"] = value
    elif variable == "post_soak_hours":
        processing["post_soak_hours"] = value
    elif variable == "freeze_thaw_cycles":
        processing["freeze_thaw_cycles"] = value


def materialize_constrained_candidates(
    out_dir: Path,
    round_idx: int,
    table: list[dict],
    parent_round_idx: int | None = None,
) -> list[dict]:
    """Create formulas from the deterministic DOE skeleton without LLM drift."""
    source_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    parent_path = out_dir / f"R{source_round}_candidates.json"
    parent_obj = read_json(parent_path)
    parent_by_id = {
        c.get("candidate_id"): c
        for c in parent_obj.get("candidates", []) or []
        if c.get("candidate_id")
    }
    candidates: list[dict] = []

    for entry in table:
        parent_id = entry.get("parent_id", "")
        parent = parent_by_id.get(parent_id)
        if not parent:
            raise RuntimeError(f"Constrained Formula Builder cannot find parent candidate: {parent_id}")

        candidate = deepcopy(parent)
        # Canonicalize material names immediately so changed-variable detection
        # does not treat Chinese→English name normalization as a design change.
        canonicalize_candidate_materials(candidate)
        next_id = entry.get("next_id", "")
        changed = entry.get("variables_changed", []) or []
        changed_names = [v.get("variable", "") for v in changed if v.get("variable")]

        candidate["candidate_id"] = next_id
        candidate["parent_candidate_id"] = parent_id
        candidate["parent_candidates"] = [parent_id]
        candidate["design_type"] = entry.get("design_type", candidate.get("design_type", "single_factor_perturbation"))
        candidate["planned_changed_variables"] = changed
        candidate["if_better"] = entry.get("if_better", "")
        candidate["if_worse"] = entry.get("if_worse", "")
        candidate["expected_outcome"] = entry.get("expected_outcome", "")
        candidate["mutation_rationale"] = entry.get("design_rationale", "")
        for stale_key in ("ratio_planner", "ratio_source", "ratio_rationale"):
            candidate.pop(stale_key, None)
        candidate["diagnosis_evidence_used"] = candidate.get("diagnosis_evidence_used") or [
            f"Parent {parent_id} was selected as the best candidate from round {round_idx - 1} "
            f"based on audit analysis and experimental results.",
            f"Constrained DOE skeleton assigns design_type={candidate['design_type']} "
            f"with changes {changed_names or ['none']}.",
        ]
        candidate["expected_mechanism"] = candidate.get("expected_mechanism") or [
            "Code-materialized candidate inherits the parent PVA system and changes only the planned local factor."
        ]
        candidate["risks_and_mitigations"] = candidate.get("risks_and_mitigations") or [
            "Risk: small local change may not exceed experimental noise; mitigation: compare against the measured parent and use repeats when validating finalists.",
            "Risk: process variability can mask the factor effect; mitigation: keep handling and tribology settings fixed.",
        ]

        for item in changed:
            variable = item.get("variable", "")
            if variable:
                set_planned_variable(candidate, variable, item.get("new_value"))

        candidate["doe_factor_levels"] = {
            item.get("variable"): item.get("new_value")
            for item in changed
            if item.get("variable")
        }
        metadata = candidate.setdefault("iteration_metadata", {})
        metadata["parent_candidate_id"] = parent_id
        metadata["parent_candidates"] = [parent_id]
        metadata["design_type"] = candidate["design_type"]
        metadata["planned_changed_variables"] = changed
        metadata["doe_factor_levels"] = candidate["doe_factor_levels"]

        if candidate["design_type"] == "baseline_reproduction":
            candidate["doe_factor_levels"] = {}
            metadata["doe_factor_levels"] = {}
            candidate["mutation_rationale"] = "Repeat parent formula exactly."

        print(
            f"[Formula Builder] {next_id}: inherited {parent_id}; "
            f"changed {changed_names or ['none']}"
        )
        candidates.append(candidate)

    print(f"[Formula Builder] Code-materialized {len(candidates)} constrained candidates")
    return candidates
