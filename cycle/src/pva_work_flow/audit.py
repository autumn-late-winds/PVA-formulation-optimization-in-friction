from pathlib import Path
from typing import Any, Dict, List, Tuple
import json
import re
from .utils import read_json, write_json
from .artifact_store import RunWorkspace
from .config import GenerationMode
from .candidate_rules import (
    black_box_jump_score,
    build_inheritance_table,
    detect_changed_variables,
    inheritance_table_markdown,
    validate_candidate_constraints,
)
from .workflow import (
    compute_prep_time_hours,
    check_material_completeness,
    _text_blob_for_mechanism,
    normalize_materials_and_formulation,
    _candidate_material_names,
)

REQUIRED_RISKS = 2


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(value))
        return float(match.group(0)) if match else default


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, float(default))))


def _rag_evidence_summary(c: dict) -> tuple[bool, list[str], list[str]]:
    """Return (has_support, evidence_labels, warnings) for candidate-level RAG evidence."""
    raw_items: list[Any] = []
    for key in ("rag_evidence", "rag_evidence_used", "formulation_rag_cases"):
        value = c.get(key)
        if isinstance(value, list):
            raw_items.extend(value)
        elif isinstance(value, dict):
            raw_items.append(value)
        elif str(value or "").strip():
            raw_items.append(str(value).strip())

    labels: list[str] = []
    warnings: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            label = (
                item.get("case_id")
                or item.get("source_title")
                or item.get("claim")
                or item.get("changed_factor")
                or item.get("locator")
            )
            labels.append(str(label or "rag_case"))
            has_source = any(item.get(k) for k in ("case_id", "source_title", "source_locator", "locator"))
            has_claim = any(item.get(k) for k in ("claim", "optimization_use", "mechanism", "mechanism_or_failure_reason"))
            if not has_source or not has_claim:
                warnings.append("rag_evidence_incomplete: evidence item needs source id/locator and claim/mechanism")
        else:
            labels.append(str(item)[:120])
            warnings.append("rag_evidence_unstructured: prefer dict with source and claim fields")

    labels = [x for x in dict.fromkeys(labels) if x]
    return bool(labels), labels, list(dict.fromkeys(warnings))

# Baseline materials that are always allowed and don't count as "new" for DOE compliance
BASELINE_MATERIALS: set[str] = {
    "pva",
    "polyvinyl alcohol",
    "di water",
    "deionized water",
    "water",
    "none",
}

