from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, List, Tuple


TOTAL_BATCH_MASS_G = 20.0


ROLE_RANGES: Dict[str, Tuple[float, float]] = {
    "pva_wt_percent": (5.0, 20.0),
    "crosslinker_wt_percent": (0.05, 1.0),
    "initiator_wt_percent": (0.02, 0.3),
    "photo_initiator_wt_percent": (0.03, 0.3),
    "nanofiller_wt_percent": (0.05, 1.0),
    "plasticizer_wt_percent": (0.5, 5.0),
    "lubricant_wt_percent": (0.1, 3.0),
    "secondary_polymer_wt_percent": (0.5, 6.0),
    "generic_additive_wt_percent": (0.1, 2.0),
}


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() in {"na", "nan", "none"}:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round_wt(v: float) -> float:
    return round(float(v), 3)


def _mass_from_wt(wt_percent: float, total_mass_g: float = TOTAL_BATCH_MASS_G) -> float:
    return round(total_mass_g * wt_percent / 100.0, 4)


def _stable_unit_interval(seed_text: str) -> float:
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    intval = int(digest[:12], 16)
    return (intval % 10000) / 10000.0


def _lhs_value(lo: float, hi: float, idx: int, n: int, seed_text: str) -> float:
    if n <= 1:
        return (lo + hi) / 2.0
    bin_width = 1.0 / n
    jitter = _stable_unit_interval(seed_text)
    frac = (idx % n) * bin_width + jitter * bin_width
    return lo + (hi - lo) * frac


def _text_blob(c: Dict[str, Any]) -> str:
    pieces: List[str] = []
    for key in ("mutation_rationale", "extension_reason", "notes"):
        pieces.append(str(c.get(key) or ""))
    for key in ("expected_mechanism", "diagnosis_evidence_used", "diagnosis_levers_used"):
        val = c.get(key) or []
        if isinstance(val, list):
            pieces.extend(str(x) for x in val)
        else:
            pieces.append(str(val))
    pieces.append(str(c.get("formulation") or {}))
    pieces.append(str(c.get("materials") or []))
    return " ".join(pieces).lower()


def _direction_flags(c: Dict[str, Any]) -> Dict[str, bool]:
    blob = _text_blob(c)
    return {
        "lower_friction": any(k in blob for k in ("lower cof", "reduce cof", "low friction", "lubric", "stick-slip", "stick slip")),
        "increase_modulus": any(k in blob for k in ("increase modulus", "too soft", "softening", "swelling", "reinforc")),
        "reduce_brittleness": any(k in blob for k in ("too brittle", "fracture", "delamination", "plasticiz", "flexib")),
        "systematic_cover": any(k in blob for k in ("systematically_cover", "doe", "cover", "grid")),
    }


def _factor_level(c: Dict[str, Any], *names: str) -> str:
    levels = c.get("doe_factor_levels") or c.get("doe_factor_levels_used") or {}
    if not isinstance(levels, dict):
        return ""
    wanted = {n.lower() for n in names}
    for key, val in levels.items():
        if str(key).strip().lower() in wanted:
            return str(val).strip()
    return ""


