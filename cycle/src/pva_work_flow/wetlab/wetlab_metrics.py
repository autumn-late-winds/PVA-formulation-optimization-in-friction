"""Wet-lab metrics for cycle optimization runs.

The script evaluates completed R*_results_filled.csv files against the
candidate tree. It is intentionally read-only: it writes only metric reports.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from pva_work_flow.tree.chain_search import resolve_chain_parent
from pva_work_flow.artifacts.io_artifacts import aggregate_cof_from_row
from pva_work_flow.core.utils import _to_float_or_none, load_allowed_materials


CRITICAL_FAILURES = {
    "no_gel",
    "not_gel",
    "gel_failed",
    "fracture",
    "rupture",
    "delamination",
}
RAG_EVIDENCE_FIELDS = (
    "rag_evidence",
    "rag_evidence_used",
    "literature_evidence",
    "literature_evidence_used",
    "formulation_rag_cases",
)
BASE_MATERIALS = {
    "di water",
    "water",
    "pva",
    "pva (polyvinyl alcohol)",
    "polyvinyl alcohol",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _round_from_path(path: Path) -> int | None:
    try:
        return int(path.stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return None


def _load_candidates(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    candidates: dict[str, dict[str, Any]] = {}
    rounds: dict[str, int] = {}
    for path in sorted(run_dir.glob("R*_candidates.json")):
        round_idx = _round_from_path(path)
        if round_idx is None:
            continue
        data = _read_json(path)
        for candidate in data.get("candidates", []) or []:
            cid = candidate.get("candidate_id")
            if not cid:
                continue
            candidates[cid] = candidate
            rounds[cid] = round_idx
    return candidates, rounds


def _load_results(run_dir: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(run_dir.glob("R*_results_filled.csv")):
        round_idx = _round_from_path(path)
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                cid = (row.get("candidate_id") or "").strip()
                if not cid:
                    continue
                cof, cof_std = aggregate_cof_from_row(row)
                row["_cof"] = cof
                row["_cof_std"] = cof_std
                row["_round"] = round_idx
                results[cid] = row
    return results


def _failure_type(row: dict[str, Any]) -> str:
    return str(row.get("failure_type") or "").strip().lower()


def _notes_have_error(row: dict[str, Any]) -> bool:
    notes = str(row.get("notes") or "").upper()
    raw = str(row.get("cof_steady_mean") or "").upper()
    return "ERROR1" in notes or "ERROR2" in notes or raw in {"ERROR1", "ERROR2"}


def _has_failure(row: dict[str, Any]) -> bool:
    failure = _failure_type(row)
    return failure not in {"", "none", "na", "n/a", "null"}


def _fabrication_success(row: dict[str, Any]) -> bool:
    if row.get("_cof") is None or _notes_have_error(row):
        return False
    return _failure_type(row) not in CRITICAL_FAILURES


def _safe_rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percent_improvement(root_cof: float | None, target_cof: float | None) -> float | None:
    if root_cof is None or target_cof is None or root_cof == 0:
        return None
    return (root_cof - target_cof) / root_cof


def _under_root(candidate: dict[str, Any], root_id: str) -> bool:
    return candidate.get("candidate_id") == root_id or candidate.get("root_candidate_id") == root_id


def _accepted_chain_path(chain_state: dict[str, Any]) -> list[str]:
    path = [chain_state["root_id"]]
    for step in chain_state.get("trace", []) or []:
        if step.get("decision") == "accept" and step.get("best_child_id"):
            path.append(step["best_child_id"])
        else:
            break
    return path


def _steps_to_condition(path: list[str], results: dict[str, dict[str, Any]], predicate) -> int | None:
    for step_idx, cid in enumerate(path):
        row = results.get(cid)
        if row and predicate(row):
            return step_idx
    return None


def _rate_for_optional_numeric(
    rows: list[dict[str, Any]],
    column: str,
    predicate,
) -> tuple[float | None, str | None]:
    values = [_to_float_or_none(row.get(column)) for row in rows if column in row]
    values = [value for value in values if value is not None]
    if not values:
        return None, f"column not available or empty: {column}"
    return sum(1 for value in values if predicate(value)) / len(values), None


def _mean_optional_numeric(rows: list[dict[str, Any]], column: str) -> tuple[float | None, str | None]:
    values = [_to_float_or_none(row.get(column)) for row in rows if column in row]
    values = [value for value in values if value is not None]
    if not values:
        return None, f"column not available or empty: {column}"
    return sum(values) / len(values), None


def _rag_supported_rate(candidates: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    present_fields = [
        field
        for field in RAG_EVIDENCE_FIELDS
        if any(field in candidate for candidate in candidates)
    ]
    if not present_fields:
        return None, "no per-candidate RAG evidence field found"

    def has_evidence(candidate: dict[str, Any]) -> bool:
        for field in present_fields:
            value = candidate.get(field)
            if isinstance(value, list) and value:
                return True
            if isinstance(value, dict) and value:
                return True
            if isinstance(value, str) and value.strip():
                return True
        return False

    return sum(1 for candidate in candidates if has_evidence(candidate)) / len(candidates), None


def _material_names(candidate: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for material in candidate.get("materials") or []:
        if isinstance(material, dict):
            name = str(material.get("name") or "").strip().lower()
            if name:
                names.add(name)
    formulation = candidate.get("formulation") or {}
    for additive in formulation.get("additives") or []:
        if isinstance(additive, dict):
            name = str(additive.get("name") or "").strip().lower()
            if name:
                names.add(name)
    for key in ("crosslinker", "initiator", "catalyst", "nanofiller", "plasticizer"):
        value = formulation.get(key)
        if isinstance(value, dict):
            name = str(value.get("name") or "").strip().lower()
            if name:
                names.add(name)
        elif isinstance(value, str) and value.strip():
            names.add(value.strip().lower())
    return {name for name in names if name not in {"none", "n/a", "na", "null"}}


def _inventory_metrics(
    candidates: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    root_id: str,
    inventory_csv: Path | None,
) -> dict[str, Any]:
    if inventory_csv is None:
        inventory_csv = Path(__file__).resolve().parents[3] / "materials" / "materials_en.csv"
    allowed_names, _ = load_allowed_materials(inventory_csv)
    allowed = {name.strip().lower() for name in allowed_names if name.strip()}
    if not allowed:
        return {
            "inventory_csv": str(inventory_csv),
            "inventory_candidate_count": 0,
            "inventory_hit_rate": None,
            "new_material_rate": None,
            "purchase_blocked_rate": None,
            "inventory_constrained_success_rate": None,
            "inventory_reason": "inventory CSV missing or empty",
        }

    parent_materials = {
        c.get("candidate_id"): _material_names(c)
        for c in candidates
        if c.get("candidate_id")
    }
    rows = []
    for candidate in candidates:
        cid = candidate.get("candidate_id")
        if not cid or cid == root_id:
            continue
        materials = _material_names(candidate)
        non_base_materials = {name for name in materials if name not in BASE_MATERIALS}
        missing = sorted(name for name in non_base_materials if name not in allowed)
        parent_id = candidate.get("parent_candidate_id")
        parent_names = parent_materials.get(parent_id, set())
        introduced = sorted(
            name for name in non_base_materials
            if parent_names and name not in parent_names and name not in BASE_MATERIALS
        )
        row = {
            "candidate_id": cid,
            "inventory_ok": not missing,
            "missing_inventory_materials": missing,
            "new_materials_vs_parent": introduced,
            "purchase_blocked": bool(missing),
            "measured": cid in results and results[cid].get("_cof") is not None,
            "fabrication_success": cid in results and _fabrication_success(results[cid]),
        }
        rows.append(row)

    n = len(rows)
    inventory_ok = [row for row in rows if row["inventory_ok"]]
    new_material = [row for row in rows if row["new_materials_vs_parent"]]
    blocked = [row for row in rows if row["purchase_blocked"]]
    return {
        "inventory_csv": str(inventory_csv),
        "inventory_candidate_count": n,
        "inventory_hit_rate": _safe_rate(len(inventory_ok), n),
        "new_material_rate": _safe_rate(len(new_material), n),
        "purchase_blocked_rate": _safe_rate(len(blocked), n),
        "inventory_constrained_success_rate": _safe_rate(
            sum(1 for row in inventory_ok if row["fabrication_success"]),
            len(inventory_ok),
        ),
        "inventory_blocked_candidates": blocked[:20],
        "inventory_new_material_candidates": new_material[:20],
        "inventory_reason": None,
    }


def compute_wetlab_metrics(
    run_dir: Path,
    root_id: str | None = None,
    chain_accept_delta: float = -1e-6,
    cof_delta_threshold: float = 0.005,
    success_cof_max: float = 0.02,
    cof_target_max: float = 0.03,
    modulus_min: float = 1.5,
    modulus_max: float = 2.5,
    stable_proportion_min: float = 0.6,
    stick_slip_max: float = 0.2,
    inventory_csv: Path | None = None,
) -> dict[str, Any]:
    candidates, rounds = _load_candidates(run_dir)
    results = _load_results(run_dir)
    if not candidates:
        raise RuntimeError(f"No R*_candidates.json files found in {run_dir}")
    if not results:
        raise RuntimeError(f"No R*_results_filled.csv rows found in {run_dir}")

    chain_state = resolve_chain_parent(run_dir, root_id=root_id, accept_delta=chain_accept_delta)
    root_id = chain_state["root_id"]
    chain_path = _accepted_chain_path(chain_state)

    root_candidates = [c for c in candidates.values() if _under_root(c, root_id)]
    root_candidate_ids = {c["candidate_id"] for c in root_candidates}
    root_results = [
        row for cid, row in results.items()
        if cid in root_candidate_ids and row.get("_cof") is not None
    ]
    sample_rows = [
        row for row in root_results
        if (row.get("candidate_id") or "") != root_id
    ]

    root_row = results.get(root_id)
    root_cof = root_row.get("_cof") if root_row else None
    final_id = chain_state.get("current_parent_id")
    final_row = results.get(final_id) if final_id else None
    final_cof = final_row.get("_cof") if final_row else None

    cofs = [(row["candidate_id"], row["_cof"]) for row in root_results if row.get("_cof") is not None]
    best_id, best_cof = min(cofs, key=lambda item: item[1]) if cofs else (None, None)

    step_hits: list[dict[str, Any]] = []
    for candidate in root_candidates:
        cid = candidate.get("candidate_id")
        parent_id = candidate.get("parent_candidate_id")
        if not cid or not parent_id or cid not in results or parent_id not in results:
            continue
        child_cof = results[cid].get("_cof")
        parent_cof = results[parent_id].get("_cof")
        if child_cof is None or parent_cof is None:
            continue
        delta = child_cof - parent_cof
        step_hits.append({
            "candidate_id": cid,
            "parent_id": parent_id,
            "delta_cof": delta,
            "hit": delta <= -cof_delta_threshold,
        })

    modulus_values = [_to_float_or_none(row.get("compression_modulus_MPa")) for row in sample_rows]
    modulus_values = [value for value in modulus_values if value is not None]
    modulus_in_target_rate = _safe_rate(
        sum(1 for value in modulus_values if modulus_min <= value <= modulus_max),
        len(modulus_values),
    )

    retention_values: list[float] = []
    for candidate in root_candidates:
        cid = candidate.get("candidate_id")
        parent_id = candidate.get("parent_candidate_id")
        if not cid or not parent_id or cid not in results or parent_id not in results:
            continue
        child_modulus = _to_float_or_none(results[cid].get("compression_modulus_MPa"))
        parent_modulus = _to_float_or_none(results[parent_id].get("compression_modulus_MPa"))
        if child_modulus is not None and parent_modulus not in (None, 0):
            retention_values.append(child_modulus / parent_modulus)

    stable_rate, stable_reason = _rate_for_optional_numeric(
        sample_rows,
        "stable_proportion",
        lambda value: value >= stable_proportion_min,
    )
    stable_mean, stable_mean_reason = _mean_optional_numeric(sample_rows, "stable_proportion")
    stick_slip_rate, stick_slip_reason = _rate_for_optional_numeric(
        sample_rows,
        "stick_slip_score",
        lambda value: value <= stick_slip_max,
    )
    stick_slip_mean, stick_slip_mean_reason = _mean_optional_numeric(sample_rows, "stick_slip_score")

    cof_target_rate = _safe_rate(
        sum(1 for row in sample_rows if row.get("_cof") is not None and row["_cof"] <= cof_target_max),
        len(sample_rows),
    )
    strict_success_rate = None
    strict_success_reason = None
    if stable_rate is None or stick_slip_rate is None:
        strict_success_reason = "strict success needs stable_proportion and stick_slip_score columns"
    else:
        strict_success_rate = _safe_rate(
            sum(
                1 for row in sample_rows
                if row.get("_cof") is not None
                and row["_cof"] <= success_cof_max
                and modulus_min <= (_to_float_or_none(row.get("compression_modulus_MPa")) or -1) <= modulus_max
                and (_to_float_or_none(row.get("stable_proportion")) or -1) >= stable_proportion_min
                and (_to_float_or_none(row.get("stick_slip_score")) or 999) <= stick_slip_max
                and not _has_failure(row)
            ),
            len(sample_rows),
        )

    rag_rate, rag_reason = _rag_supported_rate(root_candidates)
    inventory = _inventory_metrics(root_candidates, results, root_id, inventory_csv)
    best_step = None
    if best_id in chain_path:
        best_step = chain_path.index(best_id)

    metrics = {
        "run_dir": str(run_dir),
        "root_id": root_id,
        "thresholds": {
            "cof_delta_threshold": cof_delta_threshold,
            "success_cof_max": success_cof_max,
            "cof_target_max": cof_target_max,
            "modulus_min": modulus_min,
            "modulus_max": modulus_max,
            "stable_proportion_min": stable_proportion_min,
            "stick_slip_max": stick_slip_max,
            "chain_accept_delta": chain_accept_delta,
        },
        "sample_counts": {
            "candidate_count_under_root": len(root_candidates),
            "measured_count_under_root": len(root_results),
            "measured_descendant_count": len(sample_rows),
        },
        "chain_metrics": {
            "chain_path": chain_path,
            "current_parent_id": final_id,
            "root_cof": root_cof,
            "final_cof": final_cof,
            "best_candidate_id_under_root": best_id,
            "best_cof_under_root": best_cof,
            "endpoint_improvement": _percent_improvement(root_cof, final_cof),
            "best_improvement": _percent_improvement(root_cof, best_cof),
            "steps_to_best_on_chain": best_step,
            "steps_to_10pct_improvement": _steps_to_condition(
                chain_path,
                results,
                lambda row: _percent_improvement(root_cof, row.get("_cof")) is not None
                and _percent_improvement(root_cof, row.get("_cof")) >= 0.10,
            ),
            "steps_to_cof_target": _steps_to_condition(
                chain_path,
                results,
                lambda row: row.get("_cof") is not None and row["_cof"] <= cof_target_max,
            ),
            "chain_trace": chain_state.get("trace", []),
        },
        "step_metrics": {
            "single_step_hit_rate": _safe_rate(sum(1 for item in step_hits if item["hit"]), len(step_hits)),
            "single_step_evaluable_count": len(step_hits),
            "single_step_hits": step_hits,
        },
        "wetlab_quality_metrics": {
            "cof_target_rate": cof_target_rate,
            "strict_success_rate": strict_success_rate,
            "strict_success_reason": strict_success_reason,
            "fabrication_success_rate": _safe_rate(
                sum(1 for row in sample_rows if _fabrication_success(row)),
                len(sample_rows),
            ),
            "failure_rate": _safe_rate(sum(1 for row in sample_rows if _has_failure(row)), len(sample_rows)),
            "modulus_in_target_rate": modulus_in_target_rate,
            "mean_mechanical_retention_vs_parent": (
                sum(retention_values) / len(retention_values) if retention_values else None
            ),
            "stable_friction_rate": stable_rate,
            "stable_friction_reason": stable_reason,
            "mean_stable_proportion": stable_mean,
            "mean_stable_proportion_reason": stable_mean_reason,
            "stick_slip_pass_rate": stick_slip_rate,
            "stick_slip_reason": stick_slip_reason,
            "mean_stick_slip_score": stick_slip_mean,
            "mean_stick_slip_score_reason": stick_slip_mean_reason,
            "rag_supported_rate": rag_rate,
            "rag_supported_reason": rag_reason,
            "inventory_hit_rate": inventory["inventory_hit_rate"],
            "new_material_rate": inventory["new_material_rate"],
            "purchase_blocked_rate": inventory["purchase_blocked_rate"],
            "inventory_constrained_success_rate": inventory["inventory_constrained_success_rate"],
            "inventory_reason": inventory["inventory_reason"],
        },
        "inventory_metrics": inventory,
        "round_by_candidate": {cid: rounds.get(cid) for cid in sorted(root_candidate_ids)},
    }
    return metrics


def _fmt(value: Any) -> str:
    if value is None:
        return "not_available"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def write_report(metrics: dict[str, Any], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    chain = metrics["chain_metrics"]
    step = metrics["step_metrics"]
    quality = metrics["wetlab_quality_metrics"]
    counts = metrics["sample_counts"]
    lines = [
        "# Wet-lab Metrics",
        "",
        f"- run_dir: `{metrics['run_dir']}`",
        f"- root_id: `{metrics['root_id']}`",
        f"- measured under root: {counts['measured_count_under_root']}",
        f"- measured descendants: {counts['measured_descendant_count']}",
        "",
        "## Chain Optimization",
        "",
        f"- chain path: {' -> '.join(chain['chain_path'])}",
        f"- root COF: {_fmt(chain['root_cof'])}",
        f"- final COF: {_fmt(chain['final_cof'])}",
        f"- endpoint improvement: {_fmt(chain['endpoint_improvement'])}",
        f"- best candidate under root: `{chain['best_candidate_id_under_root']}`",
        f"- best COF under root: {_fmt(chain['best_cof_under_root'])}",
        f"- best improvement: {_fmt(chain['best_improvement'])}",
        f"- steps to best on chain: {_fmt(chain['steps_to_best_on_chain'])}",
        f"- steps to 10% improvement: {_fmt(chain['steps_to_10pct_improvement'])}",
        f"- steps to COF target: {_fmt(chain['steps_to_cof_target'])}",
        "",
        "## Step Quality",
        "",
        f"- single-step hit rate: {_fmt(step['single_step_hit_rate'])}",
        f"- single-step evaluable count: {step['single_step_evaluable_count']}",
        "",
        "## Wet-lab Quality",
        "",
        f"- COF target rate: {_fmt(quality['cof_target_rate'])}",
        f"- strict success rate: {_fmt(quality['strict_success_rate'])}",
        f"- strict success reason: {_fmt(quality['strict_success_reason'])}",
        f"- fabrication success rate: {_fmt(quality['fabrication_success_rate'])}",
        f"- failure rate: {_fmt(quality['failure_rate'])}",
        f"- modulus in target rate: {_fmt(quality['modulus_in_target_rate'])}",
        f"- mean mechanical retention vs parent: {_fmt(quality['mean_mechanical_retention_vs_parent'])}",
        f"- stable friction rate: {_fmt(quality['stable_friction_rate'])}",
        f"- stable friction reason: {_fmt(quality['stable_friction_reason'])}",
        f"- stick-slip pass rate: {_fmt(quality['stick_slip_pass_rate'])}",
        f"- stick-slip reason: {_fmt(quality['stick_slip_reason'])}",
        f"- RAG-supported rate: {_fmt(quality['rag_supported_rate'])}",
        f"- RAG-supported reason: {_fmt(quality['rag_supported_reason'])}",
        f"- inventory hit rate: {_fmt(quality['inventory_hit_rate'])}",
        f"- new material rate: {_fmt(quality['new_material_rate'])}",
        f"- purchase blocked rate: {_fmt(quality['purchase_blocked_rate'])}",
        f"- inventory-constrained success rate: {_fmt(quality['inventory_constrained_success_rate'])}",
        f"- inventory reason: {_fmt(quality['inventory_reason'])}",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute wet-lab metrics for a cycle run directory.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--root-id", default=None)
    parser.add_argument("--output-prefix", type=Path, default=None)
    parser.add_argument("--chain-accept-delta", type=float, default=-1e-6)
    parser.add_argument("--cof-delta-threshold", type=float, default=0.005)
    parser.add_argument("--success-cof-max", type=float, default=0.02)
    parser.add_argument("--cof-target-max", type=float, default=0.03)
    parser.add_argument("--modulus-min", type=float, default=1.5)
    parser.add_argument("--modulus-max", type=float, default=2.5)
    parser.add_argument("--stable-proportion-min", type=float, default=0.6)
    parser.add_argument("--stick-slip-max", type=float, default=0.2)
    parser.add_argument("--inventory-csv", type=Path, default=None, help="Inventory/allowed-material CSV. Defaults to cycle/materials/materials_en.csv.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    metrics = compute_wetlab_metrics(
        run_dir=args.run_dir,
        root_id=args.root_id,
        chain_accept_delta=args.chain_accept_delta,
        cof_delta_threshold=args.cof_delta_threshold,
        success_cof_max=args.success_cof_max,
        cof_target_max=args.cof_target_max,
        modulus_min=args.modulus_min,
        modulus_max=args.modulus_max,
        stable_proportion_min=args.stable_proportion_min,
        stick_slip_max=args.stick_slip_max,
        inventory_csv=args.inventory_csv,
    )
    output_prefix = args.output_prefix or (args.run_dir / "wetlab_metrics")
    json_path, md_path = write_report(metrics, output_prefix)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