# ============================================================
# Chemical feasibility rules — hard domain knowledge
# Each rule: (condition_check, failure_message)
# ============================================================
def _check_chemical_feasibility(c: dict) -> List[str]:
    """Validate chemical feasibility of a candidate formulation.

    Returns list of failure messages. Empty list = no chemical issues found.
    """
    failures: List[str] = []
    f = c.get("formulation", {}) or {}
    # Merge process + processing: process.steps (from ratio_planner) has temperature_C;
    # processing.steps (from LLM) may only have names. Prefer process for fields, processing as fallback.
    _proc = c.get("process", {}) or {}
    _processing = c.get("processing", {}) or {}
    p: Dict[str, Any] = {**_processing, **_proc}
    if _proc.get("steps") and _processing.get("steps"):
        # Merge: process.steps (ratio_planner, has temperature_C) takes priority
        merged_by_order = {s.get("order"): s for s in (_processing.get("steps") or []) if isinstance(s, dict) and s.get("order") is not None}
        for s in (_proc.get("steps") or []):
            if isinstance(s, dict) and s.get("order") is not None:
                merged_by_order[s["order"]] = {**merged_by_order.get(s["order"], {}), **s}
        p["steps"] = sorted(merged_by_order.values(), key=lambda x: x.get("order", 999))
    elif _proc.get("steps"):
        p["steps"] = _proc["steps"]
    materials = c.get("materials", []) or []
    text_blob = _text_blob_for_mechanism(c)

    # --- Gather material names and roles ---
    mat_roles: Dict[str, str] = {}
    mat_names_lower: set[str] = set()
    for m in materials:
        name = (m.get("name") or "").strip().lower()
        role = (m.get("role") or "").strip().lower()
        if name:
            mat_roles[name] = role
            mat_names_lower.add(name)

    def _has_material(keyword: str) -> bool:
        return any(keyword in n for n in mat_names_lower)

    def _has_role(keyword: str) -> bool:
        return any(keyword in r for r in mat_roles.values())

    # ---- Rule 1: PVA dissolution temperature ----
    has_pva = _has_material("pva")
    for step in p.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        name = (step.get("name") or "").lower()
        temp = _safe_float(step.get("temperature_C"), 0.0)
        if ("dissolve" in name or "溶解" in name) and (has_pva or "pva" in name):
            if temp < 80:
                failures.append(
                    f"PVA dissolution at {temp}°C is too low; requires 80-95°C."
                )

    # ---- Rule 2: Glutaraldehyde requires acidic catalyst (HCl) ----
    if "glutaraldehyde" in text_blob or "glut" in text_blob:
        has_acid = _has_material("hcl") or _has_material("acid") or "acid" in text_blob
        if not has_acid:
            failures.append(
                "Glutaraldehyde crosslinking requires acidic catalyst (HCl). "
                "No acid or HCl found in materials or mechanisms."
            )

    # ---- Rule 3: Borax crosslinking is pH-sensitive ----
    if "borax" in text_blob or "borate" in text_blob or "硼砂" in text_blob:
        post_soak = _safe_float(p.get("post_soak_hours"), 0.0)
        if post_soak > 4:
            failures.append(
                f"Borax crosslinking is pH-reversible. "
                f"Post-soak of {post_soak}h in DI water will de-crosslink the gel."
            )

    # ---- Rule 4: UV photocuring vs opaque nanofillers ----
    if "photo" in text_blob or "uv" in text_blob or "光固" in text_blob:
        for name in mat_names_lower:
            if any(k in name for k in ("clay", "graphene", "carbon", "cnt", "tio2", "sio2", "nanoparticle")):
                # Check concentration
                for m in materials:
                    if (m.get("name") or "").strip().lower() == name:
                        wt = _safe_float(
                            m.get("wt_percent", m.get("amount")),
                            0.0,
                        )
                        if wt > 1.0:  # > 1 wt% blocks UV
                            failures.append(
                                f"Nanofiller '{name}' at {wt}% will scatter/block UV light. "
                                f"Photocuring requires transparent solution. Reduce to < 1 wt% or switch to chemical crosslinking."
                            )
                        break

    # ---- Rule 5: PEG plasticizer leaching ----
    if _has_material("peg") and "plasticiz" in " ".join(mat_roles.values()):
        post_soak = _safe_float(p.get("post_soak_hours"), 0.0)
        if post_soak > 2:
            failures.append(
                f"PEG plasticizer is water-soluble. "
                f"Post-soak of {post_soak}h will leach PEG, reducing plasticization effect."
            )

    # ---- Rule 6: Dual crosslinking without justification ----
    has_chemical_xl = any(k in text_blob for k in ("chemical crosslink", "covalent", "glutaraldehyde"))
    has_photo_xl = any(k in text_blob for k in ("photo", "uv cure", "光固化"))
    if has_chemical_xl and has_photo_xl:
        notes = (c.get("notes") or "").lower()
        expected = (c.get("expected_mechanism") or [])
        mech_text = " ".join(expected).lower() if isinstance(expected, list) else str(expected).lower()
        if "dual" not in notes and "dual" not in mech_text and "double network" not in mech_text:
            failures.append(
                "Both chemical crosslinking and photocuring detected. "
                "If this is a dual-network design, explicitly explain the rationale in expected_mechanism. "
                "Otherwise pick one crosslinking strategy."
            )

    # ---- Rule 7: Freeze-thaw without PVA is meaningless ----
    ft_cycles = _safe_float(p.get("freeze_thaw_cycles"), 0.0)
    if ft_cycles > 0 and not _has_material("pva"):
        failures.append(
            "Freeze-thaw cycling is a PVA-specific physical crosslinking mechanism. "
            "No PVA detected in materials."
        )

    # ---- Rule 8: Incompatible additive pairs ----
    if _has_material("cacl2") and _has_material("na2so4"):
        failures.append("CaCl2 and Na2SO4 will precipitate as CaSO4; incompatible in same formulation.")

    if _has_material("hcl") and _has_material("naoh"):
        failures.append("HCl and NaOH will neutralize each other; choose one pH adjuster.")

    # ---- Rule 9: Generic/placeholder material names ----
    GENERIC_MATERIAL_PATTERNS = [
        r"^polymer\s*[a-z]?$",         # "Polymer A", "Polymer B", etc.
        r"^polymer\s*\d+$",            # "Polymer 1", "Polymer 2"
        r"^additive\s*[a-z]?$",        # "Additive A"
        r"^material\s*[a-z\d]$",       # "Material A", "Material 1"
        r"^agent\s*[a-z]$",            # "Agent A"
        r"^monomer\s*[a-z]$",          # "Monomer A"
    ]
    for name in mat_names_lower:
        for pat in GENERIC_MATERIAL_PATTERNS:
            if re.match(pat, name):
                failures.append(
                    f"Generic/placeholder material name '{name}' is not allowed. "
                    f"Use real chemical names (e.g., 'PVA', 'polyvinyl alcohol', 'PEG 400')."
                )
                break

    return failures


def _normalize_diag_ctx(diag_ctx: Dict[str, Any] | None) -> Dict[str, Any]:
    diag_ctx = diag_ctx or {}
    factors = diag_ctx.get("factors") or []
    factor_levels: Dict[str, set[str]] = {}
    for f in factors:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        factor_levels[name] = {str(x) for x in (f.get("levels") or [])}
    return {
        "factors": factors,
        "factor_levels": factor_levels,
        "parent_material_names": {str(x).strip().lower() for x in (diag_ctx.get("parent_material_names") or []) if str(x).strip()},
        "allow_extension": bool(diag_ctx.get("allow_extension", False)),
    }

