"""Chain-level statistical memory for greedy optimization runs.

This module is separate from chain_search.py. Chain search selects the next
parent; chain memory summarizes what previous chain steps taught the model.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from pva_work_flow.artifacts.artifact_store import RunWorkspace
from pva_work_flow.artifacts.io_artifacts import aggregate_cof_from_row
from pva_work_flow.wetlab.wetlab_outcomes import has_failure, compute_cvs


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _candidate_round(path: Path) -> int | None:
    try:
        return int(path.stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return None


def _load_candidates(out_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], list[str]]:
    candidates: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for path in RunWorkspace(out_dir).all_round_artifact_paths("candidates.json"):
        round_idx = _candidate_round(path)
        obj = _load_json(path)
        for candidate in obj.get("candidates", []) or []:
            cid = candidate.get("candidate_id")
            if not cid:
                continue
            candidates[cid] = candidate
            parent_id = candidate.get("parent_candidate_id")
            if parent_id:
                children[parent_id].append(cid)
            if round_idx == 1 or not parent_id:
                roots.append(cid)
    return candidates, dict(children), sorted(set(roots))


def _load_results(out_dir: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for path in RunWorkspace(out_dir).all_round_artifact_paths("results_filled.csv"):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                cid = (row.get("candidate_id") or "").strip()
                if not cid:
                    continue
                row["_experimental_failed"] = has_failure(row)
                cof, cof_std = aggregate_cof_from_row(row)
                row["_cof"] = cof
                row["_cof_std"] = cof_std
                # Compute CVS for ranking
                cvs_result = compute_cvs(row)
                row["_cvs"] = cvs_result.get("cvs", 0.0)
                row["_cvs_grade"] = cvs_result.get("grade", "F")
                row["_cvs_i"] = cvs_result.get("i_multiplier", 1.0)
                results[cid] = row
    return results


def _changed_names(candidate: dict[str, Any]) -> list[str]:
    names = candidate.get("changed_variable_names") or []
    if isinstance(names, list) and names:
        return [str(item) for item in names if str(item).strip()]
    changes = candidate.get("changed_variables") or []
    out: list[str] = []
    if isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict) and item.get("variable"):
                out.append(str(item["variable"]))
            elif isinstance(item, str) and item.strip():
                out.append(item)
    planned = candidate.get("planned_changed_variables") or []
    if not out and isinstance(planned, list):
        for item in planned:
            if isinstance(item, dict) and item.get("variable"):
                out.append(str(item["variable"]))
    return out


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percent_improvement(root_cof: float | None, final_cof: float | None) -> float | None:
    if root_cof is None or final_cof is None or root_cof == 0:
        return None
    return round((root_cof - final_cof) / root_cof, 6)


def _trace_root(
    root_id: str,
    children: dict[str, list[str]],
    candidates: dict[str, dict[str, Any]],
    cofs: dict[str, float],
    accept_delta: float,
    cvs_by_id: dict[str, float] | None = None,
) -> dict[str, Any]:
    current = root_id
    visited: set[str] = set()
    trace: list[dict[str, Any]] = []
    while current not in visited:
        visited.add(current)
        parent_cof = cofs.get(current)
        measured_children = [cid for cid in children.get(current, []) if cid in cofs]
        if parent_cof is None or not measured_children:
            break
        # Use CVS as primary ranking (higher = better); fall back to COF (lower = better)
        if cvs_by_id and any(cvs_by_id.get(c) is not None for c in measured_children):
            best_child = max(measured_children, key=lambda cid: cvs_by_id.get(cid, 0.0))
        else:
            best_child = min(measured_children, key=lambda cid: cofs[cid])
        delta = cofs[best_child] - parent_cof
        accepted = delta < accept_delta
        candidate = candidates.get(best_child, {})
        trace.append(
            {
                "parent_id": current,
                "parent_cof": parent_cof,
                "best_child_id": best_child,
                "best_child_cof": cofs[best_child],
                "best_child_cvs": cvs_by_id.get(best_child) if cvs_by_id else None,
                "changed_variables": _changed_names(candidate),
                "delta_cof": round(delta, 6),
                "decision": "accept" if accepted else "retry_parent",
            }
        )
        if not accepted:
            break
        current = best_child

    return {
        "root_id": root_id,
        "root_cof": cofs.get(root_id),
        "current_parent_id": current,
        "current_parent_cof": cofs.get(current),
        "accepted_steps": sum(1 for item in trace if item["decision"] == "accept"),
        "endpoint_improvement": _percent_improvement(cofs.get(root_id), cofs.get(current)),
        "trace": trace,
    }


def _summarize_variables(chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_var: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chain in chains:
        for step in chain.get("trace", []):
            variables = step.get("changed_variables") or ["unknown"]
            for variable in variables:
                by_var[str(variable)].append(step)

    rows: list[dict[str, Any]] = []
    for variable, steps in by_var.items():
        measured = [step for step in steps if step.get("delta_cof") is not None]
        deltas = [float(step["delta_cof"]) for step in measured]
        accepted = [step for step in measured if step.get("decision") == "accept"]
        retry = [step for step in measured if step.get("decision") == "retry_parent"]
        rows.append(
            {
                "variable": variable,
                "offered_n": len(steps),
                "measured_n": len(measured),
                "accepted_n": len(accepted),
                "retry_n": len(retry),
                "accept_rate": _rate(len(accepted), len(measured)),
                "mean_delta_cof": round(mean(deltas), 6) if deltas else None,
                "best_delta_cof": round(min(deltas), 6) if deltas else None,
                "worst_delta_cof": round(max(deltas), 6) if deltas else None,
            }
        )
    rows.sort(
        key=lambda row: (
            -(row["accept_rate"] or 0),
            row["mean_delta_cof"] if row["mean_delta_cof"] is not None else 999,
            -row["offered_n"],
        )
    )
    return rows


def build_chain_memory(out_dir: Path, accept_delta: float = -1e-6) -> dict[str, Any]:
    candidates, children, roots = _load_candidates(out_dir)
    results = _load_results(out_dir)
    cofs = {
        cid: float(row["_cof"])
        for cid, row in results.items()
        if row.get("_cof") is not None and not row.get("_experimental_failed")
    }
    cvs_by_id = {
        cid: float(row.get("_cvs", 0.0))
        for cid, row in results.items()
        if row.get("_cvs") is not None
    }
    measured_roots = [root for root in roots if root in cofs]
    chains = [
        _trace_root(root, children, candidates, cofs, accept_delta, cvs_by_id)
        for root in measured_roots
    ]
    chains = [chain for chain in chains if chain.get("trace")]
    variable_stats = _summarize_variables(chains)

    # Best CVS across all measured candidates
    _all_cvs = [(cid, row.get("_cvs", 0.0), row.get("_cvs_grade", "F"))
                 for cid, row in results.items() if row.get("_cvs") is not None]
    _all_cvs.sort(key=lambda x: -x[1])

    payload = {
        "summary": {
            "root_count": len(measured_roots),
            "chain_count": len(chains),
            "accepted_step_count": sum(chain["accepted_steps"] for chain in chains),
            "retry_step_count": sum(
                1 for chain in chains for step in chain.get("trace", []) if step.get("decision") == "retry_parent"
            ),
            "accept_delta": accept_delta,
            "best_overall_cvs": _all_cvs[0][1] if _all_cvs else 0.0,
            "best_overall_cvs_candidate": _all_cvs[0][0] if _all_cvs else None,
            "best_overall_cvs_grade": _all_cvs[0][2] if _all_cvs else "F",
        },
        "cvs_ranking": [
            {"candidate_id": cid, "cvs": cvs, "grade": grade}
            for cid, cvs, grade in _all_cvs[:10]
        ],
        "variable_stats": variable_stats,
        "chains": chains,
    }
    (out_dir / "chain_memory.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "chain_memory.md").write_text(chain_memory_markdown(payload), encoding="utf-8")
    _write_memory_cards(out_dir, payload)
    return payload


def chain_memory_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = ["# Chain Memory", ""]
    lines.append("## Summary")
    for key in ["root_count", "chain_count", "accepted_step_count", "retry_step_count", "accept_delta"]:
        lines.append(f"- {key}: {summary.get(key)}")

    lines.extend(["", "## Variable Priors"])
    lines.append("| variable | offered | accepted | retry | accept_rate | mean_delta_cof | best_delta_cof |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in payload.get("variable_stats", [])[:20]:
        lines.append(
            f"| {row['variable']} | {row['offered_n']} | {row['accepted_n']} | "
            f"{row['retry_n']} | {row['accept_rate']} | {row['mean_delta_cof']} | {row['best_delta_cof']} |"
        )

    lines.extend(["", "## Chains"])
    for chain in payload.get("chains", [])[:20]:
        lines.append(
            f"- {chain['root_id']} -> {chain['current_parent_id']}: "
            f"accepted_steps={chain['accepted_steps']}, endpoint_improvement={chain['endpoint_improvement']}"
        )
    return "\n".join(lines) + "\n"


def _write_memory_cards(out_dir: Path, payload: dict[str, Any]) -> None:
    cards: list[dict[str, Any]] = []
    for row in payload.get("variable_stats", [])[:20]:
        direction = "chain_promising" if row.get("mean_delta_cof") is not None and row["mean_delta_cof"] < 0 else "chain_risky_or_uncertain"
        cards.append(
            {
                "card_type": "chain_variable_effect",
                "key": row["variable"],
                "direction": direction,
                "evidence": row,
                "retrieval_text": (
                    f"{row['variable']}: chain_accept_rate={row['accept_rate']}, "
                    f"mean_delta_cof={row['mean_delta_cof']}, best_delta_cof={row['best_delta_cof']}"
                ),
            }
        )
    with (out_dir / "chain_memory_cards.jsonl").open("w", encoding="utf-8") as handle:
        for card in cards:
            handle.write(json.dumps(card, ensure_ascii=False) + "\n")


def build_chain_memory_context(out_dir: Path, max_variables: int = 6, max_chains: int = 4) -> str:
    try:
        memory = build_chain_memory(out_dir)
    except Exception:
        memory = _load_json(out_dir / "chain_memory.json")
        if not memory:
            return ""
    if not memory.get("chains"):
        return ""

    summary = memory.get("summary", {})
    lines = [
        "=== CHAIN-LEVEL OPTIMIZATION MEMORY ===",
        "Use this as chain-search prior only. Prefer variables repeatedly accepted on measured chains; avoid variables that caused retry_parent.",
        (
            f"chains={summary.get('chain_count')}, accepted_steps={summary.get('accepted_step_count')}, "
            f"retry_steps={summary.get('retry_step_count')}"
        ),
    ]
    variable_stats = memory.get("variable_stats", [])[:max_variables]
    if variable_stats:
        lines.append("Chain variable priors:")
        for row in variable_stats:
            lines.append(
                f"- {row['variable']}: offered={row['offered_n']}, accept_rate={row['accept_rate']}, "
                f"mean_delta_cof={row['mean_delta_cof']}, best_delta_cof={row['best_delta_cof']}"
            )
    chains = memory.get("chains", [])[:max_chains]
    if chains:
        lines.append("Measured chain outcomes:")
        for chain in chains:
            lines.append(
                f"- {chain['root_id']} -> {chain['current_parent_id']}: "
                f"accepted_steps={chain['accepted_steps']}, endpoint_improvement={chain['endpoint_improvement']}"
            )
    lines.append("Implication: in the next local step, choose small changes with negative chain mean_delta_cof unless parent-specific evidence says otherwise.")
    return "\n".join(lines)
