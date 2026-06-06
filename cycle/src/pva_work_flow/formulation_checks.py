"""Formulation integrity checks extracted from workflow.py.

These helpers validate material completeness, sync formulation/materials
bi-directionally, estimate preparation time, and collect material names.
They are used by generator.py, audit.py, and workflow.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .config import CandidateDict


def _text_blob_for_mechanism(c: CandidateDict) -> str:
    parts = []
    mech = c.get("expected_mechanism") or []
    if isinstance(mech, list):
        parts.extend(mech)
    elif isinstance(mech, str):
        parts.append(mech)
    parts.append(c.get("notes", "") or "")
    parts.append(json.dumps(c.get("risks_and_mitigations", []), ensure_ascii=False))
    return " ".join(parts).lower()


def compute_prep_time_hours(c: CandidateDict) -> Tuple[float, Dict[str, float]]:
    breakdown = {
        "dissolution_h": 0.0,
        "degas_h": 0.0,
        "gelation_h": 0.0,
        "crosslink_h": 0.0,
        "freeze_thaw_h": 0.0,
        "soak_h": 0.0,
        "curing_or_anneal_h": 0.0,
        "other_h": 0.0,
    }

    _proc = c.get("process")
    if isinstance(_proc, list):
        steps = _proc
    elif isinstance(_proc, dict):
        steps = _proc.get("steps") or []
    else:
        steps = (c.get("processing") or {}).get("steps") or []

    for step in steps:
        if isinstance(step, dict):
            name = (step.get("name") or "").lower()
            h = float(step.get("duration_hours") or 0.0)
        else:
            name = str(step).lower()
            h = 0.0

        if "dissolve" in name:
            breakdown["dissolution_h"] += h
        elif "degas" in name or "degassing" in name:
            breakdown["degas_h"] += h
        elif "gel" in name:
            breakdown["gelation_h"] += h
        elif "crosslink" in name:
            breakdown["crosslink_h"] += h
        elif "soak" in name:
            breakdown["soak_h"] += h
        elif "cure" in name or "anneal" in name:
            breakdown["curing_or_anneal_h"] += h
        else:
            breakdown["other_h"] += h

    p = c.get("processing") or {}
    ft_cycles = float(p.get("freeze_thaw_cycles") or 0.0)
    cycle_hours = float(p.get("cycle_hours") or 0.0)
    ft_time = ft_cycles * cycle_hours
    breakdown["freeze_thaw_h"] += ft_time

    soak_h = float(p.get("post_soak_hours") or 0.0)
    breakdown["soak_h"] += soak_h

    total = sum(breakdown.values())
    return total, breakdown


def check_material_completeness(c: CandidateDict) -> Tuple[bool, List[str]]:
    missing: List[str] = []

    materials = c.get("materials") or []
    formulation = c.get("formulation") or {}
    _proc = c.get("process")
    process = _proc if isinstance(_proc, dict) else (c.get("processing") or {})

    main_polymers = [m for m in materials if (m.get("role") or "").lower() in ["polymer", "matrix", "main_polymer"]]
    if not main_polymers and formulation.get("pva_wt_percent") is None:
        missing.append("missing main polymer name and loading")

    for i, m in enumerate(materials):
        if not m.get("name"):
            missing.append(f"materials[{i}].name missing")
        if not m.get("role"):
            missing.append(f"materials[{i}].role missing")
        if m.get("amount") is None:
            missing.append(f"materials[{i}].amount missing")
        if not m.get("unit"):
            missing.append(f"materials[{i}].unit missing")
        if not m.get("basis"):
            missing.append(f"materials[{i}].basis missing (e.g., wt% of total)")

    total_mass = process.get("total_batch_mass_g")
    if total_mass is None:
        if not all((m.get("basis") or "").startswith("wt%") for m in materials):
            missing.append("total_batch_mass_g missing; cannot convert to fixed 20 g batch")

    blob = _text_blob_for_mechanism(c)

    def _ensure_material_role(role_keywords: List[str], role_name: str, human_desc: str):
        role_mats = [m for m in materials if any(k in (m.get("role") or "").lower() for k in role_keywords)]
        if not role_mats:
            missing.append(f"{human_desc} mentioned but no material with role={role_name} and wt% defined")

    if "chemical crosslink" in blob or "chemical_crosslink" in blob or "covalent" in blob:
        cl = formulation.get("crosslinker") or {}
        if not cl.get("name") or cl.get("wt_percent") is None:
            missing.append("chemical_crosslinking mentioned but formulation.crosslinker.name/wt_percent missing")
        init_cat = formulation.get("initiator_or_catalyst") or {}
        if not init_cat.get("name") or init_cat.get("wt_percent") is None:
            missing.append("chemical_crosslinking mentioned but initiator/catalyst name/wt_percent missing")
        _ensure_material_role(["crosslink", "cross-link"], "crosslinker", "chemical_crosslinking")

    if "photo" in blob or "uv cure" in blob:
        photo = formulation.get("photo_initiator") or {}
        if not photo.get("name") or photo.get("wt_percent") is None:
            missing.append("photocuring mentioned but photo_initiator name/wt_percent missing")
        if not process.get("light_conditions"):
            missing.append("photocuring mentioned but process.light_conditions missing (wavelength/intensity/time)")

    if "nanocomposite" in blob or "nanofiller" in blob or "nanoparticle" in blob:
        nano = formulation.get("nanofiller") or {}
        if not nano.get("name") or nano.get("wt_percent") is None or not nano.get("dispersion_method"):
            missing.append("nanocomposite reinforcement mentioned but nanofiller name/wt_percent/dispersion_method missing")
        _ensure_material_role(["nano", "nanofiller"], "nanofiller", "nanocomposite reinforcement")

    if "surface lubrication" in blob or "lubricant" in blob:
        _ensure_material_role(["lubricant", "lubrication"], "lubricant", "surface lubrication")

    if "copolymer" in blob:
        copolymers = [m for m in materials if (m.get("role") or "").lower() in ["copolymer", "second_polymer"]]
        if not copolymers:
            missing.append("copolymer modification mentioned but no second polymer with name/wt% defined")

    visc_triggers = ["viscosity_modifier", "viscosity modifier", "thickener", "thickening agent",
                     "rheology_modifier", "rheology modifier", "viscosifier"]
    if any(kw in blob for kw in visc_triggers):
        _ensure_material_role(["viscosity", "thickener", "rheology"], "viscosity_modifier", "viscosity modification")

    steps = process.get("steps") or []
    if not steps:
        missing.append("process.steps missing; cannot determine addition order")
    else:
        for step in steps:
            if not isinstance(step, dict) or step.get("order") is None:
                missing.append("process.steps[*].order missing; addition order not fully specified")
                break

    is_complete = len(missing) == 0
    return is_complete, missing


def normalize_materials_and_formulation(
    c: Dict[str, Any]
) -> Tuple[bool, bool, bool, bool, List[str]]:
    errors: List[str] = []

    materials = c.get("materials") or []
    if not isinstance(materials, list):
        materials = []
    c["materials"] = materials

    formulation = c.get("formulation") or {}
    if not isinstance(formulation, dict):
        formulation = {}
    c["formulation"] = formulation

    def _lower(s: str) -> str:
        return (s or "").strip().lower()

    def _find_material_idx(name: str, roles: List[str] | None = None) -> int | None:
        name_l = _lower(name)
        for i, m in enumerate(materials):
            if not isinstance(m, dict):
                continue
            if _lower(m.get("name") or "") != name_l:
                continue
            if roles is None:
                return i
            role_l = _lower(m.get("role") or "")
            if any(r in role_l for r in roles):
                return i
        return None

    def _ensure_material(name: str, role: str):
        if not name:
            return
        if _lower(name) == "none":
            return
        idx = _find_material_idx(name, None)
        if idx is not None:
            m = materials[idx]
            if not _lower(m.get("role") or ""):
                m["role"] = role
            return
        materials.append({"name": name, "role": role})

    # ---- formulation -> materials ----
    f_cl = (formulation.get("crosslinker") or {}) if isinstance(formulation.get("crosslinker"), dict) else {}
    cl_name = (f_cl.get("name") or "").strip()
    if cl_name:
        role = "crosslinker"
        if "glutaraldehyde" in _lower(cl_name):
            role = "crosslinker"
        _ensure_material(cl_name, role)

    f_ic = (formulation.get("initiator_or_catalyst") or {}) if isinstance(formulation.get("initiator_or_catalyst"), dict) else {}
    ic_name = (f_ic.get("name") or "").strip()
    if ic_name:
        role = "initiator_or_catalyst"
        if "hcl" in _lower(ic_name):
            role = "catalyst"
        _ensure_material(ic_name, role)

    f_pi = (formulation.get("photo_initiator") or {}) if isinstance(formulation.get("photo_initiator"), dict) else {}
    pi_name = (f_pi.get("name") or "").strip()
    if pi_name:
        _ensure_material(pi_name, "photo_initiator")

    f_nf = (formulation.get("nanofiller") or {}) if isinstance(formulation.get("nanofiller"), dict) else {}
    nf_name = (f_nf.get("name") or "").strip()
    if nf_name:
        _ensure_material(nf_name, "nanofiller")

    f_pl = (formulation.get("plasticizer") or {}) if isinstance(formulation.get("plasticizer"), dict) else {}
    pl_name = (f_pl.get("name") or "").strip()
    if pl_name:
        _ensure_material(pl_name, "plasticizer")

    f_adds = formulation.get("additives") or []
    if isinstance(f_adds, list):
        for a in f_adds:
            if not isinstance(a, (dict, str)):
                continue
            if isinstance(a, dict):
                nm = (a.get("name") or "").strip()
                if not nm:
                    continue
                role = _lower(a.get("role") or "") or "additive"
            else:
                nm = str(a).strip()
                if not nm:
                    continue
                role = "additive"
            _ensure_material(nm, role)

    # ---- materials -> formulation ----
    if not isinstance(formulation.get("additives"), list):
        formulation["additives"] = []
    adds_list = formulation["additives"]

    def _ensure_additive_in_formulation(name: str, role: str):
        name_l = _lower(name)
        for a in adds_list:
            if isinstance(a, dict) and _lower(a.get("name") or "") == name_l:
                if not _lower(a.get("role") or ""):
                    a["role"] = role
                return
        adds_list.append({"name": name, "role": role})

    for m in materials:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        role_l = _lower(m.get("role") or "")
        if not name or not role_l:
            continue

        if "crosslink" in role_l:
            tgt = formulation.get("crosslinker") or {}
            if not isinstance(tgt, dict):
                tgt = {}
            if not (tgt.get("name") or "").strip():
                tgt["name"] = name
            formulation["crosslinker"] = tgt

        elif role_l in ("catalyst", "initiator", "initiator_or_catalyst"):
            tgt = formulation.get("initiator_or_catalyst") or {}
            if not isinstance(tgt, dict):
                tgt = {}
            if not (tgt.get("name") or "").strip():
                tgt["name"] = name
            formulation["initiator_or_catalyst"] = tgt

        elif "photo" in role_l:
            tgt = formulation.get("photo_initiator") or {}
            if not isinstance(tgt, dict):
                tgt = {}
            if not (tgt.get("name") or "").strip():
                tgt["name"] = name
            formulation["photo_initiator"] = tgt

        elif "nano" in role_l:
            tgt = formulation.get("nanofiller") or {}
            if not isinstance(tgt, dict):
                tgt = {}
            if not (tgt.get("name") or "").strip():
                tgt["name"] = name
            formulation["nanofiller"] = tgt

        elif role_l in ("additive", "plasticizer", "lubricant"):
            _ensure_additive_in_formulation(name, role_l)

    # ---- consistency flags ----
    materials_complete = True

    def _require_material(name: str, expect_roles: List[str], label: str):
        nonlocal materials_complete
        if not name:
            return
        if _lower(name) == "none":
            return
        if _find_material_idx(name, expect_roles) is None:
            materials_complete = False
            errors.append(f"{label} '{name}' in formulation not present in materials with proper role")

    f_cl = formulation.get("crosslinker") or {}
    _require_material(f_cl.get("name") or "", ["crosslink"], "crosslinker")

    f_ic = formulation.get("initiator_or_catalyst") or {}
    _require_material(f_ic.get("name") or "", ["initiator", "catalyst"], "initiator_or_catalyst")

    f_pi = formulation.get("photo_initiator") or {}
    _require_material(f_pi.get("name") or "", ["photo"], "photo_initiator")

    f_nf = formulation.get("nanofiller") or {}
    _require_material(f_nf.get("name") or "", ["nano"], "nanofiller")

    for a in adds_list:
        if not isinstance(a, dict):
            continue
        nm = (a.get("name") or "").strip()
        if not nm:
            continue
        _require_material(nm, None, "additive/plasticizer")

    formulation_complete = True
    formulation_role_mapping_complete = True

    def _exists_in_formulation(name: str, role_l: str) -> bool:
        n_l = _lower(name)
        f = formulation

        if "crosslink" in role_l:
            tgt = f.get("crosslinker") or {}
            return _lower(tgt.get("name") or "") == n_l

        if role_l in ("catalyst", "initiator", "initiator_or_catalyst"):
            tgt = f.get("initiator_or_catalyst") or {}
            return _lower(tgt.get("name") or "") == n_l

        if "photo" in role_l:
            tgt = f.get("photo_initiator") or {}
            return _lower(tgt.get("name") or "") == n_l

        if "nano" in role_l:
            tgt = f.get("nanofiller") or {}
            return _lower(tgt.get("name") or "") == n_l

        if role_l in ("additive", "plasticizer", "lubricant"):
            for a in adds_list:
                if isinstance(a, dict) and _lower(a.get("name") or "") == n_l:
                    return True
        return False

    for m in materials:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        role_l = _lower(m.get("role") or "")
        if not name or not role_l:
            continue
        if role_l not in (
            "additive", "plasticizer", "nanofiller", "crosslinker",
            "catalyst", "initiator", "initiator_or_catalyst",
            "photo_initiator", "lubricant",
        ):
            continue
        if not _exists_in_formulation(name, role_l):
            formulation_complete = False
            formulation_role_mapping_complete = False
            errors.append(
                f"material '{name}' with role={role_l} in materials "
                "not reflected in formulation"
            )

    materials_vs_formulation_consistency = materials_complete and formulation_complete

    c["materials_complete"] = materials_complete
    c["formulation_complete"] = formulation_complete
    c["materials_vs_formulation_consistency"] = materials_vs_formulation_consistency
    c["formulation_role_mapping_complete"] = formulation_role_mapping_complete

    return (
        materials_complete,
        formulation_complete,
        materials_vs_formulation_consistency,
        formulation_role_mapping_complete,
        errors,
    )


def candidate_material_names(c: Dict[str, Any]) -> set[str]:
    """Return the set of material names declared in a candidate (materials + formulation)."""
    names = set()
    for m in c.get("materials") or []:
        nm = str((m.get("name") or "")).strip().lower()
        if nm:
            names.add(nm)
    f = c.get("formulation") or {}
    for a in f.get("additives") or []:
        if isinstance(a, dict):
            nm = str((a.get("name") or "")).strip().lower()
        else:
            nm = str(a).strip().lower()
        if nm:
            names.add(nm)
    for key in ("crosslinker", "initiator_or_catalyst", "photo_initiator", "nanofiller"):
        obj = f.get(key) or {}
        nm = str((obj.get("name") or "")).strip().lower() if isinstance(obj, dict) else ""
        if nm:
            names.add(nm)
    return names