def _check_time_and_slow_process(c: dict, p: dict) -> Tuple[float, bool, List[str], List[str]]:
    """Return (total_h, time_exceeded, forbidden_flags, failure_messages)."""
    total_h = c.get("total_prep_time_hours")
    if total_h is None:
        total_h, _bd = compute_prep_time_hours(c)

    post_soak_h = _safe_float(p.get("post_soak_hours"), 0.0)
    ft_cycles = _safe_float(p.get("freeze_thaw_cycles"), 0.0)
    cycle_hours = _safe_float(p.get("cycle_hours"), 0.0)

    time_exceeded = False
    forbidden_flags: List[str] = []
    failures: List[str] = []

    if total_h > 24.0:
        failures.append(f"total_prep_time_hours={total_h:.2f} > 24 h (violates one-day requirement)")
        time_exceeded = True

    if ft_cycles >= 2 and cycle_hours >= 24.0:
        forbidden_flags.append("forbidden_12h_freeze_12h_thaw_multi_cycles")
    if post_soak_h >= 24.0:
        forbidden_flags.append("forbidden_48h_soak_or_ge_24h")

    if forbidden_flags:
        failures.append(f"forbidden_slow_process_pattern: {';'.join(forbidden_flags)}")
        time_exceeded = True

    return total_h, time_exceeded, forbidden_flags, failures


# Network type normalization: map variants to canonical forms
_NETWORK_ALIASES: dict[str, str] = {
    "chemical": "chemical", "fast_chemical": "chemical",
    "photo": "photo", "photocured": "photo", "uv": "photo",
    "physical": "physical", "freeze-thaw": "physical", "freeze_thaw": "physical",
    "room_temp_gel": "room_temp_gel", "room_temp": "room_temp_gel",
}


def _norm_network(net: str) -> str:
    net = net.strip().lower().replace(" ", "_").replace("-", "_")
    for alias, canonical in _NETWORK_ALIASES.items():
        if alias in net:
            return canonical
    return net


# Crosslinker name aliases: map Chinese/abbreviated names to canonical English names
_CL_ALIASES: dict[str, str] = {
    "glutaraldehyde": "glutaraldehyde", "glut": "glutaraldehyde",
    "borax": "borax",
    "pegda": "pegda",
    "mbaa": "mbaa", "bisacrylamide": "mbaa", "bis": "mbaa",
    "genipin": "genipin",
    "citric": "citric_acid", "citric_acid": "citric_acid",
    "ga": "glutaraldehyde",
}
# Priority: higher = more likely to be the actual crosslinker (disambiguates monomer vs crosslinker)
_CL_PRIORITY: dict[str, int] = {
    "mbaa": 10, "bisacrylamide": 10,
    "glutaraldehyde": 8, "borax": 8,
    "pegda": 7, "genipin": 7,
    "citric_acid": 5, "acrylamide": 1,
}


def _norm_crosslinker(name: str) -> str:
    """Normalize crosslinker name to canonical form, handling Chinese/English variants."""
    name_lower = name.strip().lower()
    # Check if any Chinese GA variants
    if any(kw in name_lower for kw in ("戊二醛", "glutaraldehyde", "glut")):
        return "glutaraldehyde"
    if any(kw in name_lower for kw in ("硼砂", "borax")):
        return "borax"
    if any(kw in name_lower for kw in ("盐酸", "hcl")):
        return "hcl"
    # Check alias table
    for alias, canonical in _CL_ALIASES.items():
        if alias in name_lower:
            return canonical
    return name_lower


def _infer_crosslinker_from_materials(parent: dict) -> str:
    """When parent has no crosslinker field, infer from materials/additives.

    Collects all candidate crosslinker names and returns the highest-priority one,
    to avoid confusing monomers (acrylamide) with crosslinkers (MBAA).
    """
    candidates: list[tuple[int, str]] = []
    f = parent.get("formulation", {}) or {}
    # Look in formulation.additives
    for a in (f.get("additives", []) or []):
        nm = str(a.get("name") or "")
        cl = _norm_crosslinker(nm)
        if cl != nm.lower() and cl != "none":
            prio = _CL_PRIORITY.get(cl, 0)
            candidates.append((prio, cl))
    # Look in materials
    for m in (parent.get("materials", []) or []):
        nm = str(m.get("name") or "")
        role = str(m.get("role") or "").lower()
        cl = _norm_crosslinker(nm)
        if cl != nm.lower() and cl != "none":
            prio = _CL_PRIORITY.get(cl, 0)
            if "crosslink" in role:
                prio += 5  # boost explicitly-role crosslinkers
            candidates.append((prio, cl))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    # Fallback: check method string
    method = (f.get("crosslink_or_phys_method") or "").lower()
    if "ga" in method or "glutar" in method or "戊二醛" in method:
        return "glutaraldehyde"
    if "borax" in method or "硼砂" in method:
        return "borax"
    if "photo" in method or "uv" in method:
        return "photo_initiator"
    return "none"


