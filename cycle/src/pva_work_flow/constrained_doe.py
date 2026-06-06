"""Deterministic constrained DOE skeleton generation.

The goal is to lower task difficulty for 14B-class models: code creates the
lineage skeleton, while the LLM only explains and completes it.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from .utils import read_json
from .experiment_notes import is_candidate_mechanically_failed, load_notes


MAX_CONSTRAINED_CANDIDATES = 6
ENABLE_LIMITED_EXPLORATION_BY_DEFAULT = False


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
        return max(0.0, round(number * 0.75, 4))
    if direction == "increase":
        return round(number * 1.25, 4)
    return number


def _small_step_for_variable(variable: str, value: Any, direction: str) -> Any:
    if variable == "freeze_thaw_cycles":
        try:
            cycles = int(round(float(value)))
        except (TypeError, ValueError):
            return value
        delta = -1 if direction == "decrease" else 1
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


def _changed(variable: str, old_value: Any, new_value: Any, reason_code: str) -> Dict[str, Any]:
    return {
        "variable": variable,
        "old_value": old_value,
        "new_value": new_value,
        "change_magnitude": "small",
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


def _choose_additive_variable(parent: Dict[str, Any]) -> str:
    additives = (parent.get("formulation") or {}).get("additives") or []
    for additive in additives:
        if isinstance(additive, dict) and additive.get("name") not in ("", None, "none"):
            return "primary_additive_wt_percent"
    return "post_soak_hours"


def _choose_network_or_process_variable(parent: Dict[str, Any]) -> str:
    for var in ("crosslinker_wt_percent", "freeze_thaw_cycles", "post_soak_hours", "pva_wt_percent"):
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

    Default output is a set of small-step optimization rows:
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
    # ---- Parent selection with mechanical-failure awareness ----
    # Sort candidates by COF (lowest first) so code-layer ranking supplements LLM audit.
    _results_path = out_dir / f"R{source_round}_results_filled.csv"
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
        """Yield parent dicts in order: non-failed first (by COF), then the rest."""
        failed_ids = set()
        for cid in ids:
            if is_candidate_mechanically_failed(by_id.get(cid, {}), out_dir, round_idx - 1):
                failed_ids.add(cid)
        # non-failed, sorted by COF ascending
        ok_ids = [cid for cid in ids if cid in by_id and cid not in failed_ids]
        ok_ids.sort(key=lambda cid: _cof_by_id.get(cid, float("inf")))
        for cid in ok_ids:
            yield by_id[cid]
        # then failed ones
        for cid in ids:
            if cid in by_id and cid in failed_ids:
                yield by_id[cid]
        # fallback: any remaining parents not yet yielded
        yielded_ids = set(ok_ids) | {cid for cid in ids if cid in failed_ids}
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
        _best_iter = _ranked_parents(_audit_best_ids(audit), by_id, parents)
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

    table: List[Dict[str, Any]] = []
    if len(table) < n:
        variable = "pva_wt_percent"
        old = _value_for_variable(best_parent, variable)
        new = _small_step_for_variable(variable, old, "decrease")
        if old not in (None, "") and new is not None and not _numeric_equivalent(old, new):
            table.append(
                _entry(
                    f"R{round_idx}-{len(table) + 1:02d}",
                    "single_factor_perturbation",
                    best_parent,
                    [_changed(variable, old, new, "reduce_contact_stiffness_or_increase_hydration")],
                    "Test a lower PVA concentration around the best parent instead of spending a slot on exact reproduction.",
                    "COF decreases if lower contact stiffness/higher hydration helps, while gel integrity remains acceptable.",
                )
            )

    if len(table) < n:
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

    if len(table) < n:
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

    if len(table) < n:
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
    if len(table) < n:
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
    if len(table) < n:
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
        "design_theme": "Constrained small-step PVA DOE generated by code; LLM may explain but must not alter skeleton fields.",
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
