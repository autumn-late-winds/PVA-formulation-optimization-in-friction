"""Code-layer candidate lineage and constrained-DOE checks.

These helpers are intentionally deterministic.  They protect the workflow from
LLM drift by deriving changed variables from parent formulas and by separating
record/audit status from wet-experiment outcome.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set, Tuple

from pva_work_flow.core.utils import canonicalize_material_name

# ── Coupled variable pairs ───────────────────────────────────────────
# Some formulations require two variables to change together (e.g.,
# acrylamide + DMSO in UV-photo IPN systems where DMSO acts as co-solvent
# for the monomer).  These pairs are exempt from the strict single-factor
# "exactly 1 variable" rule.
COUPLED_VARIABLE_PAIRS: List[Tuple[str, str]] = [
    ("formulation.additive.acrylamide.wt_percent", "formulation.additive.dmso.wt_percent"),
    ("formulation.additive.dmso.wt_percent", "formulation.additive.acrylamide.wt_percent"),
    # Future: ("formulation.additive.nvp.wt_percent", "formulation.additive.dmso.wt_percent"),
]

# ── Crosslinker materials that require specific companion additives ───
# When a crosslink_or_phys_method references one of these, the companion
# material MUST appear in either the crosslinker block or the additives list.
CROSSLINK_REQUIRED_ADDITIVES: Dict[str, List[str]] = {
    "ga": ["glutaraldehyde"],
    "glutaraldehyde": ["glutaraldehyde"],
    "ga_hcl": ["glutaraldehyde", "hydrochloric acid"],
    "ga_hcl_fast": ["glutaraldehyde", "hydrochloric acid"],
    "epoxy": ["epoxy"],
    "borax": ["borax"],
    "uv_photo": [],  # photoinitiator is in the initiator block, not additives
}


_METADATA_KEYS = {
    "candidate_id",
    "parent_candidate_id",
    "parent_candidates",
    "design_type",
    "generation_mode",
    "iteration",
    "iteration_metadata",
    "diagnosis_evidence_used",
    "mutation_rationale",
    "expected_mechanism",
    "risks_and_mitigations",
    "if_better",
    "if_worse",
    "notes",
    "audit_status",
    "experimental_status",
}


def _norm_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _material_amount(material: Dict[str, Any]) -> Any:
    for key in ("amount", "wt_percent", "concentration", "value"):
        if material.get(key) not in (None, ""):
            return material.get(key)
    return ""


def has_pva(candidate: Dict[str, Any]) -> bool:
    """Return True when candidate keeps PVA as the main polymer."""
    formulation = candidate.get("formulation") or {}
    pva_wt = formulation.get("pva_wt_percent")
    try:
        if pva_wt is not None and float(pva_wt) > 0:
            return True
    except (TypeError, ValueError):
        pass

    for material in candidate.get("materials") or []:
        if not isinstance(material, dict):
            continue
        name = _norm_name(material.get("name"))
        role = _norm_name(material.get("role"))
        if ("pva" in name or "polyvinyl alcohol" in name) and role in (
            "polymer",
            "main_polymer",
            "matrix",
            "",
        ):
            return True
    return False


def candidate_variable_map(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten formula-relevant materials and process parameters for comparison."""
    formulation = candidate.get("formulation") or {}
    processing = candidate.get("processing") or {}
    process = candidate.get("process") if isinstance(candidate.get("process"), dict) else {}

    variables: Dict[str, Any] = {}
    for key in (
        "pva_wt_percent",
        "network_type",
        "crosslink_or_phys_method",
    ):
        if formulation.get(key) not in (None, ""):
            variables[f"formulation.{key}"] = formulation.get(key)

    for block_name in ("crosslinker", "initiator_or_catalyst", "photo_initiator", "nanofiller"):
        block = formulation.get(block_name) or {}
        if isinstance(block, dict):
            for key in ("name", "wt_percent", "concentration", "unit", "basis"):
                val = block.get(key)
                if val not in (None, ""):
                    if key == "name":
                        val = canonicalize_material_name(str(val))
                    variables[f"formulation.{block_name}.{key}"] = val

    additives = formulation.get("additives") or []
    if isinstance(additives, list):
        for idx, additive in enumerate(additives):
            if not isinstance(additive, dict):
                continue
            raw_name = additive.get("name") or f"additive_{idx + 1}"
            # canonicalize so Chinese→English name drift isn't counted as a variable change
            canonical = canonicalize_material_name(raw_name)
            prefix = f"formulation.additive.{_norm_name(canonical)}"
            for key in ("name", "role", "wt_percent", "concentration", "unit", "basis"):
                val = additive.get(key)
                if val not in (None, ""):
                    if key == "name":
                        val = canonicalize_material_name(str(val))
                    variables[f"{prefix}.{key}"] = val

    # Do not treat the rendered materials list as independent design variables.
    # It is regenerated from formulation/processing by ratio_planner and can
    # change amount/unit/basis strings without changing the experimental design.
    # New material introduction is checked separately by new_material_names().

    for key in (
        "freeze_thaw_cycles",
        "freeze_temp_C",
        "thaw_temp_C",
        "cycle_hours",
        "post_soak_hours",
        "curing_temperature_C",
        "curing_time_hours",
    ):
        if processing.get(key) not in (None, ""):
            variables[f"processing.{key}"] = processing.get(key)

    # process.total_batch_mass_g and process.light_conditions are always injected
    # by ratio_planner / post-processing — they are derived outputs, not design variables.
    for key in ("post_treatment",):
        if process.get(key) not in (None, ""):
            variables[f"process.{key}"] = process.get(key)

    return variables