def _chemistry_fingerprint(c: dict) -> str:
    """Return a stable fingerprint for the candidate's chemical system."""
    f = c.get("formulation", {}) or {}
    cl = f.get("crosslinker") or {}
    cl_name = (cl.get("name") or "").strip() if isinstance(cl, dict) else ""
    cl_norm = _norm_crosslinker(cl_name) if cl_name else "none"

    # If crosslinker is "none", try to infer from additives/materials (old pipeline format)
    if cl_norm == "none":
        inferred = _infer_crosslinker_from_materials(c)
        if inferred != "none":
            cl_norm = inferred

    network = _norm_network(f.get("network_type") or "")
    return f"{network}|{cl_norm}"


def _check_chemistry_lineage(c: dict, parent: dict | None) -> list[str]:
    """Return failures if child switched chemical system without marking extension."""
    if parent is None:
        return []
    child_fp = _chemistry_fingerprint(c)
    parent_fp = _chemistry_fingerprint(parent)
    if child_fp == parent_fp:
        return []
    is_ext = bool(c.get("is_extension", c.get("iteration_metadata", {}).get("is_extension", False)))
    ext_reason = (c.get("extension_reason") or c.get("iteration_metadata", {}).get("extension_reason") or "").strip()
    if not is_ext or not ext_reason:
        return [
            f"chemistry_lineage_broken: child system [{child_fp}] differs from parent [{parent_fp}]. "
            f"Set is_extension=true and provide extension_reason explaining the switch."
        ]
    return []


