"""Deterministic constrained DOE skeleton generation.

The goal is to lower task difficulty for 14B-class models: code creates the
lineage skeleton, while the LLM only explains and completes it.
"""

from __future__ import annotations

import csv
from copy import deepcopy
import os
from pathlib import Path
from typing import Any, Dict, List

from pva_work_flow.core.utils import read_json
from pva_work_flow.artifacts.experiment_notes import is_candidate_mechanically_failed, load_notes


MAX_CONSTRAINED_CANDIDATES = 6
ENABLE_LIMITED_EXPLORATION_BY_DEFAULT = False
DEFAULT_NUMERIC_DECREASE_FACTOR = 0.5
DEFAULT_NUMERIC_INCREASE_FACTOR = 2.0
DEFAULT_FREEZE_THAW_STEP = 2
DEFAULT_POST_SOAK_RESCUE_FACTOR = 0.5
FIVE_MIN_SOAK_HOURS = round(5.0 / 60.0, 4)
BINARY_SEARCH_BOUNDS = {
    "pva_wt_percent": (5.0, 20.0),
    "primary_additive_wt_percent": (0.05, 1.5),
    "crosslinker_wt_percent": (0.05, 1.5),
    "initiator_or_catalyst_wt_percent": (0.02, 0.5),
    "post_soak_hours": (FIVE_MIN_SOAK_HOURS, 4.0),
    "freeze_thaw_cycles": (0.0, 5.0),
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _parent_round_path(out_dir: Path, round_idx: int, parent_round_idx: int | None = None) -> Path:
    source_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    return out_dir / f"R{source_round}_candidates.json"


def _candidate_by_id(candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c.get("candidate_id"): c for c in candidates if c.get("candidate_id")}


def _audit_best_ids(audit: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for entry in audit.get("best_candidates", []) or []:
        cid = entry.get("candidate_id") if isinstance(entry, dict) else entry
        if cid:
            ids.append(str(cid))
    return ids


def _audit_failed_ids(audit: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for entry in audit.get("failed_candidates", []) or []:
        cid = entry.get("candidate_id") if isinstance(entry, dict) else entry
        if cid:
            ids.append(str(cid))
    return ids


def _first_existing(ids: List[str], by_id: Dict[str, Dict[str, Any]], fallback: List[Dict[str, Any]]) -> Dict[str, Any]:
    for cid in ids:
        if cid in by_id:
            return by_id[cid]
    if fallback:
        return fallback[0]
    raise RuntimeError("No parent candidates available for constrained DOE skeleton")


def _value_for_variable(parent: Dict[str, Any], variable: str) -> Any:
    formulation = parent.get("formulation") or {}
    processing = parent.get("processing") or {}
    if variable == "pva_wt_percent":
        return formulation.get("pva_wt_percent")
    if variable == "freeze_thaw_cycles":
        return processing.get("freeze_thaw_cycles")
    if variable == "post_soak_hours":
        return processing.get("post_soak_hours")
    if variable == "crosslinker_wt_percent":
        return (formulation.get("crosslinker") or {}).get("wt_percent")
    if variable == "initiator_or_catalyst_wt_percent":
        return (formulation.get("initiator_or_catalyst") or {}).get("wt_percent")
    if variable == "primary_additive_wt_percent":
        additives = formulation.get("additives") or []
        for additive in additives:
            if isinstance(additive, dict) and additive.get("name") not in ("", None, "none"):
                return additive.get("wt_percent")
    return None


def _small_numeric_step(value: Any, direction: str) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if direction == "decrease":
        factor = _env_float("PVA_CONSTRAINED_NUMERIC_DECREASE_FACTOR", DEFAULT_NUMERIC_DECREASE_FACTOR)
        return max(0.0, round(number * factor, 4))
    if direction == "increase":
        factor = _env_float("PVA_CONSTRAINED_NUMERIC_INCREASE_FACTOR", DEFAULT_NUMERIC_INCREASE_FACTOR)
        return round(number * factor, 4)
    return number


def _binary_midpoint_step(variable: str, value: Any, direction: str) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    bounds = BINARY_SEARCH_BOUNDS.get(variable)
    if not bounds:
        return _small_numeric_step(value, direction)
    low, high = bounds
    if direction == "decrease":
        target = low
    elif direction == "increase":
        target = high
    else:
        return number
    midpoint = round((number + target) / 2.0, 4)
    if variable == "freeze_thaw_cycles":
        return max(0, int(round(midpoint)))
    return midpoint


def _small_step_for_variable(variable: str, value: Any, direction: str) -> Any:
    step_strategy = os.environ.get("PVA_CONSTRAINED_STEP_STRATEGY", "binary").strip().lower()
    if step_strategy in ("binary", "bisection", "midpoint"):
        return _binary_midpoint_step(variable, value, direction)
    if variable == "freeze_thaw_cycles":
        try:
            cycles = int(round(float(value)))
        except (TypeError, ValueError):
            return value
        step = max(1, _env_int("PVA_CONSTRAINED_FREEZE_THAW_STEP", DEFAULT_FREEZE_THAW_STEP))
        delta = -step if direction == "decrease" else step
        return max(0, cycles + delta)
    return _small_numeric_step(value, direction)


def _numeric_equivalent(a: Any, b: Any) -> bool:
    """Return True if a and b represent the same numeric value (e.g. 0 and 0.0)."""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _entry_is_duplicate(new_entry: Dict[str, Any], existing: List[Dict[str, Any]]) -> bool:
    """Check if a new entry duplicates any existing entry in semantics (variable + new_value)."""
    new_vars = {(v.get("variable", ""), v.get("new_value")) for v in new_entry.get("variables_changed", [])}
    if not new_vars:
        return False
    for ex in existing:
        ex_vars = {(v.get("variable", ""), v.get("new_value")) for v in ex.get("variables_changed", [])}
        if new_vars == ex_vars:
            return True
    return False


def _opposite_direction(direction: str) -> str:
    return "decrease" if direction == "increase" else "increase"


def _post_soak_rescue_value(parent_value: Any, suspected_value: Any) -> float | None:
    """Shorten parent post-soak when soaking is suspected to cause rupture."""
    try:
        parent = float(parent_value)
        suspected = float(suspected_value)
    except (TypeError, ValueError):
        return None
    low, _high = BINARY_SEARCH_BOUNDS["post_soak_hours"]
    if parent <= low:
        return None
    factor = _env_float("PVA_POST_SOAK_RESCUE_FACTOR", DEFAULT_POST_SOAK_RESCUE_FACTOR)
    factor = min(max(factor, 0.1), 0.9)
    target = _env_float("PVA_POST_SOAK_RESCUE_HOURS", FIVE_MIN_SOAK_HOURS)
    rescue = max(low, round(min(parent * factor, target), 4))
    if _numeric_equivalent(parent, rescue):
        return None
    return rescue


def _pva_rupture_rescue_value(parent_value: Any) -> float | None:
    """Raise low-PVA rupturing gels into the 18-20 wt% rescue window."""
    try:
        parent = float(parent_value)
    except (TypeError, ValueError):
        return None
    low, high = BINARY_SEARCH_BOUNDS["pva_wt_percent"]
    if parent >= high:
        return None
    target = 18.0 if parent < 18.0 else 20.0
    target = max(low, min(high, target))
    if _numeric_equivalent(parent, target):
        return None
    return target


def _is_rupture_mode(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"rupture", "break", "broken", "failed", "failure", "error1"} or "rupture" in text


def _round_has_all_rupture(out_dir: Path, round_idx: int) -> bool:
    results_path = out_dir / f"R{round_idx}_results_filled.csv"
    if not results_path.exists():
        alt_path = out_dir / "run_state_files" / f"R{round_idx}_results_filled.csv"
        if alt_path.exists():
            results_path = alt_path
    rows: list[dict[str, Any]] = []
    if results_path.exists():
        try:
            with open(results_path, encoding="utf-8-sig", newline="") as fh:
                rows = [row for row in csv.DictReader(fh) if (row.get("candidate_id") or "").strip()]
        except Exception:
            rows = []
    if not rows:
        return False

    notes = load_notes(out_dir, round_idx) or {}
    rupture_count = 0
    for row in rows:
        cid = (row.get("candidate_id") or "").strip()
        failure_type = row.get("failure_type") or row.get("failure_mode") or row.get("status")
        note_entry = notes.get(cid) if isinstance(notes, dict) else None
        note_codes = []
        if isinstance(note_entry, dict):
            note_codes = [str(code).lower() for code in note_entry.get("error_codes", []) or []]
        if _is_rupture_mode(failure_type) or "error1" in note_codes:
            rupture_count += 1
    return rupture_count == len(rows)


def _global_rupture_rescue_entries(
    round_idx: int,
    parent: Dict[str, Any],
    max_count: int,
) -> List[Dict[str, Any]]:
    entries: list[Dict[str, Any]] = []
    if max_count <= 0:
        return entries

    old_soak = _value_for_variable(parent, "post_soak_hours")
    new_soak = _post_soak_rescue_value(old_soak, old_soak)

    def _pva_rescue_changes(new_pva: float) -> list[dict[str, Any]]:
        changes = [_changed("pva_wt_percent", old_pva, new_pva, "raise_pva_after_all_rupture")]
        if old_soak not in (None, "") and new_soak is not None:
            changes.append(_changed("post_soak_hours", old_soak, new_soak, "five_min_soak_rescue_after_rupture"))
        return changes

    old_pva = _value_for_variable(parent, "pva_wt_percent")
    new_pva = _pva_rupture_rescue_value(old_pva)
    if old_pva not in (None, "") and new_pva is not None:
        entries.append(
            _entry(
                f"R{round_idx}-{len(entries) + 1:02d}",
                "failure_rescue_verification",
                parent,
                _pva_rescue_changes(new_pva),
                "All candidates in the source round ruptured; raise PVA into the 18-20 wt% rescue window and use 5 min post-gel treatment.",
                "If the higher-PVA/5 min candidate survives, treat insufficient matrix strength and over-soaking as coupled rupture drivers.",
            )
        )

        stronger_pva = 20.0 if new_pva < 20.0 else None
        if stronger_pva is not None and len(entries) < max_count:
            entries.append(
                _entry(
                    f"R{round_idx}-{len(entries) + 1:02d}",
                    "failure_rescue_verification",
                    parent,
                    [
                        (
                            _changed("pva_wt_percent", old_pva, stronger_pva, "raise_pva_stronger_after_all_rupture")
                            if change.get("variable") == "pva_wt_percent"
                            else change
                        )
                        for change in _pva_rescue_changes(stronger_pva)
                    ],
                    "All candidates in the source round ruptured; test the upper PVA rescue level (20 wt%) with 5 min post-gel treatment.",
                    "If 20 wt%/5 min survives better than 18 wt%/5 min, treat matrix strength as the dominant rupture limit.",
                )
            )

    if len(entries) >= max_count:
        return entries

    if old_soak not in (None, "") and new_soak is not None:
        entry = _entry(
            f"R{round_idx}-{len(entries) + 1:02d}",
            "failure_rescue_verification",
            parent,
            [_changed("post_soak_hours", old_soak, new_soak, "five_min_soak_rescue_after_rupture")],
            "All candidates in the source round ruptured; shorten the post-gel treatment to 5 min while keeping chemistry fixed.",
            "If the 5 min candidate survives, treat over-soaking/shrinkage treatment time as a rupture driver.",
        )
        if not _entry_is_duplicate(entry, entries):
            entries.append(entry)

    return entries[:max_count]


def _changed(variable: str, old_value: Any, new_value: Any, reason_code: str) -> Dict[str, Any]:
    return {
        "variable": variable,
        "old_value": old_value,
        "new_value": new_value,
        "change_magnitude": os.environ.get("PVA_CONSTRAINED_CHANGE_MAGNITUDE", "binary_midpoint"),
        "reason_code": reason_code,
    }


def _entry(
    next_id: str,
    design_type: str,
    parent: Dict[str, Any],
    variables_changed: List[Dict[str, Any]],
    rationale: str,
    expected: str,
) -> Dict[str, Any]:
    parent_id = parent.get("candidate_id", "")
    changed_names = [v.get("variable", "") for v in variables_changed]
    return {
        "next_id": next_id,
        "design_type": design_type,
        "parent_id": parent_id,
        "parent_summary": f"{parent_id}: constrained PVA parent formula",
        "variables_unchanged": [
            "PVA_main_polymer",
            "counterface",
            "load_N",
            "medium",
            "test_speed",
        ],
        "variables_changed": variables_changed,
        "changed_variables": variables_changed,
        "design_rationale": rationale,
        "expected_outcome": expected,
        "if_better": (
            f"If {next_id} improves COF/stability, the tested variable(s) "
            f"{changed_names or ['none']} are supported for the next local step."
        ),
        "if_worse": (
            f"If {next_id} worsens, revert {changed_names or ['the baseline condition']} "
            f"toward parent {parent_id} and avoid expanding the design space."
        ),
        "black_box_risk": 0 if design_type == "baseline_reproduction" else 1,
        "source_conclusion": f"Derived from previous-round parent {parent_id}.",
    }


def _normalize_factor_variable(variable: str) -> str:
    name = str(variable or "").strip().lower()
    if name in BINARY_SEARCH_BOUNDS:
        return name
    if name.endswith("pva_wt_percent"):
        return "pva_wt_percent"
    if name.endswith("post_soak_hours"):
        return "post_soak_hours"
    if name.endswith("freeze_thaw_cycles"):
        return "freeze_thaw_cycles"
    if "crosslinker" in name and name.endswith("wt_percent"):
        return "crosslinker_wt_percent"
    if "initiator" in name or "catalyst" in name:
        return "initiator_or_catalyst_wt_percent"
    if ".additive." in name and name.endswith("wt_percent"):
        return "primary_additive_wt_percent"
    return variable


def _verification_entries_from_memory(
    out_dir: Path,
    round_idx: int,
    parent: Dict[str, Any],
    max_count: int,
) -> List[Dict[str, Any]]:
    if max_count <= 0:
        return []
    try:
        from pva_work_flow.memory.failure_factor_memory import select_suspected_factors_for_parent
    except Exception:
        return []

    entries: List[Dict[str, Any]] = []
    seen_vars: set[str] = set()
    for factor in select_suspected_factors_for_parent(out_dir, parent, limit=max_count * 2):
        variable = _normalize_factor_variable(str(factor.get("variable") or ""))
        if not variable or variable in seen_vars:
            continue
        new_value = factor.get("suspected_value")
        old_value = _value_for_variable(parent, variable)
        if old_value in (None, "") or new_value in (None, ""):
            continue
        if _numeric_equivalent(old_value, new_value):
            continue
        failure_mode = str(factor.get("failure_mode") or "").lower()
        if variable == "post_soak_hours" and _is_rupture_mode(failure_mode):
            rescue_value = _post_soak_rescue_value(old_value, new_value)
            if rescue_value is None:
                continue
            rescue_entry = _entry(
                f"R{round_idx}-{len(entries) + 1:02d}",
                "failure_rescue_verification",
                parent,
                [_changed(variable, old_value, rescue_value, "five_min_soak_rescue_after_rupture")],
                (
                    f"Rescue-check suspected soak-related rupture {factor.get('factor_id')}: "
                    "use a 5 min post-gel treatment while keeping all chemistry fixed. "
                    "This avoids re-running a long-soak condition that already maps to rupture."
                ),
                (
                    "If the 5 min candidate survives with measurable COF, treat post-treatment "
                    "duration as a likely rupture driver. If it still ruptures, prioritize higher "
                    "PVA concentration or other matrix-strength changes."
                ),
            )
            rescue_entry["failure_factor_id"] = factor.get("factor_id")
            rescue_entry["source_conclusion"] = (
                f"Rescue mirror of failure-factor memory: status={factor.get('status')}, "
                f"evidence={factor.get('evidence')}"
            )
            if not _entry_is_duplicate(rescue_entry, entries):
                entries.append(rescue_entry)
            seen_vars.add(variable)
            if len(entries) >= max_count:
                break
            continue
        entry = _entry(
            f"R{round_idx}-{len(entries) + 1:02d}",
            "failure_factor_verification",
            parent,
            [_changed(variable, old_value, new_value, "verify_suspected_failure_factor")],
            (
                f"Verify suspected failure factor {factor.get('factor_id')}: "
                f"{factor.get('factor')}. Change only {variable}; keep all other parent "
                "conditions fixed to test whether this factor alone causes failure."
            ),
            (
                "If this single-factor verification ruptures or fails again, mark the factor "
                "confirmed and avoid it in later optimization. If it survives, weaken or disprove "
                "the suspected factor and investigate interaction effects."
            ),
        )
        entry["failure_factor_id"] = factor.get("factor_id")
        entry["source_conclusion"] = (
            f"Failure-factor memory: status={factor.get('status')}, "
            f"evidence={factor.get('evidence')}"
        )
        entries.append(entry)

        if variable == "post_soak_hours" and len(entries) < max_count:
            rescue_value = _post_soak_rescue_value(old_value, new_value)
            if rescue_value is not None and failure_mode in ("rupture", "break", "broken", "failed", "failure"):
                rescue_entry = _entry(
                    f"R{round_idx}-{len(entries) + 1:02d}",
                    "failure_rescue_verification",
                    parent,
                    [_changed(variable, old_value, rescue_value, "shorten_suspected_over_soak_rescue")],
                    (
                        f"Rescue-check suspected over-soak failure {factor.get('factor_id')}: "
                        f"shorten post_soak_hours below the parent value while keeping all chemistry fixed. "
                        "This tests whether reducing soak time prevents rupture instead of only reproducing "
                        "the long-soak failure."
                    ),
                    (
                        "If the shortened-soak candidate survives with measurable COF, treat long soaking "
                        "as a likely rupture driver and use the shorter soak window in later optimization. "
                        "If it still ruptures, investigate formulation strength or additive effects."
                    ),
                )
                rescue_entry["failure_factor_id"] = factor.get("factor_id")
                rescue_entry["source_conclusion"] = (
                    f"Rescue mirror of failure-factor memory: status={factor.get('status')}, "
                    f"evidence={factor.get('evidence')}"
                )
                if not _entry_is_duplicate(rescue_entry, entries):
                    entries.append(rescue_entry)
        seen_vars.add(variable)
        if len(entries) >= max_count:
            break
    return entries


def _choose_additive_variable(parent: Dict[str, Any]) -> str:
    additives = (parent.get("formulation") or {}).get("additives") or []
    for additive in additives:
        if isinstance(additive, dict) and additive.get("name") not in ("", None, "none"):
            return "primary_additive_wt_percent"
    return "post_soak_hours"


def _choose_network_or_process_variable(parent: Dict[str, Any]) -> str:
    for var in ("pva_wt_percent", "post_soak_hours", "freeze_thaw_cycles", "crosslinker_wt_percent"):
        val = _value_for_variable(parent, var)
        if val not in (None, "") and str(val) != "0" and str(val) != "0.0":
            return var
    return "pva_wt_percent"


def build_constrained_doe_skeleton(
    out_dir: Path,
    round_idx: int,
    audit: Dict[str, Any],
    requested_count: int = MAX_CONSTRAINED_CANDIDATES,
    allow_limited_exploration: bool = ENABLE_LIMITED_EXPLORATION_BY_DEFAULT,
    target_parent_id: str | None = None,
    parent_round_idx: int | None = None,
) -> Dict[str, Any]:
    """Build a small deterministic inheritance table for R2+.

    Default output is a set of binary-midpoint optimization rows:
    single-factor perturbations first, then local optimization if more rows are requested.
    limited_exploration is disabled by default to keep the project easier.
    """
    if round_idx <= 1:
        raise ValueError("Constrained DOE skeleton is only for R2+")

    source_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    parent_path = _parent_round_path(out_dir, round_idx, source_round)
    if not parent_path.exists():
        raise FileNotFoundError(f"Parent candidates not found: {parent_path}")

    parent_obj = read_json(parent_path)
    parents = parent_obj.get("candidates", []) or []
    by_id = _candidate_by_id(parents)

    diagnosis_best_ids: List[str] = []
    diagnosis_path = out_dir / f"R{source_round}_diagnosis.json"
    if diagnosis_path.exists():
        try:
            diagnosis_obj = read_json(diagnosis_path)
            diagnosis_best_ids = [
                str(cid)
                for cid in (diagnosis_obj.get("best_candidates") or [])
                if str(cid).strip()
            ]
        except Exception:
            diagnosis_best_ids = []

    # ---- Parent selection with mechanical-failure awareness ----
    # Sort candidates by COF (lowest first) so code-layer ranking supplements LLM audit.
    # Results CSV may live in out_dir/ or out_dir/run_state_files/ (newer layout).
    _results_path = out_dir / f"R{source_round}_results_filled.csv"
    if not _results_path.exists():
        _alt_path = out_dir / "run_state_files" / f"R{source_round}_results_filled.csv"
        if _alt_path.exists():
            _results_path = _alt_path
    _cof_by_id: Dict[str, float] = {}
    if _results_path.exists():
        import csv
        try:
            with open(_results_path, encoding="utf-8-sig", newline="") as _fh:
                for _row in csv.DictReader(_fh):
                    _cid = (_row.get("candidate_id") or "").strip()
                    _cof_str = _row.get("cof_steady_mean") or ""
                    if _cid and _cof_str:
                        try:
                            _cof_by_id[_cid] = float(_cof_str)
                        except ValueError:
                            pass
        except Exception:
            pass

    def _ranked_parents(ids, by_id, parents_list):
        """Yield parent dicts with manual integrity notes ahead of raw CVS.

        Manual observations can identify spikes, rupture, indentation, or
        surface damage that make a high-looking trace unreliable. Keep those
        parents behind clean samples, then use CVS within the same integrity
        class.
        """
        from pva_work_flow.wetlab.wetlab_outcomes import compute_cvs

        # Resolve error codes from experiment_notes
        _notes_e = load_notes(out_dir, round_idx - 1)
        _error_codes_by_id: dict[str, list[str]] = {}
        if _notes_e:
            for _cid, _entry in _notes_e.items():
                if isinstance(_entry, dict) and _entry.get("error_codes"):
                    _error_codes_by_id[_cid] = [str(e) for e in _entry["error_codes"]]

        # Compute CVS for each parent that has COF data.
        _cvs_by_id: dict[str, dict[str, Any]] = {}
        for cid in ids:
            if cid not in by_id:
                continue
            # Build a result-like row from the filled CSV if available
            row: dict[str, Any] = {"candidate_id": cid}
            cof = _cof_by_id.get(cid)
            if cof is not None:
                row["cof_steady_mean"] = cof
            # Enrich with other columns from results CSV
            if _results_path.exists():
                try:
                    with open(_results_path, encoding="utf-8-sig", newline="") as _fh:
                        for _row in csv.DictReader(_fh):
                            if (_row.get("candidate_id") or "").strip() == cid:
                                row = _row
                                break
                except Exception:
                    pass
            if row.get("cof_steady_mean") is not None:
                ec = _error_codes_by_id.get(cid)
                _cvs_by_id[cid] = compute_cvs(row, error_codes=ec if ec else None)
            else:
                _cvs_by_id[cid] = {"cvs": 0.0, "i_multiplier": 0.0}  # no COF -> bottom

        # Sort by integrity first, then CVS descending.
        sorted_ids = sorted(
            [cid for cid in ids if cid in by_id],
            key=lambda cid: (
                float(_cvs_by_id.get(cid, {}).get("i_multiplier", 0.0)) < 1.0,
                -float(_cvs_by_id.get(cid, {}).get("cvs", 0.0)),
            ),
        )
        for cid in sorted_ids:
            yield by_id[cid]

        # Fallback: any remaining parents not yet yielded
        yielded_ids = set(sorted_ids)
        for p in parents_list:
            pid = p.get("candidate_id", "")
            if pid and pid not in yielded_ids:
                yield p

    if target_parent_id:
        if target_parent_id not in by_id:
            raise RuntimeError(
                f"target_parent_id={target_parent_id} not found in R{source_round}_candidates.json"
            )
        best_parent = by_id[target_parent_id]
    else:
        ranked_ids: List[str] = []
        for cid in diagnosis_best_ids + _audit_best_ids(audit):
            if cid not in ranked_ids:
                ranked_ids.append(cid)
        if not ranked_ids:
            ranked_ids = [p.get("candidate_id", "") for p in parents if p.get("candidate_id")]
        _best_iter = _ranked_parents(ranked_ids, by_id, parents)
        best_parent = next(_best_iter, parents[0] if parents else {})
    failed_parent = _first_existing(_audit_failed_ids(audit), by_id, parents)

    _notes = load_notes(out_dir, source_round)
    if _notes:
        _best_cid = best_parent.get("candidate_id", "")
        _best_entry = _notes.get(_best_cid) if isinstance(_notes, dict) else None
        if _best_entry and isinstance(_best_entry, dict) and _best_entry.get("error_codes"):
            print(
                f"[DOE] WARNING: best parent {_best_cid} has manual error codes: "
                f"{_best_entry['error_codes']}. Check experiment notes."
            )

    n = max(1, min(int(requested_count or MAX_CONSTRAINED_CANDIDATES), MAX_CONSTRAINED_CANDIDATES))

    # ---- Parent-state-aware first perturbation direction ----
    # Read parent COF to decide whether to prioritise lubrication or reinforcement.
    _parent_cid = best_parent.get("candidate_id", "")
    _parent_cof = _cof_by_id.get(_parent_cid)
    _parent_friction = ""  # from results CSV if available
    _parent_stick_slip = None
    if _parent_cid and _results_path.exists():
        try:
            with open(_results_path, encoding="utf-8-sig", newline="") as _fh:
                for _row in csv.DictReader(_fh):
                    if (_row.get("candidate_id") or "").strip() == _parent_cid:
                        _parent_friction = (_row.get("friction_pattern") or "").strip().lower()
                        try:
                            _parent_stick_slip = float(_row.get("stick_slip_score", ""))
                        except (ValueError, TypeError):
                            pass
                        break
        except Exception:
            pass

    # Heuristic: parent with low COF but irregular/asymmetric friction or high stick-slip
    # needs mechanical reinforcement, not further softening.
    _needs_reinforcement = (
        _parent_cof is not None
        and _parent_cof < 0.02
        and (
            _parent_friction in ("irregular", "asymmetric")
            or (_parent_stick_slip is not None and _parent_stick_slip > 0.3)
        )
    )

    table: List[Dict[str, Any]] = []
    _all_rupture_source = _round_has_all_rupture(out_dir, source_round)
    if _all_rupture_source:
        rupture_entries = _global_rupture_rescue_entries(round_idx, best_parent, n)
        for entry in rupture_entries:
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
        if rupture_entries:
            print(
                f"[DOE] Source R{source_round} was all-rupture; prioritized "
                f"{len(rupture_entries)} PVA/5min-soak rescue entries."
            )

    verification_entries = _verification_entries_from_memory(out_dir, round_idx, best_parent, n - len(table))
    if verification_entries:
        for entry in verification_entries:
            entry["next_id"] = f"R{round_idx}-{len(table) + 1:02d}"
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
        print(f"[DOE] Prioritized {len(verification_entries)} failure-factor verification entries from memory.")

    allow_generic_fallbacks = not _all_rupture_source

    if allow_generic_fallbacks and len(table) < n:
        if _needs_reinforcement:
            # Prioritise matrix strength; GA/HCl can be post-gel shrinkage treatment,
            # so crosslinker loading should not be the default rupture fix.
            variable = "pva_wt_percent"
            old = _value_for_variable(best_parent, variable)
            if old in (None, "") or str(old) in ("0", "0.0"):
                variable = "freeze_thaw_cycles"
                old = _value_for_variable(best_parent, variable)
            direction = "increase"
            reason_code = "increase_mechanical_integrity"
            rationale = (
                f"Increase PVA matrix strength rather than GA/HCl loading "
                f"(parent COF={_parent_cof:.4f} is already low but friction is {_parent_friction})."
            )
        else:
            variable = "pva_wt_percent"
            old = _value_for_variable(best_parent, variable)
            direction = "decrease"
            reason_code = "reduce_contact_stiffness_or_increase_hydration"
            rationale = (
                "Test a lower PVA concentration around the best parent "
                "instead of spending a slot on exact reproduction."
            )

        new = _small_step_for_variable(variable, old, direction)
        if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
            entry = _entry(
                f"R{round_idx}-{len(table) + 1:02d}",
                "single_factor_perturbation",
                best_parent,
                [_changed(variable, old, new, reason_code)],
                rationale,
                "COF decreases if lower contact stiffness/higher hydration helps, while gel integrity remains acceptable.",
            )
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
            else:
                print(f"[DOE] Entry 1 fallback ({variable}: {old}->{new}) duplicates existing entry; skipped.")

    if allow_generic_fallbacks and len(table) < n:
        variable = _choose_network_or_process_variable(best_parent)
        old = _value_for_variable(best_parent, variable)
        direction = "decrease" if variable in ("crosslinker_wt_percent", "pva_wt_percent") else "increase"
        new = _small_step_for_variable(variable, old, direction)
        # No-change guard: try opposite direction if values are equivalent
        if old not in (None, "") and _numeric_equivalent(old, new):
            new = _small_step_for_variable(variable, old, _opposite_direction(direction))
        if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
            entry = _entry(
                f"R{round_idx}-{len(table) + 1:02d}",
                "single_factor_perturbation",
                best_parent,
                [_changed(variable, old, new, "reduce_network_density_or_contact_stiffness")],
                f"Test one small {direction} step in {variable} around the best parent to isolate its effect on friction stability.",
                "COF or friction stability improves without losing gel integrity.",
            )
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
            else:
                print(f"[DOE] Entry 2 ({variable}: {old}->{new}) duplicates existing entry; skipped.")

    if allow_generic_fallbacks and len(table) < n:
        variable = _choose_additive_variable(best_parent)
        old = _value_for_variable(best_parent, variable)
        new = _small_step_for_variable(variable, old, "increase") if old not in (None, "") else old
        # No-change guard
        if old not in (None, "") and _numeric_equivalent(old, new):
            new = _small_step_for_variable(variable, old, "decrease")
        if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
            entry = _entry(
                f"R{round_idx}-{len(table) + 1:02d}",
                "single_factor_perturbation",
                best_parent,
                [_changed(variable, old, new, "test_hydration_or_post_treatment_effect")],
                "Change exactly one hydration/lubrication-related factor while holding the parent chemistry fixed.",
                "The result clarifies whether the parent low-friction behavior is controlled by hydration/lubrication support.",
            )
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
            else:
                print(f"[DOE] Entry 3 ({variable}: {old}->{new}) duplicates existing entry; skipped.")

    if allow_generic_fallbacks and len(table) < n:
        variable = "post_soak_hours"
        old = _value_for_variable(best_parent, variable)
        new = _small_step_for_variable(variable, old, "increase") if old not in (None, "") else old
        if old not in (None, "") and _numeric_equivalent(old, new):
            new = _small_step_for_variable(variable, old, "decrease")
        if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
            # Fallback: if post_soak still no good, try freeze_thaw
            if _numeric_equivalent(old, new):
                variable = "freeze_thaw_cycles"
                old = _value_for_variable(best_parent, variable)
                new = _small_step_for_variable(variable, old, "increase") if old not in (None, "") else old
            entry = _entry(
                f"R{round_idx}-{len(table) + 1:02d}",
                "local_optimization",
                best_parent,
                [_changed(variable, old, new, "small_process_optimization")],
                "Make one conservative process adjustment around the best parent without introducing new materials.",
                "Friction stability improves while the material remains experimentally comparable to the parent.",
            )
            if not _entry_is_duplicate(entry, table):
                table.append(entry)
            else:
                print(f"[DOE] Entry 4 ({variable}: {old}->{new}) duplicates existing entry; skipped.")

    # ---- Entry 5: second single_factor_perturbation (fallback chain, expanded) ----
    if allow_generic_fallbacks and len(table) < n:
        _entry5_vars = (
            "initiator_or_catalyst_wt_percent",
            "crosslinker_wt_percent",
            "primary_additive_wt_percent",
            "pva_wt_percent",
            "post_soak_hours",
            "freeze_thaw_cycles",
        )
        for variable in _entry5_vars:
            old = _value_for_variable(best_parent, variable)
            if old in (None, "") or str(old) == "0" or str(old) == "0.0":
                continue
            direction = "increase" if len(table) % 2 == 0 else "decrease"
            new = _small_step_for_variable(variable, old, direction)
            if _numeric_equivalent(old, new):
                new = _small_step_for_variable(variable, old, _opposite_direction(direction))
            if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
                entry = _entry(
                    f"R{round_idx}-{len(table) + 1:02d}",
                    "single_factor_perturbation",
                    best_parent,
                    [_changed(variable, old, new, "third_factor_perturbation")],
                    f"Isolate the effect of {variable} while holding other formulation variables fixed.",
                    "Provides a third independent single-factor data point.",
                )
                if not _entry_is_duplicate(entry, table):
                    table.append(entry)
                    break
                else:
                    print(f"[DOE] Entry 5 ({variable}: {old}->{new}) duplicates existing entry; trying next lever...")
        else:
            print("[DOE] Entry 5: all fallback variables exhausted or duplicate; skipped.")

    # ---- Entry 6: second local_optimization (fallback chain, expanded) ----
    if allow_generic_fallbacks and len(table) < n:
        _entry6_vars = (
            "post_soak_hours",
            "freeze_thaw_cycles",
            "pva_wt_percent",
            "crosslinker_wt_percent",
            "initiator_or_catalyst_wt_percent",
        )
        for variable in _entry6_vars:
            old = _value_for_variable(best_parent, variable)
            if old in (None, "") or str(old) == "0" or str(old) == "0.0":
                continue
            direction = "decrease"
            new = _small_step_for_variable(variable, old, direction)
            if _numeric_equivalent(old, new):
                new = _small_step_for_variable(variable, old, "increase")
            if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
                entry = _entry(
                    f"R{round_idx}-{len(table) + 1:02d}",
                    "local_optimization",
                    best_parent,
                    [_changed(variable, old, new, "secondary_process_optimization")],
                    f"Second conservative process tweak ({variable}) to confirm direction.",
                    "Provides a second local data point without adding new materials.",
                )
                if not _entry_is_duplicate(entry, table):
                    table.append(entry)
                    break
                else:
                    print(f"[DOE] Entry 6 ({variable}: {old}->{new}) duplicates existing entry; trying next lever...")
        else:
            print("[DOE] Entry 6: all fallback variables exhausted or duplicate; skipped.")

    if allow_limited_exploration and len(table) < n:
        exploration_parent = deepcopy(failed_parent)
        table.append(
            _entry(
                f"R{round_idx}-{len(table) + 1:02d}",
                "limited_exploration",
                exploration_parent,
                [],
                "Reserved for a human-approved limited exploration; disabled by default.",
                "Only used when local PVA-system optimization is exhausted.",
            )
        )

    type_counts: Dict[str, int] = {}
    for item in table:
        dt = item.get("design_type", "unknown")
        type_counts[dt] = type_counts.get(dt, 0) + 1

    return {
        "design_theme": "Constrained binary-midpoint PVA DOE generated by code; LLM may explain but must not alter skeleton fields.",
        "optimization_scope": "single_parent_tree",
        "target_parent_id": best_parent.get("candidate_id", ""),
        "parent_round_idx": source_round,
        "total_count": len(table),
        "type_counts": type_counts,
        "limited_exploration_enabled": bool(allow_limited_exploration),
        "questions_this_round_answers": [
            "Which local variables around the selected parent affect COF/stability?",
            "Can the best parent be improved without spending a slot on exact reproduction?",
            "Can optimization proceed without introducing a new material system?",
        ],
        "inheritance_table": table,
        "skeleton_source": "code_constrained_doe",
    }