def _planned_pva_wt(c: Dict[str, Any], idx: int, n: int) -> Tuple[float, List[str]]:
    reasons: List[str] = []
    lo, hi = ROLE_RANGES["pva_wt_percent"]

    numeric_level = _to_float(_factor_level(c, "pva_wt_percent", "polymer_wt_percent"))
    if numeric_level is not None:
        wt = _clamp(numeric_level, lo, hi)
        reasons.append(f"PVA wt% follows DOE level {wt:g}.")
        return _round_wt(wt), reasons

    qualitative = _factor_level(c, "polymer_wt_percent", "pva_wt_percent").lower()
    if qualitative in {"lower", "low"}:
        lo, hi = 8.0, 12.0
        reasons.append("PVA search window shifted lower by DOE level.")
    elif qualitative in {"higher", "high"}:
        lo, hi = 14.0, 18.0
        reasons.append("PVA search window shifted higher by DOE level.")

    flags = _direction_flags(c)
    if flags["lower_friction"] and not flags["increase_modulus"]:
        hi = min(hi, 15.0)
        reasons.append("Upper PVA bound reduced to limit contact stiffness and friction.")
    if flags["increase_modulus"]:
        lo = max(lo, 12.0)
        reasons.append("Lower PVA bound increased to improve swelling resistance and modulus.")
    if flags["reduce_brittleness"]:
        hi = min(hi, 16.0)
        reasons.append("Very high PVA is avoided because brittleness/fracture risk was mentioned.")

    current = _to_float((c.get("formulation") or {}).get("pva_wt_percent"))
    if current is not None and lo <= current <= hi:
        reasons.append("Existing LLM PVA value is inside the planned role range and is retained.")
        return _round_wt(current), reasons

    wt = _lhs_value(lo, hi, idx, n, f"{c.get('candidate_id','')}:pva:{idx}")
    reasons.append(f"PVA wt% selected by deterministic LHS within {lo:g}-{hi:g} wt%.")
    return _round_wt(wt), reasons


def _role_for_additive(additive: Dict[str, Any]) -> str:
    role = str(additive.get("role") or "").lower()
    name = str(additive.get("name") or "").lower()
    text = f"{role} {name}"
    if any(k in text for k in ("plasticizer", "glycerol", "peg")):
        return "plasticizer_wt_percent"
    if any(k in text for k in ("lubric", "hyaluronic", "ha", "pam", "pvp", "mucin")):
        return "lubricant_wt_percent"
    if any(k in text for k in ("polymer", "copolymer", "secondary")):
        return "secondary_polymer_wt_percent"
    return "generic_additive_wt_percent"


# DOE crosslinker_concentration qualitative → numeric range mapping
_CROSSLINKER_DOE_MAP: dict[str, tuple[float, float]] = {
    "low": (0.05, 0.3),
    "medium": (0.3, 0.6),
    "high": (0.6, 1.0),
}


def _planned_component_wt(
    c: Dict[str, Any],
    role_key: str,
    idx: int,
    n: int,
    existing: float | None,
    name: str,
) -> Tuple[float, List[str]]:
    lo, hi = ROLE_RANGES[role_key]
    reasons: List[str] = []
    flags = _direction_flags(c)

    # ---- Read crosslinker concentration from DOE factor levels ----
    if role_key == "crosslinker_wt_percent":
        doe_cl = _factor_level(c, "crosslinker_concentration", "cross_linker_concentration")
        if doe_cl:
            doe_cl_norm = doe_cl.strip().lower().replace(" ", "_").replace("wt%", "").replace("%", "").strip()
            # Try parsing as numeric
            num = _to_float(doe_cl_norm)
            if num is not None:
                lo = _clamp(num * 0.5, ROLE_RANGES[role_key][0], ROLE_RANGES[role_key][1])
                hi = _clamp(num * 1.5, ROLE_RANGES[role_key][0], ROLE_RANGES[role_key][1])
                reasons.append(f"{name} range centered on DOE crosslinker target {num:g} wt%.")
            elif doe_cl_norm in _CROSSLINKER_DOE_MAP:
                lo, hi = _CROSSLINKER_DOE_MAP[doe_cl_norm]
                reasons.append(f"{name} range set to DOE level '{doe_cl}' ({lo:g}-{hi:g} wt%).")

    if role_key == "plasticizer_wt_percent" and flags["reduce_brittleness"]:
        lo = max(lo, 2.0)
        reasons.append(f"{name} plasticizer lower bound increased to reduce brittleness.")
    if role_key == "lubricant_wt_percent" and flags["lower_friction"]:
        lo = max(lo, 0.5)
        hi = min(max(hi, 2.0), 3.0)
        reasons.append(f"{name} lubricant level biased upward for low-friction screening.")
    if role_key in {"crosslinker_wt_percent", "nanofiller_wt_percent"} and flags["reduce_brittleness"]:
        hi = min(hi, 0.6)
        reasons.append(f"{name} upper bound reduced to avoid brittle/high-friction networks.")
    if role_key in {"crosslinker_wt_percent", "nanofiller_wt_percent"} and flags["increase_modulus"]:
        lo = max(lo, 0.2)
        reasons.append(f"{name} lower bound raised because modulus/swelling resistance is a target.")

    if existing is not None and lo <= existing <= hi:
        reasons.append(f"{name} existing wt% retained because it is inside {lo:g}-{hi:g} wt%.")
        return _round_wt(existing), reasons

    wt = _lhs_value(lo, hi, idx, n, f"{c.get('candidate_id','') or name}:{role_key}:{idx}")
    reasons.append(f"{name} wt% selected by deterministic LHS within {lo:g}-{hi:g} wt%.")
    return _round_wt(wt), reasons