def audit_candidate(
    c: dict,
    diag_ctx: Dict[str, Any] | None = None,
    parent_candidate: dict | None = None,
) -> Tuple[bool, List[str], List[str], Dict[str, Any]]:
    failed: List[str] = []
    warnings: List[str] = []

    f = c.get("formulation", {}) or {}
    p = c.get("processing", {}) or {}
    gen_mode = (c.get("generation_mode") or GenerationMode.FALLBACK).strip().lower()

    # 0) 化学可行性硬检查（最高优先级）
    chemical_failures = _check_chemical_feasibility(c)
    if chemical_failures:
        for cf in chemical_failures:
            failed.append("chemical_infeasible: " + cf)

    # 0.5) 化学体系连续性检查
    lineage_failures = _check_chemistry_lineage(c, parent_candidate)
    if lineage_failures:
        failed.extend(lineage_failures)

    # 1) 基础硬检查：关键参数 + 风险条目
    if f.get("pva_wt_percent") is None:
        failed.append("missing formulation.pva_wt_percent")

    for k in ["freeze_thaw_cycles", "post_soak_hours"]:
        if p.get(k) is None:
            failed.append(f"missing processing.{k}")
    # freeze_temp_C / thaw_temp_C / cycle_hours are only required when FT > 0
    if _safe_int(p.get("freeze_thaw_cycles"), 0) > 0:
        for k in ["freeze_temp_C", "thaw_temp_C", "cycle_hours"]:
            if p.get(k) is None:
                failed.append(f"missing processing.{k}")

    for a in f.get("additives") or []:
        if isinstance(a, dict) and a.get("wt_percent_range") is None:
            failed.append("additive missing wt_percent_range")

    rlist = c.get("risks_and_mitigations") or []
    if len(rlist) < REQUIRED_RISKS:
        failed.append("insufficient risks_and_mitigations (need >= 2)")

    rag_supported, rag_labels, rag_warnings = _rag_evidence_summary(c)
    warnings.extend(rag_warnings)
    if parent_candidate is not None and not rag_supported:
        warnings.append("rag_evidence_missing: candidate has no per-candidate RAG support field")

    # 2) 机制文本 & DI water 提示
    text_blob = _text_blob_for_mechanism(c)  # 已经 lower()
    if "di water" not in text_blob and "di_water" not in text_blob:
        cid = c.get("candidate_id", "<unknown>")
        print(f"[WARN] candidate {cid}: no explicit DI water consideration")

    # 3) 材料完整性（只看信息是否闭合）
    is_complete, missing_material_fields = check_material_completeness(c)
    materials_incomplete = not is_complete
    if materials_incomplete:
        failed.append("materials_incomplete: " + "; ".join(missing_material_fields))

    # 4) 机制 → 具体材料名 + 功能组分检查
    mech_blob = text_blob
    formulation_additives = f.get("additives") or []
    mat_names_lower: set[str] = set()

    materials = c.get("materials") or []
    for m in materials:
        nm = (m.get("name") or "").strip().lower()
        if nm:
            mat_names_lower.add(nm)

    for a in formulation_additives:
        if isinstance(a, dict):
            nm = (a.get("name") or "").strip().lower()
        else:
            nm = str(a).strip().lower()
        if nm:
            mat_names_lower.add(nm)

    def _ensure_mechanism_has_material(keyword_list: List[str], human_desc: str):
        if any(k in mech_blob for k in keyword_list):
            if not mat_names_lower:
                failed.append(f"{human_desc} mentioned but no concrete material name or loading given")
            # 具体 wt% 要求在 completeness_check 里已覆盖

    _ensure_mechanism_has_material(["plasticization", "plasticizer"], "plasticization mechanism")
    _ensure_mechanism_has_material(["surface lubrication", "lubricant"], "surface lubrication mechanism")
    _ensure_mechanism_has_material(["nanocomposite", "nanofiller", "nanoparticle"], "nanocomposite reinforcement")
    _ensure_mechanism_has_material(["copolymer"], "copolymer modification")
    _ensure_mechanism_has_material(
        ["viscosity modifier", "viscosity modification", "rheology", "thickener"],
        "viscosity modification",
    )

    # chemical_crosslinking: 必须有交联剂 + 引发剂/催化剂
    if (
        "chemical crosslink" in mech_blob
        or "chemical_crosslink" in mech_blob
        or "covalent" in mech_blob
    ):
        cl = f.get("crosslinker") or {}
        if not cl.get("name") or cl.get("wt_percent") is None:
            failed.append("mechanism chemical_crosslinking but crosslinker.name/wt_percent missing")
        init_cat = f.get("initiator_or_catalyst") or {}
        if not init_cat.get("name") or init_cat.get("wt_percent") is None:
            failed.append("mechanism chemical_crosslinking but initiator_or_catalyst.name/wt_percent missing")

    # 5) materials vs formulation 一致性（双向 + 自动同步）
    (
        materials_complete,
        formulation_complete,
        mat_consistent,
        role_mapping_complete,
        mat_consistency_errs,
    ) = normalize_materials_and_formulation(c)

    if not mat_consistent:
        failed.append("materials_vs_formulation_inconsistent: " + "; ".join(mat_consistency_errs))

    # 6) 时间预算 & 禁用工艺模式（硬 FAIL）
    post_soak_h = _safe_float(p.get("post_soak_hours"), 0.0)
    if post_soak_h > 2.0 and post_soak_h <= 24.0:
        warnings.append(f"post_soak_hours={post_soak_h} h > 2 h (slow for screening)")

    total_h, time_exceeded, forbidden_flags, time_failures = _check_time_and_slow_process(c, p)
    failed.extend(time_failures)

    # 7) 迭代信息字段 + DOE / extension 约束
    iter_missing: List[str] = []
    diag_info = _normalize_diag_ctx(diag_ctx)
    factor_levels = diag_info["factor_levels"]
    factor_names = list(factor_levels.keys())

    iteration_meta = c.get("iteration_metadata") or {}
    diagnosis_evidence_used = c.get("diagnosis_evidence_used") or iteration_meta.get("diagnosis_evidence_used") or []
    mutation_rationale = c.get("mutation_rationale") or iteration_meta.get("mutation_rationale") or ""
    diagnosis_levers_used = c.get("diagnosis_levers_used") or iteration_meta.get("diagnosis_levers_used") or []
    doe_levels = c.get("doe_factor_levels") or c.get("doe_factor_levels_used") or iteration_meta.get("doe_factor_levels") or {}
    is_extension = bool(c.get("is_extension", iteration_meta.get("is_extension", False)))
    extension_reason = (c.get("extension_reason") or iteration_meta.get("extension_reason") or "").strip()

    if gen_mode in (GenerationMode.RESULT_DRIVEN, GenerationMode.DIAGNOSIS_DRIVEN):
        if not c.get("parent_candidates"):
            iter_missing.append("missing parent_candidates for non-fallback generation_mode")
        if not diagnosis_evidence_used:
            iter_missing.append("missing diagnosis_evidence_used for non-fallback generation_mode")
        if not mutation_rationale:
            iter_missing.append("missing mutation_rationale for non-fallback generation_mode")
        if not diagnosis_levers_used:
            iter_missing.append("missing diagnosis_levers_used for non-fallback generation_mode")
        if not isinstance(doe_levels, dict) or not doe_levels:
            iter_missing.append("missing doe_factor_levels for non-fallback generation_mode")

    outside_doe_reasons: List[str] = []
    doe_compliance = True

    # Normalize factor names: lowercase, spaces/hyphens → underscores
    def _normalize_factor_name(s: str) -> str:
        return s.strip().lower().replace(" ", "_").replace("-", "_")

    doe_levels_normalized = {_normalize_factor_name(k): k for k in doe_levels}

    # Fallback: auto-populate missing DOE factors from candidate's actual data
    _FACTOR_FALLBACK: dict[str, list] = {
        # normalized_name: [candidate_key_path, value_transform]
        "post_soak_hours":    [["processing", "post_soak_hours"], str],
        "freeze_thaw_cycles": [["processing", "freeze_thaw_cycles"], str],
        "pva_wt_percent":     [["formulation", "pva_wt_percent"], str],
        "cross_linker_concentration": [["formulation", "crosslinker", "wt_percent"], lambda v: f"{_safe_float(v):g} wt%"],
        "additive_combination": [["formulation", "additives"], lambda v: (v[0]["name"] if v else "")],
    }

    # Factors that are only required when candidate has specific content
    _CONDITIONAL_FACTORS: dict[str, str] = {
        "additive_type": "additives",
        "additive_combination": "additives",
    }

    if factor_names:
        for name in factor_names:
            name_norm = _normalize_factor_name(name)
            if name_norm not in doe_levels_normalized:
                # Skip conditional factors when candidate doesn't have the relevant content
                if name_norm in _CONDITIONAL_FACTORS:
                    dep_field = _CONDITIONAL_FACTORS[name_norm]
                    f_chk = c.get("formulation", {}) or {}
                    adds = f_chk.get("additives", []) or []
                    if dep_field == "additives" and not adds:
                        continue  # No additives in this candidate, skip requirement
                # Try fallback from candidate data
                fb = _FACTOR_FALLBACK.get(name_norm)
                fallback_val = None
                if fb:
                    obj = c
                    for key in fb[0]:
                        obj = (obj or {}).get(key) if isinstance(obj, dict) else None
                    if obj is not None:
                        try:
                            fallback_val = fb[1](obj)
                        except (ValueError, TypeError):
                            pass
                if fallback_val and str(fallback_val).strip():
                    doe_levels[name_norm.replace("_", " ").title()] = fallback_val
                    doe_levels_normalized[name_norm] = name_norm.replace("_", " ").title()
                else:
                    doe_compliance = False
                    iter_missing.append("doe_factor_levels missing factors from diagnosis.next_round_doe: " + name)
                    continue
            orig_key = doe_levels_normalized[name_norm]
            lvl = str(doe_levels.get(orig_key))
            allowed = {_normalize_factor_name(s) for s in (factor_levels.get(name) or set())}
            if factor_levels.get(name) and _normalize_factor_name(lvl) not in allowed:
                doe_compliance = False
                outside_doe_reasons.append(f"{name}={lvl} outside allowed levels {sorted(factor_levels[name])}")

    parent_material_names = diag_info["parent_material_names"]
    current_material_names = {str((m.get("name") or "")).strip().lower() for m in materials if str((m.get("name") or "")).strip()}
    # Collect all DOE factor level values as lowercase for exemption
    doe_level_values: set[str] = set()
    for levels in factor_levels.values():
        doe_level_values.update(s.lower() for s in levels)
    new_materials = sorted(
        n for n in current_material_names
        if parent_material_names
        and n not in parent_material_names
        and n not in BASELINE_MATERIALS
        and not any(dlv in n or n in dlv for dlv in doe_level_values)
    )
    if new_materials and not diag_info.get("allow_extension", False):
        outside_doe_reasons.append("new materials introduced before DOE completion: " + ", ".join(new_materials))
        doe_compliance = False

    outside_doe_space = bool(outside_doe_reasons)
    if outside_doe_space and (not is_extension or not extension_reason):
        iter_missing.append("DOE-external candidate without valid is_extension=true and extension_reason: " + "; ".join(outside_doe_reasons))
    if outside_doe_space and is_extension and not diag_info.get("allow_extension", False):
        iter_missing.append("extension candidate generated before main DOE coverage was completed")

    if iter_missing:
        failed.append("iteration_metadata_incomplete: " + "; ".join(iter_missing))

    # 8) 归并 rejection_reason
    rejection_reason: str | None = None
    if chemical_failures:
        rejection_reason = "chemical_infeasible"
    elif lineage_failures:
        rejection_reason = "chemistry_lineage_broken"
    elif materials_incomplete or not mat_consistent:
        if time_exceeded:
            rejection_reason = "materials_incomplete_and_time_exceeded"
        else:
            rejection_reason = "materials_incomplete_or_inconsistent"
    elif time_exceeded:
        rejection_reason = "time_exceeded"
    elif iter_missing:
        rejection_reason = "iteration_metadata_incomplete"

    # 9) extra 汇总，供外层写回 candidates / audits
    extra = {
        "total_prep_time_hours": total_h,
        "fits_one_day_requirement": bool(total_h <= 24.0 and not forbidden_flags),
        "completeness_check": {
            "is_complete": not materials_incomplete,
            "missing_fields": missing_material_fields,
        },
        "materials_complete": materials_complete,
        "formulation_complete": formulation_complete,
        "formulation_role_mapping_complete": role_mapping_complete,
        "materials_vs_formulation_consistency": mat_consistent,
        "rejection_reason": rejection_reason,
        "forbidden_slow_process_pattern": forbidden_flags,
        "doe_compliance": doe_compliance,
        "outside_doe_space": outside_doe_space,
        "is_extension": is_extension,
        "extension_reason": extension_reason,
        "doe_factor_levels_used": doe_levels,
        "diagnosis_levers_used": diagnosis_levers_used,
        "rag_supported": rag_supported,
        "rag_evidence_labels": rag_labels,
    }

    ok = (
        len(failed) == 0
        and extra["fits_one_day_requirement"]
        and extra["completeness_check"]["is_complete"]
        and extra["materials_vs_formulation_consistency"]
    )

    return ok, failed, warnings, extra