def _numeric_equivalent(a: Any, b: Any) -> bool:
    """Return True if a and b represent the same numeric value (e.g. 0 and 0.0)."""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def detect_changed_variables(
    candidate: Dict[str, Any],
    parent: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Compare a candidate with its parent and return structured differences."""
    if not parent:
        return []

    parent_vars = candidate_variable_map(parent)
    child_vars = candidate_variable_map(candidate)

    # Detect material relocations: when canonicalize_candidate_materials or
    # ratio_planner moves a material between formulation sections (e.g. mucin
    # from additives list → crosslinker block), those key-prefix changes are
    # post-processing normalization, NOT experimental design changes.
    relocation_keys: Set[str] = _material_relocation_keys(parent, candidate)

    changes: List[Dict[str, Any]] = []
    for variable in sorted(set(parent_vars) | set(child_vars)):
        old_value = parent_vars.get(variable, "")
        new_value = child_vars.get(variable, "")
        # Skip fields that didn't exist in the parent — they are ratio_planner
        # or post-processing injections, not experimental design changes.
        if old_value == "" and new_value != "":
            continue
        # Skip post-processing material relocations (e.g. additive → crosslinker)
        if variable in relocation_keys:
            continue
        # Numeric equivalence: 0 vs 0.0 is not a real change.
        if old_value != "" and new_value != "" and _numeric_equivalent(old_value, new_value):
            continue
        if str(old_value) != str(new_value):
            changes.append(
                {
                    "variable": variable,
                    "old_value": old_value,
                    "new_value": new_value,
                }
            )
    return changes


def _material_relocation_keys(
    parent: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Set[str]:
    """Return variable keys that represent material location changes (not design changes).

    When canonicalize_candidate_materials or ratio_planner reclassifies a material's
    role (e.g. Gastric Mucin moved from additives list to crosslinker block), the
    resulting key-prefix changes in candidate_variable_map should NOT count as
    experimental design changes.
    """
    relocation: Set[str] = set()
    parent_mats = _extract_material_names(parent)
    child_mats = _extract_material_names(candidate)
    common = parent_mats & child_mats
    if not common:
        return relocation

    parent_vars = candidate_variable_map(parent)
    child_vars = candidate_variable_map(candidate)

    for mat_name in common:
        # Collect all keys in parent and child that reference this material
        p_keys = {k for k in parent_vars if mat_name in k.lower()}
        c_keys = {k for k in child_vars if mat_name in k.lower()}
        # If the material exists in both but under different key prefixes,
        # it was relocated.  Find keys present in parent but whose prefix
        # differs in child (i.e. the parent's key doesn't exist in child).
        for pk in p_keys:
            if pk not in child_vars and any(
                ck not in parent_vars for ck in c_keys
            ):
                relocation.add(pk)
                # Also add related keys (wt_percent, role, concentration)
                base = pk.rsplit(".", 1)[0]
                for related_k in list(parent_vars):
                    if related_k.startswith(base + ".") or related_k == pk:
                        relocation.add(related_k)
        # Symmetric: child keys that don't exist in parent
        for ck in c_keys:
            if ck not in parent_vars and any(
                pk not in child_vars for pk in p_keys
            ):
                relocation.add(ck)
                base = ck.rsplit(".", 1)[0]
                for related_k in list(child_vars):
                    if related_k.startswith(base + ".") or related_k == ck:
                        relocation.add(related_k)

    return relocation


def _extract_material_names(candidate: Dict[str, Any]) -> Set[str]:
    """Extract all unique material names from a candidate's formulation sections."""
    names: Set[str] = set()
    formulation = candidate.get("formulation") or {}

    # From crosslinker / initiator blocks
    for block_name in ("crosslinker", "initiator_or_catalyst", "photo_initiator"):
        block = formulation.get(block_name) or {}
        if isinstance(block, dict):
            name = str(block.get("name") or "").strip().lower()
            if name:
                names.add(canonicalize_material_name(name))

    # From additives list
    for a in formulation.get("additives") or []:
        if isinstance(a, dict):
            name = str(a.get("name") or "").strip().lower()
            if name:
                names.add(canonicalize_material_name(name))

    # From materials list (post-ratio_planner)
    for m in candidate.get("materials") or []:
        if isinstance(m, dict):
            name = str(m.get("name") or "").strip().lower()
            if name:
                names.add(canonicalize_material_name(name))

    names.discard("")
    return names


def changed_variable_names(changed_variables: Iterable[Any]) -> List[str]:
    names: List[str] = []
    for item in changed_variables or []:
        if isinstance(item, dict):
            name = item.get("variable") or item.get("name")
        else:
            name = item
        if name not in (None, ""):
            names.append(str(name))
    return names


# Materials that are always expected in PVA hydrogel formulations but may
# not appear in the materials list of earlier rounds (pre-ratio_planner).
_BASE_MATERIALS: Set[str] = {
    "pva",
    "polyvinyl alcohol",
    "pva (polyvinyl alcohol)",
    "di water",
    "di_water",
    "deionized water",
    "water",
    "distilled water",
}


def new_material_names(candidate: Dict[str, Any], parent: Dict[str, Any] | None) -> List[str]:
    if not parent:
        return []
    parent_names = {
        _norm_name(m.get("name"))
        for m in parent.get("materials") or []
        if isinstance(m, dict) and _norm_name(m.get("name")) not in ("", "none")
    }
    child_names = {
        _norm_name(m.get("name"))
        for m in candidate.get("materials") or []
        if isinstance(m, dict) and _norm_name(m.get("name")) not in ("", "none")
    }
    # Exclude base materials (PVA, water) that are always present but may
    # have been omitted from the parent's materials list in pre-ratio_planner rounds.
    truly_new = child_names - parent_names - _BASE_MATERIALS
    return sorted(truly_new)


def classify_experimental_status(result_row: Dict[str, Any] | None = None) -> str:
    """Classify wet-lab outcome without using audit status."""
    if not result_row:
        return "not_measured"
    cof = result_row.get("cof_steady_mean")
    try:
        if cof not in (None, "") and float(cof) > 0.0001:
            return "measured"
    except (TypeError, ValueError):
        pass
    failure = _norm_name(result_row.get("failure_type"))
    if failure and failure not in ("none", "na", "n/a"):
        return "experimental_failed"
    return "measured_without_cof" if cof not in (None, "") else "not_measured"


def validate_candidate_constraints(
    candidate: Dict[str, Any],
    parent_by_id: Dict[str, Dict[str, Any]] | None = None,
    round_limited_exploration_count: int = 0,
    require_parent: bool = True,
) -> List[str]:
    """Return hard-rule failures for one candidate."""
    errors: List[str] = []
    parent_by_id = parent_by_id or {}
    cid = candidate.get("candidate_id", "?")
    design_type = candidate.get("design_type", "")
    parent_id = candidate.get("parent_candidate_id") or ""
    parent = parent_by_id.get(parent_id)
    changed = candidate.get("changed_variables") or []
    changed_names = changed_variable_names(changed)

    if require_parent and not parent_id:
        errors.append(f"{cid}: missing parent_candidate_id")
    if require_parent and parent_id and parent_id not in parent_by_id:
        errors.append(f"{cid}: parent_candidate_id '{parent_id}' is not a completed previous-round candidate")
    if not has_pva(candidate):
        errors.append(f"{cid}: missing PVA main polymer")

    if design_type == "baseline_reproduction":
        # Filter out relocations before judging baseline purity
        real_changes = [n for n in changed_names if not _is_relocated_name(n, candidate, parent)]
        if real_changes:
            errors.append(f"{cid}: baseline_reproduction changed variables: {real_changes}")
    elif design_type == "local_optimization":
        if len(changed_names) > 2:
            errors.append(f"{cid}: local_optimization changed {len(changed_names)} variables, max 2")
        if parent and new_material_names(candidate, parent):
            errors.append(f"{cid}: local_optimization introduced new materials outside limited_exploration")
    elif design_type == "single_factor_perturbation":
        # Allow coupled variable pairs (e.g. AAm + DMSO in UV-photo systems)
        effective_count = _effective_change_count(changed_names)
        if effective_count > 1:
            errors.append(f"{cid}: single_factor_perturbation must change exactly 1 variable, got {len(changed_names)} (effective={effective_count})")
        if parent and new_material_names(candidate, parent):
            errors.append(f"{cid}: single_factor_perturbation introduced new materials outside limited_exploration")
    elif design_type in ("failure_verification", "failure_factor_verification", "failure_rescue_verification"):
        allowed_coupled_rescue = (
            design_type == "failure_rescue_verification"
            and len(changed_names) == 2
            and {
                n.rsplit(".", 1)[-1] if "." in n else n
                for n in changed_names
            }
            == {"pva_wt_percent", "post_soak_hours"}
        )
        if len(changed_names) > 1 and not allowed_coupled_rescue:
            errors.append(f"{cid}: {design_type} changed {len(changed_names)} variables, max 1")
        if parent and new_material_names(candidate, parent):
            errors.append(f"{cid}: {design_type} introduced new materials outside limited_exploration")
    elif design_type == "limited_exploration":
        if round_limited_exploration_count > 1:
            errors.append("limited_exploration count exceeds 1 for this round")
        if parent and len(new_material_names(candidate, parent)) > 1:
            errors.append(f"{cid}: limited_exploration introduced more than 1 new material")

    for field in ("if_better", "if_worse"):
        if not str(candidate.get(field) or "").strip():
            errors.append(f"{cid}: missing {field}")

    if (
        candidate.get("experimental_status") == "experimental_failed"
        and candidate.get("audit_status") == "FAIL"
        and candidate.get("has_measurable_cof")
    ):
        errors.append(f"{cid}: audit failure was conflated with experimental failure despite measurable COF")

    return errors


def _effective_change_count(changed_names: List[str]) -> int:
    """Count effective independent changes, treating coupled pairs as 1 change.

    Example: [acrylamide.wt_percent, dmso.wt_percent] → 1 (coupled pair)
             [post_soak_hours, acrylamide.wt_percent, dmso.wt_percent] → 2
    """
    if len(changed_names) <= 1:
        return len(changed_names)

    # Build a map from each variable name to all names in the list
    name_set = set(changed_names)

    # Find coupled groups: for each pair (a,b), if both a and b are in name_set,
    # they form a group.  Use exact match first, then suffix-of-last-segment match.
    coupled_groups: List[Set[str]] = []
    remaining = set(changed_names)

    for a, b in COUPLED_VARIABLE_PAIRS:
        # Find the actual name(s) in remaining that match a or b
        a_matches = {n for n in remaining if n == a or _coupled_match(n, a)}
        b_matches = {n for n in remaining if n == b or _coupled_match(n, b)}
        if a_matches and b_matches:
            group = a_matches | b_matches
            coupled_groups.append(group)
            remaining -= group

    # Each coupled group counts as 1 change
    return len(coupled_groups) + len(remaining)


def _coupled_match(name: str, pair_entry: str) -> bool:
    """Check if *name* matches a coupled-pair entry by last two segments.

    Example: _coupled_match('formulation.additive.acrylamide.wt_percent',
                            'formulation.additive.acrylamide.wt_percent') → True
             _coupled_match('additive.acrylamide.wt_percent',
                            'formulation.additive.acrylamide.wt_percent') → True
    """
    # Exact match
    if name == pair_entry:
        return True
    # Match by last 2 segments: "acrylamide.wt_percent"
    name_parts = name.rsplit(".", 2)
    pair_parts = pair_entry.rsplit(".", 2)
    return len(name_parts) >= 2 and len(pair_parts) >= 2 and name_parts[-2:] == pair_parts[-2:]


def _is_relocated_name(name: str, candidate: Dict[str, Any], parent: Dict[str, Any] | None) -> bool:
    """Check if a changed_variable name is a post-processing relocation."""
    if not parent:
        return False
    relocation_keys = _material_relocation_keys(parent, candidate)
    return name in relocation_keys


def check_crosslink_additive_consistency(
    candidate: Dict[str, Any],
) -> List[str]:
    """Warn when crosslink method references GA/HCl but glutaraldehyde is missing.

    Returns a list of warning strings (empty = all consistent).
    """
    warnings: List[str] = []
    formulation = candidate.get("formulation") or {}
    cid = candidate.get("candidate_id", "?")
    method = str(formulation.get("crosslink_or_phys_method") or "").strip().lower()

    if not method or method == "none":
        return warnings

    # Find the most specific matching key (longest first to avoid double-matching
    # e.g. 'ga_hcl_fast' matches both 'ga' and 'ga_hcl_fast')
    matched_key = ""
    for method_key in sorted(CROSSLINK_REQUIRED_ADDITIVES, key=lambda k: -len(k)):
        if method_key in method:
            matched_key = method_key
            break

    if not matched_key:
        return warnings

    required = CROSSLINK_REQUIRED_ADDITIVES.get(matched_key, [])
    if not required:
        return warnings  # uv_photo has no required additives

    # Gather all material names in the candidate
    all_names: Set[str] = set()
    for a in formulation.get("additives") or []:
        if isinstance(a, dict):
            all_names.add(str(a.get("name") or "").strip().lower())
    crosslinker = formulation.get("crosslinker") or {}
    if isinstance(crosslinker, dict):
        all_names.add(str(crosslinker.get("name") or "").strip().lower())
    for m in candidate.get("materials") or []:
        if isinstance(m, dict):
            all_names.add(str(m.get("name") or "").strip().lower())

    missing = [r for r in required if not any(r in name for name in all_names)]
    if missing:
        warnings.append(
            f"{cid}: crosslink_or_phys_method='{method}' expects {missing} "
            f"but they are not present in additives, crosslinker, or materials"
        )

    return warnings


def black_box_jump_score(candidate: Dict[str, Any], parent: Dict[str, Any] | None) -> int:
    """Small deterministic risk score; >=4 should be rejected."""
    score = 0
    if not candidate.get("parent_candidate_id"):
        score += 2
    if not has_pva(candidate):
        score += 4
    changed_count = len(changed_variable_names(candidate.get("changed_variables") or []))
    if changed_count > 2:
        score += changed_count - 2
    new_mats = new_material_names(candidate, parent)
    if new_mats and candidate.get("design_type") != "limited_exploration":
        score += 2 + len(new_mats)
    if candidate.get("design_type") == "baseline_reproduction" and changed_count:
        score += 3
    if (
        candidate.get("experimental_status") == "experimental_failed"
        and candidate.get("audit_status") == "FAIL"
        and candidate.get("has_measurable_cof")
    ):
        score += 2
    return score


def build_inheritance_table(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    table: List[Dict[str, Any]] = []
    for c in candidates:
        table.append(
            {
                "candidate_id": c.get("candidate_id"),
                "tree_id": c.get("tree_id", ""),
                "node_id": c.get("node_id", c.get("candidate_id")),
                "parent_node_id": c.get("parent_node_id", c.get("parent_candidate_id")),
                "branch_status": c.get("branch_status", ""),
                "parent_candidate_id": c.get("parent_candidate_id"),
                "design_type": c.get("design_type"),
                "changed_variables": changed_variable_names(c.get("changed_variables") or []),
                "if_better": c.get("if_better", ""),
                "if_worse": c.get("if_worse", ""),
                "black_box_jump_score": c.get("black_box_jump_score", 0),
                "audit_status": c.get("audit_status", ""),
                "experimental_status": c.get("experimental_status", ""),
            }
        )
    return table


def inheritance_table_markdown(candidates: List[Dict[str, Any]]) -> str:
    headers = [
        "candidate_id",
        "tree_id",
        "branch_status",
        "parent_candidate_id",
        "design_type",
        "changed_variables",
        "if_better",
        "if_worse",
        "black_box_jump_score",
    ]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in build_inheritance_table(candidates):
        values = []
        for header in headers:
            value = row.get(header, "")
            if isinstance(value, list):
                value = ", ".join(value)
            values.append(str(value).replace("\n", " ").replace("|", "/"))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"