def _set_material_amount(
    materials: List[Dict[str, Any]],
    name: str,
    role: str,
    wt_percent: float,
    total_mass_g: float,
) -> None:
    if not name:
        return
    name_l = name.strip().lower()
    target = None
    for m in materials:
        if str(m.get("name") or "").strip().lower() == name_l:
            target = m
            break
    if target is None:
        target = {"name": name, "role": role}
        materials.append(target)
    target.setdefault("role", role)
    target["amount"] = _mass_from_wt(wt_percent, total_mass_g)
    target["unit"] = "g"
    target["basis"] = f"{wt_percent:g} wt% of {total_mass_g:g} g batch"


def _add_water_balance(materials: List[Dict[str, Any]], used_wt: float, total_mass_g: float) -> None:
    water_wt = _round_wt(_clamp(100.0 - used_wt, 0.0, 100.0))
    for m in materials:
        name = str(m.get("name") or "").strip().lower()
        role = str(m.get("role") or "").strip().lower()
        if name in {"di water", "water", "deionized water"} or role == "solvent":
            m["name"] = m.get("name") or "DI water"
            m["role"] = m.get("role") or "solvent"
            m["amount"] = _mass_from_wt(water_wt, total_mass_g)
            m["unit"] = "g"
            m["basis"] = f"balance to {total_mass_g:g} g batch"
            return
    materials.append({
        "name": "DI water",
        "role": "solvent",
        "amount": _mass_from_wt(water_wt, total_mass_g),
        "unit": "g",
        "basis": f"balance to {total_mass_g:g} g batch",
    })


def _ensure_minimal_process_steps(process: Dict[str, Any], processing: Dict[str, Any]) -> None:
    if process.get("steps"):
        return
    process.setdefault("light_conditions", "not applicable")
    process["steps"] = [
        {
            "order": 1,
            "name": "Dissolve PVA in DI water at 90C until clear",
            "temperature_C": 90,
            "duration_hours": 2.0,
            "needs_degas": False,
            "needs_pre_soak": False,
        },
        {
            "order": 2,
            "name": "Add planned additives or crosslinking components and mix",
            "temperature_C": 25,
            "duration_hours": 0.5,
            "needs_degas": False,
            "needs_pre_soak": False,
        },
        {
            "order": 3,
            "name": "Cast into mold and degas",
            "temperature_C": 25,
            "duration_hours": 0.5,
            "needs_degas": True,
            "needs_pre_soak": False,
        },
        {
            "order": 4,
            "name": "Apply freeze-thaw schedule defined in processing",
            "temperature_C": processing.get("freeze_temp_C", -20),
            "duration_hours": 0.0,
            "needs_degas": False,
            "needs_pre_soak": False,
        },
        {
            "order": 5,
            "name": "Post-soak in DI water as defined in processing",
            "temperature_C": 25,
            "duration_hours": 0.0,
            "needs_degas": False,
            "needs_pre_soak": False,
        },
    ]