def run_auditor_rulebased(
    out_dir: Path,
    round_idx: int,
    candidates_path: Path,
    n_select: int
) -> Tuple[Path, List[str]]:
    # 读取候选
    cand_obj = read_json(candidates_path)
    cands = cand_obj["candidates"]

    # 构造上一轮诊断上下文，用于 diagnosis_driven 约束
    diag_ctx = None
    parent_round_idx = cand_obj.get("parent_round_idx") or max(1, round_idx - 1)
    workspace = RunWorkspace(out_dir)
    diag_path = workspace.diagnosis_path(parent_round_idx)
    if diag_path.exists():
        diag = read_json(diag_path)
        parent_cand_path = workspace.candidates_path(parent_round_idx)
        parent_material_names = set()
        if parent_cand_path.exists():
            parent_obj = read_json(parent_cand_path)
            for pc in parent_obj.get("candidates", []):
                parent_material_names |= _candidate_material_names(pc)
        next_round_doe = diag.get("next_round_doe", {}) or {}
        diag_ctx = {
            "factors": next_round_doe.get("factors", []),
            "parent_material_names": sorted(parent_material_names),
            "allow_extension": bool(next_round_doe.get("allow_extension", False) or diag.get("allow_extension", False)),
        }

    # Build parent candidate lookup for chemistry lineage check
    parent_by_id: dict[str, dict] = {}
    if parent_round_idx:
        parent_cand_path = workspace.candidates_path(parent_round_idx)
        if parent_cand_path.exists():
            parent_obj = read_json(parent_cand_path)
            for pc in parent_obj.get("candidates", []):
                pid = pc.get("candidate_id", "")
                if pid:
                    parent_by_id[pid] = pc

    audits: List[Dict[str, Any]] = []
    passed_ids: List[str] = []
    limited_exploration_count = sum(1 for cand in cands if cand.get("design_type") == "limited_exploration")

    for c in cands:
        parent_id = c.get("parent_candidate_id") or ""
        parent = parent_by_id.get(parent_id)
        c["changed_variables"] = detect_changed_variables(c, parent)
        c["changed_variable_names"] = [x["variable"] for x in c["changed_variables"]]
        c.setdefault("experimental_status", "not_measured")
        c["black_box_jump_score"] = black_box_jump_score(c, parent)
        ok, failed_rules, warnings, extra = audit_candidate(c, diag_ctx=diag_ctx, parent_candidate=parent)

        constrained_failures = validate_candidate_constraints(
            c,
            parent_by_id=parent_by_id,
            round_limited_exploration_count=limited_exploration_count,
            require_parent=round_idx > 1,
        )
        if constrained_failures:
            failed_rules.extend(constrained_failures)
            ok = False

        # 从 failed_rules 中抽取“硬约束失败”标签
        hard_constraint_failures: List[str] = []
        for fr in failed_rules:
            if fr.startswith("materials_vs_formulation_inconsistent"):
                hard_constraint_failures.append("materials_vs_formulation_inconsistent")
            if fr.startswith("total_prep_time_hours"):
                hard_constraint_failures.append("total_prep_time_hours>24h")
            if fr.startswith("forbidden_slow_process_pattern"):
                hard_constraint_failures.append("forbidden_slow_process_pattern")
            if "doe_factor_levels" in fr:
                hard_constraint_failures.append("doe_factor_levels_missing")
            if "parent_candidate_id" in fr:
                hard_constraint_failures.append("parent_candidate_id_invalid")
            if "baseline_reproduction" in fr:
                hard_constraint_failures.append("baseline_reproduction_not_exact")
            if "limited_exploration count exceeds" in fr:
                hard_constraint_failures.append("limited_exploration_count_exceeds_1")
            if "single_factor_perturbation" in fr:
                hard_constraint_failures.append("single_factor_changed_variables_invalid")
            if "local_optimization changed" in fr:
                hard_constraint_failures.append("local_optimization_changed_variables_invalid")
            if "missing if_better" in fr or "missing if_worse" in fr:
                hard_constraint_failures.append("if_better_if_worse_missing")
            if "missing PVA" in fr:
                hard_constraint_failures.append("pva_required")

        # 去重
        hard_constraint_failures = list(dict.fromkeys(hard_constraint_failures))

         # 把审计得到的关键字段同步回候选
        c["total_prep_time_hours"] = extra["total_prep_time_hours"]
        c["fits_one_day_requirement"] = extra["fits_one_day_requirement"]
        c["completeness_check"] = extra["completeness_check"]
        c["materials_complete"] = extra["materials_complete"]
        c["formulation_complete"] = extra["formulation_complete"]
        c["formulation_role_mapping_complete"] = extra["formulation_role_mapping_complete"]
        c["materials_vs_formulation_consistency"] = extra["materials_vs_formulation_consistency"]
        # Classify: PASS / WARNING (metadata-only) / FAIL (formulation invalid)
        if ok:
            c["audit_status"] = "PASS"
        elif (
            extra["rejection_reason"] == "iteration_metadata_incomplete"
            and extra["materials_complete"]
            and extra["materials_vs_formulation_consistency"]
            and extra["fits_one_day_requirement"]
        ):
            c["audit_status"] = "WARNING"
        else:
            c["audit_status"] = "FAIL"
        c["experimental_status"] = c.get("experimental_status", "not_measured")
        c["rejection_reason"] = extra["rejection_reason"]
        c["hard_constraint_failures"] = hard_constraint_failures
        c["doe_compliance"] = extra["doe_compliance"]
        c["outside_doe_space"] = extra["outside_doe_space"]
        c["is_extension"] = extra["is_extension"]
        c["extension_reason"] = extra["extension_reason"]
        c["doe_factor_levels_used"] = extra["doe_factor_levels_used"]
        c["diagnosis_levers_used"] = extra["diagnosis_levers_used"]
        c["rag_supported"] = extra["rag_supported"]
        c["rag_evidence_labels"] = extra["rag_evidence_labels"]

        # missing_info 只保留“材料缺失”类信息（去重）
        missing_info = c.get("missing_info") or []
        missing_info.extend(extra["completeness_check"]["missing_fields"])
        c["missing_info"] = list(dict.fromkeys(missing_info))

        audits.append({
            "candidate_id": c["candidate_id"],
            "decision": c["audit_status"],
            "audit_status": c["audit_status"],
            "experimental_status": c.get("experimental_status", "not_measured"),
            "changed_variables": c.get("changed_variables", []),
            "black_box_jump_score": c.get("black_box_jump_score", 0),
            "failed_rules": failed_rules,
            "warnings": warnings,
            "required_fixes": [],
            "generation_mode": c.get("generation_mode", "fallback"),
            "total_prep_time_hours": extra["total_prep_time_hours"],
            "fits_one_day_requirement": extra["fits_one_day_requirement"],
            "completeness_check": extra["completeness_check"],
            "materials_complete": extra["materials_complete"],
            "formulation_complete": extra["formulation_complete"],
            "formulation_role_mapping_complete": extra["formulation_role_mapping_complete"],
            "materials_vs_formulation_consistency": extra["materials_vs_formulation_consistency"],
            "rejection_reason": extra["rejection_reason"],
            "forbidden_slow_process_pattern": extra["forbidden_slow_process_pattern"],
            "hard_constraint_failures": hard_constraint_failures,
            "doe_compliance": extra["doe_compliance"],
            "outside_doe_space": extra["outside_doe_space"],
            "is_extension": extra["is_extension"],
            "extension_reason": extra["extension_reason"],
            "doe_factor_levels_used": extra["doe_factor_levels_used"],
            "diagnosis_levers_used": extra["diagnosis_levers_used"],
            "rag_supported": extra["rag_supported"],
            "rag_evidence_labels": extra["rag_evidence_labels"],
        })

        # 最终推荐：必须 PASS 且为 diagnosis_driven / result_driven
        if ok and (
            round_idx == 1
            or c.get("generation_mode") in (GenerationMode.DIAGNOSIS_DRIVEN, GenerationMode.RESULT_DRIVEN)
        ):
            passed_ids.append(c["candidate_id"])

    selected = passed_ids[:n_select]

    # 兜底提示：R1 且一个都没通过时，给出 warning
    if not selected and round_idx == 1:
        print(
            f"[WARN] No candidates PASSED strict audit in R{round_idx}; "
            f"no candidates will enter second-round recommendation list."
        )

    # 简单 DOE 规划（可根据需要再细化）
    doe_plan = {
        "factors": [
            {
                "name": "pva_wt_percent",
                "levels": ["lower", "higher"],
                "mapping_note": "Cover concentration variation",
            },
            {
                "name": "network_type",
                "levels": ["fast_chemical", "photocured", "room_temp_gel"],
                "mapping_note": "Prioritize fast one-day-prep networks",
            },
        ],
        "coverage_check": [
            "Ensure selected candidates span multiple polymer wt% and "
            "fast-crosslinking strategies (chemical, photo, dynamic, room-temp)."
        ],
    }

    out = {
        "audits": audits,
        "selected_for_round": selected,
        "doe_plan": doe_plan,
        "missing_info": [],
    }
    p = out_dir / f"R{round_idx}_audits.json"
    write_json(p, out)

    # 把带审计字段的 candidates 写回原始 candidates 文件
    cand_obj["candidates"] = cands
    cand_obj["inheritance_table"] = build_inheritance_table(cands)
    write_json(candidates_path, cand_obj)
    table_path = out_dir / f"R{round_idx}_inheritance_table.md"
    table_path.write_text(inheritance_table_markdown(cands), encoding="utf-8")

    return p, selected
