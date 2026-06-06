"""
Layer 2.2 — Experiment Trajectory RAG (docs/design/hydrogel_agent_optimization_plan.md)

JSONL-based experiment memory with keyword retrieval so the LLM sees
relevant historical context without blowing up the prompt window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .wetlab_outcomes import rank_key


# ---- JSONL storage ----
def append_experiment_record(record: Dict[str, Any], path: str | Path = "experiment_records.jsonl") -> None:
    """Append one experiment record to a JSONL memory file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_experiment_records(path: str | Path = "experiment_records.jsonl") -> list[Dict[str, Any]]:
    """Load all experiment records from JSONL."""
    p = Path(path)
    if not p.exists():
        return []
    records: list[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---- Retrieval functions ----
def retrieve_best_formulas(
    records: list[Dict[str, Any]],
    metric: str = "friction_coefficient",
    top_k: int = 5,
) -> list[Dict[str, Any]]:
    """Retrieve top-k formulas ranked by a numeric metric (ascending = better)."""
    scored: list[tuple[tuple[int, float, float], Dict[str, Any]]] = []
    for r in records:
        obs = r.get("observations", {}) or {}
        val = obs.get(metric)
        if val is None:
            continue
        scored.append((
            rank_key(
                {
                    "cof_steady_mean": val,
                    "failure_type": "failure" if r.get("experimental_status") == "experimental_failed" else "",
                    "notes": obs.get("failure_notes", ""),
                }
            ),
            r,
        ))
    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored[:top_k]]


def retrieve_failed_formulas(
    records: list[Dict[str, Any]],
    top_k: int = 5,
) -> list[Dict[str, Any]]:
    """Retrieve formulas with failure notes or poor performance."""
    failed: list[Dict[str, Any]] = []
    for r in records:
        obs = r.get("observations", {}) or {}
        interp = r.get("interpretation", {}) or {}
        failure = obs.get("failure_notes", "").strip()
        rank = interp.get("performance_rank", "").strip().lower()
        if failure or rank in ("poor", "failed"):
            failed.append(r)
    return failed[:top_k]


def retrieve_by_variable(
    records: list[Dict[str, Any]],
    variable_name: str,
    top_k: int = 10,
) -> list[Dict[str, Any]]:
    """Retrieve formulas whose changed/unchanged variables mention `variable_name`."""
    var_lower = variable_name.strip().lower()
    matched: list[Dict[str, Any]] = []
    for r in records:
        changed = r.get("changed_variables", []) or []
        unchanged = r.get("unchanged_variables", []) or []
        all_vars = [str(v).lower() for v in changed + unchanged]
        if any(var_lower in v for v in all_vars):
            matched.append(r)
        elif var_lower in json.dumps(r.get("composition", {}), ensure_ascii=False).lower():
            matched.append(r)
    return matched[:top_k]


def retrieve_by_material(
    records: list[Dict[str, Any]],
    material_name: str,
    top_k: int = 10,
) -> list[Dict[str, Any]]:
    """Retrieve formulas whose composition mentions `material_name`."""
    mat_lower = material_name.strip().lower()
    matched: list[Dict[str, Any]] = []
    for r in records:
        comp_str = json.dumps(r.get("composition", {}), ensure_ascii=False).lower()
        if mat_lower in comp_str:
            matched.append(r)
    return matched[:top_k]


# ---- Context builder for prompt injection ----
def build_context_from_retrieval(records: list[Dict[str, Any]]) -> str:
    """Convert retrieved records into a concise LLM-readable context block."""
    if not records:
        return ""

    lines: list[str] = []
    lines.append("Relevant historical findings:")

    for i, r in enumerate(records[:8]):
        fid = r.get("formula_id", "?")
        parent = r.get("parent_formula_id", "?")
        dt = r.get("design_type", "?")
        obs = r.get("observations", {}) or {}
        interp = r.get("interpretation", {}) or {}
        comp = r.get("composition", {}) or {}

        pva_wt = comp.get("PVA", {}).get("wt_percent", "?")
        cl = comp.get("crosslinker", {}) or {}
        cl_name = cl.get("name", "none") if isinstance(cl, dict) else "none"

        lines.append(
            f"  {i+1}. {fid} (parent={parent}, type={dt}): "
            f"PVA={pva_wt}%, cl={cl_name}, "
            f"COF={obs.get('friction_coefficient', '?')}, "
            f"gel={obs.get('gelation_status', '?')}, "
            f"failure={obs.get('failure_notes', 'none')[:60]}"
        )
        if interp.get("main_problem"):
            lines.append(f"     problem: {interp['main_problem']}")

    lines.append("\nImplication for next round:")
    if records:
        best = min(
            (r for r in records if r.get("observations", {}).get("friction_coefficient") is not None),
            key=lambda r: rank_key(
                {
                    "cof_steady_mean": r.get("observations", {}).get("friction_coefficient"),
                    "failure_type": "failure" if r.get("experimental_status") == "experimental_failed" else "",
                    "notes": r.get("observations", {}).get("failure_notes", ""),
                }
            ),
            default=None,
        )
        if best:
            lines.append(f"  - Best historical: {best.get('formula_id')} (COF={best.get('observations', {}).get('friction_coefficient')})")

        failures = [r for r in records if (r.get("observations", {}).get("failure_notes", "").strip())]
        if failures:
            fail_ids = [r.get("formula_id", "?") for r in failures[:3]]
            lines.append(f"  - Failed formulas to avoid repeating: {', '.join(fail_ids)}")

    return "\n".join(lines)


# ---- Integration: build RAG context for the next round ----
def build_rag_context_for_round(
    memory_path: str | Path = "experiment_records.jsonl",
    focus_variables: list[str] | None = None,
    focus_materials: list[str] | None = None,
) -> str:
    """Assemble a retrieval-augmented context block for the next generation round."""
    records = load_experiment_records(memory_path)
    if not records:
        return ""

    best = retrieve_best_formulas(records, top_k=5)
    failed = retrieve_failed_formulas(records, top_k=5)

    # Deduplicate
    seen_ids: set[str] = set()
    merged: list[Dict[str, Any]] = []
    for r in best + failed:
        fid = r.get("formula_id", "")
        if fid not in seen_ids:
            seen_ids.add(fid)
            merged.append(r)

    # Add variable/material specific retrievals
    if focus_variables:
        for var in focus_variables:
            var_recs = retrieve_by_variable(records, var, top_k=3)
            for r in var_recs:
                fid = r.get("formula_id", "")
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    merged.append(r)

    if focus_materials:
        for mat in focus_materials:
            mat_recs = retrieve_by_material(records, mat, top_k=3)
            for r in mat_recs:
                fid = r.get("formula_id", "")
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    merged.append(r)

    return build_context_from_retrieval(merged)