def _small_sample_design_policy(candidate_index: int, total_candidates: int) -> Dict[str, Any]:
    """Assign a conservative role for small-data optimization batches.

    Repeats estimate measurement noise; they do not create independent design
    points. The planner therefore keeps most slots exploratory and reserves a
    small number for exploitation/control when the batch is large enough.
    """
    n = max(1, int(total_candidates or 1))
    idx = max(0, int(candidate_index or 0))

    if n >= 8:
        if idx >= n - 1:
            role = "control_or_best_repeat"
            repeats = 3
        elif idx >= n - 3:
            role = "exploitation"
            repeats = 3
        else:
            role = "exploration"
            repeats = 2
    elif n >= 4:
        if idx == n - 1:
            role = "control_or_best_repeat"
            repeats = 3
        elif idx == n - 2:
            role = "exploitation"
            repeats = 3
        else:
            role = "exploration"
            repeats = 2
    else:
        role = "exploration"
        repeats = 3

    return {
        "design_role": role,
        "recommended_repeats": repeats,
        "unique_design_point": True,
        "bo_readiness_note": (
            "Treat this as one design point. Repeats estimate noise; start Bayesian "
            "optimization only after roughly 16-24 unique low-dimensional design points."
        ),
    }


def apply_ratio_plan(
    c: Dict[str, Any],
    candidate_index: int,
    total_candidates: int,
    total_batch_mass_g: float = TOTAL_BATCH_MASS_G,
) -> Dict[str, Any]:
    """Attach explainable composition ratios to one generated candidate.

    The planner is intentionally lightweight: it does not claim a physical
    simulation. It constrains role-specific ranges, honors DOE levels when
    present, then uses deterministic LHS-style sampling to avoid arbitrary
    one-off numbers.
    """
    formulation = c.get("formulation") or {}
    if not isinstance(formulation, dict):
        formulation = {}
    process = c.get("process") or {}
    if not isinstance(process, dict):
        process = {}
    processing = c.get("processing") or {}
    if not isinstance(processing, dict):
        processing = {}
    materials = c.get("materials") or []
    if not isinstance(materials, list):
        materials = []

    total_batch = _to_float(process.get("total_batch_mass_g")) or total_batch_mass_g
    process["total_batch_mass_g"] = total_batch
    _ensure_minimal_process_steps(process, processing)

    rationale: List[str] = []
    ratio_space: Dict[str, Any] = {}
    preserve_design_values = str(c.get("design_type") or "").strip().lower() == "baseline_reproduction"
    # Code-materialized candidates already have correct wt% values from the
    # parent deepcopy + set_planned_variable.  Only compute material amounts
    # and water balance — do NOT re-sample additive/crosslinker wt%.
    #
    # Detection: 'planned_changed_variables' exists (even if empty list for
    # baseline) on materialized candidates; 'skeleton_source' in the DOE
    # plan is copied into iteration_metadata.
    code_materialized = (
        (c.get("iteration_metadata") or {}).get("skeleton_source") == "code_constrained_doe"
        or "planned_changed_variables" in c
    )
    if code_materialized:
        preserve_design_values = True

    current_pva = _to_float(formulation.get("pva_wt_percent"))
    if preserve_design_values and current_pva is not None:
        pva_wt = _round_wt(current_pva)
        pva_reasons = ["Baseline reproduction preserves parent PVA wt% exactly."]
    else:
        pva_wt, pva_reasons = _planned_pva_wt(c, candidate_index, total_candidates)
    rationale.extend(pva_reasons)
    formulation["pva_wt_percent"] = pva_wt
    ratio_space["pva_wt_percent"] = list(ROLE_RANGES["pva_wt_percent"])
    used_wt = pva_wt
    _set_material_amount(materials, "PVA", "main_polymer", pva_wt, total_batch)

    additives = formulation.get("additives") or []
    if not isinstance(additives, list):
        additives = []

    def _matching_additive_wt(name: str) -> float | None:
        name_l = str(name or "").strip().lower()
        if not name_l:
            return None
        for additive in additives:
            if not isinstance(additive, dict):
                continue
            if str(additive.get("name") or "").strip().lower() != name_l:
                continue
            return _to_float(additive.get("wt_percent"))
        return None

    component_specs: List[Tuple[str, str, str, float | None]] = []
    for field, role_key, role in (
        ("crosslinker", "crosslinker_wt_percent", "crosslinker"),
        ("initiator_or_catalyst", "initiator_wt_percent", "catalyst"),
        ("photo_initiator", "photo_initiator_wt_percent", "photo_initiator"),
        ("nanofiller", "nanofiller_wt_percent", "nanofiller"),
    ):
        obj = formulation.get(field) or {}
        if isinstance(obj, dict) and obj.get("name"):
            if field == "crosslinker" and str(obj.get("name") or "").strip().lower() in {"gastric mucin", "mucin"}:
                additives = formulation.setdefault("additives", [])
                if isinstance(additives, list) and not any(
                    isinstance(a, dict)
                    and str(a.get("name") or "").strip().lower() in {"gastric mucin", "mucin"}
                    for a in additives
                ):
                    additives.append(
                        {
                            "name": "Gastric Mucin",
                            "role": "lubricant",
                            "wt_percent": obj.get("wt_percent"),
                        }
                    )
                formulation[field] = {}
                continue
            additive_wt = _matching_additive_wt(str(obj.get("name") or ""))
            if code_materialized and additive_wt is not None:
                obj["wt_percent"] = _round_wt(additive_wt)
                formulation[field] = obj
                rationale.append(
                    f"Code-constrained candidate preserves parent {obj.get('name')} wt% from additives."
                )
                continue
            component_specs.append((obj["name"], role_key, role, _to_float(obj.get("wt_percent"))))

    additives = formulation.get("additives") or []
    if not isinstance(additives, list):
        additives = []

    # ---- Bridge doe_factor_levels → formulation.additives ----
    # LLM may declare an additive in doe_factor_levels but forget to put
    # it in formulation.additives.  Fill the gap here so the planner can
    # compute actual wt% / gram amounts.
    if not additives:
        add_decl = _factor_level(c, "additive_combination", "additive_type", "additive")
        if add_decl and add_decl.lower() not in {"", "none"}:
            # Map declared additive names to concrete material entries.
            _ADDITIVE_LOOKUP: Dict[str, List[Dict[str, str]]] = {
                "sodium hyaluronate": [{"name": "Sodium Hyaluronate", "role": "lubricant"}],
                "ha": [{"name": "Sodium Hyaluronate", "role": "lubricant"}],
                "hyaluronate": [{"name": "Sodium Hyaluronate", "role": "lubricant"}],
                "cmc": [{"name": "Carboxymethyl cellulose (CMC)", "role": "additive"}],
                "carboxymethyl cellulose": [{"name": "Carboxymethyl cellulose (CMC)", "role": "additive"}],
                "dmso": [{"name": "DMSO", "role": "plasticizer"}],
                "gastric mucin": [{"name": "Gastric Mucin", "role": "lubricant"}],
                "mucin": [{"name": "Gastric Mucin", "role": "lubricant"}],
                "peg 400": [{"name": "PEG 400", "role": "plasticizer"}],
                "peg": [{"name": "PEG 400", "role": "plasticizer"}],
            }
            _SPLIT_MARKER = "hybrid"
            add_lower = add_decl.strip().lower()
            if _SPLIT_MARKER in add_lower:
                # e.g. "Hybrid (HA/CMC)" → try to split into known components
                # Extract parenthesised list: "ha/cmc" → ["ha", "cmc"]
                _m = re.search(r"\(([^)]+)\)", add_decl)
                parts = [p.strip() for p in (_m.group(1).split("/") if _m else []) if p.strip()]
                if not parts:
                    parts = [add_decl]
                for part in parts:
                    entries = _ADDITIVE_LOOKUP.get(part.lower(), [{"name": part, "role": "additive"}])
                    additives.extend(entries)
            else:
                entries = _ADDITIVE_LOOKUP.get(add_lower, [{"name": add_decl.strip(), "role": "additive"}])
                additives.extend(entries)

    normalized_additives = []
    for additive in additives:
        if not isinstance(additive, dict):
            additive = {"name": str(additive), "role": "additive"}
        if not additive.get("name"):
            continue
        role_key = _role_for_additive(additive)
        role = str(additive.get("role") or "additive")
        wt_existing = _to_float(additive.get("wt_percent"))
        if preserve_design_values and wt_existing is not None:
            wt = _round_wt(wt_existing)
            reasons = [f"Baseline reproduction preserves parent {additive.get('name')} wt% exactly."]
        else:
            wt, reasons = _planned_component_wt(
                c,
                role_key,
                candidate_index,
                total_candidates,
                wt_existing,
                str(additive.get("name")),
            )
        additive["wt_percent"] = wt
        additive["wt_percent_range"] = f"{ROLE_RANGES[role_key][0]:g}-{ROLE_RANGES[role_key][1]:g}"
        used_wt += wt
        rationale.extend(reasons)
        ratio_space[str(additive.get("name"))] = list(ROLE_RANGES[role_key])
        _set_material_amount(materials, str(additive.get("name")), role, wt, total_batch)
        normalized_additives.append(additive)
    formulation["additives"] = normalized_additives

    for name, role_key, role, existing in component_specs:
        if preserve_design_values and existing is not None:
            wt = _round_wt(existing)
            reasons = [f"Baseline reproduction preserves parent {name} wt% exactly."]
        else:
            wt, reasons = _planned_component_wt(c, role_key, candidate_index, total_candidates, existing, name)
        used_wt += wt
        rationale.extend(reasons)
        ratio_space[name] = list(ROLE_RANGES[role_key])
        _set_material_amount(materials, name, role, wt, total_batch)
        for field in ("crosslinker", "initiator_or_catalyst", "photo_initiator", "nanofiller"):
            obj = formulation.get(field) or {}
            if isinstance(obj, dict) and str(obj.get("name") or "").strip().lower() == name.strip().lower():
                obj["wt_percent"] = wt
                formulation[field] = obj

    if used_wt > 35.0:
        rationale.append(
            f"Total non-solvent loading is {used_wt:g} wt%; review viscosity and casting practicality before wet-lab execution."
        )

    _add_water_balance(materials, used_wt, total_batch)
    small_data_policy = _small_sample_design_policy(candidate_index, total_candidates)

    c["formulation"] = formulation
    c["process"] = process
    c["processing"] = processing
    c["materials"] = materials
    c["design_role"] = small_data_policy["design_role"]
    c["recommended_repeats"] = small_data_policy["recommended_repeats"]
    c["optimization_phase"] = "doe_lhs_until_enough_unique_points"
    c["ratio_planner"] = {
        "method": "role_range_rules_plus_deterministic_lhs",
        "total_batch_mass_g": total_batch,
        "ratio_space": ratio_space,
        "small_data_policy": small_data_policy,
        "active_numeric_lever_limit": 4,
        "bayesian_optimization_gate": {
            "min_unique_design_points": 16,
            "preferred_unique_design_points": 24,
            "max_active_numeric_levers": 4,
            "use_repeats_as_noise_estimates": True,
        },
        "rationale": list(dict.fromkeys(rationale)),
        "assumptions": [
            "Role-specific ranges are heuristic screening ranges, not validated physical predictions.",
            "DOE numeric levels are treated as hard anchors when present.",
            "Water is balanced to the fixed batch mass after non-solvent components are assigned.",
            "Repeats are not counted as independent formulation samples for Bayesian optimization.",
        ],
    }
    c["ratio_source"] = "ratio_planner"
    c["ratio_rationale"] = c["ratio_planner"]["rationale"]
    return c
