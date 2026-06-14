"""Failure-factor memory for causal wet-lab learning.

This module converts failed experiments into reusable factor cards.  A failed
formula branch is not enough for learning; the system needs to remember which
single factor is suspected, confirmed, or disproved so later DOE rounds can
verify or avoid it.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List


MEMORY_JSONL = "failure_factor_memory.jsonl"
CONTRAST_JSONL = "experiment_contrast_memory.jsonl"
SUMMARY_MD = "FAILURE_FACTOR_SUMMARY.md"
PLAN_MD = "NEXT_VERIFICATION_PLAN.md"
FAILURE_TYPES = {"rupture", "break", "broken", "failure", "failed", "gel_failed", "no_gelation", "no gelation"}
CRITICAL_ERROR_CODES = {"ERROR1", "ERROR2", "ERROR3"}


def _artifact_dirs(out_dir: Path) -> List[Path]:
    trees_dir = out_dir / "trees"
    if trees_dir.exists():
        dirs = sorted(p for p in trees_dir.iterdir() if p.is_dir())
        return dirs or [out_dir]
    return [out_dir]


def _source_label(out_dir: Path, artifact_dir: Path) -> str:
    try:
        label = str(artifact_dir.relative_to(out_dir)).replace("\\", "/")
    except ValueError:
        label = str(artifact_dir).replace("\\", "/")
    return "." if label == "." else label


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _round_from_path(path: Path) -> int:
    try:
        return int(path.stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return 0


def _load_candidates(artifact_dir: Path) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for path in sorted(artifact_dir.glob("R*_candidates.json"), key=_round_from_path):
        obj = _load_json(path)
        for c in obj.get("candidates", []) or []:
            cid = c.get("candidate_id")
            if cid:
                cc = dict(c)
                cc["_round_idx"] = _round_from_path(path)
                candidates[cid] = cc
    return candidates


def _load_results(artifact_dir: Path) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    root_result = _load_json(artifact_dir / "results.json")
    if root_result.get("candidate_id"):
        results[str(root_result["candidate_id"])] = dict(root_result)
    for path in sorted(artifact_dir.glob("R*_results_filled.csv"), key=_round_from_path):
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    cid = (row.get("candidate_id") or "").strip()
                    if cid:
                        rr = dict(row)
                        rr["_round_idx"] = _round_from_path(path)
                        results[cid] = rr
        except Exception:
            continue
    return results


def _load_notes(artifact_dir: Path) -> Dict[str, Dict[str, Any]]:
    notes: Dict[str, Dict[str, Any]] = {}
    for path in sorted(artifact_dir.glob("R*_experiment_notes.json"), key=_round_from_path):
        obj = _load_json(path)
        for cid, entry in obj.items():
            if cid.startswith("_") or not isinstance(entry, dict):
                continue
            ee = dict(entry)
            ee["_round_idx"] = _round_from_path(path)
            notes[cid] = ee
    return notes


def _changed_variables(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    planned = candidate.get("planned_changed_variables") or []
    if isinstance(planned, list) and planned:
        return [x for x in planned if isinstance(x, dict) and x.get("variable")]
    changes = candidate.get("changed_variables") or []
    out: List[Dict[str, Any]] = []
    if isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict) and item.get("variable"):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                out.append({"variable": item.strip()})
    return out


def _failure_mode(row: Dict[str, Any], note: Dict[str, Any]) -> str:
    failure = str(row.get("failure_type") or "").strip()
    if failure:
        return failure
    codes = note.get("error_codes") or []
    if "ERROR1" in codes:
        return "rupture"
    if "ERROR2" in codes:
        return "no_gelation"
    if "ERROR3" in codes:
        return "too_soft_to_test"
    return ""


def _is_failed(row: Dict[str, Any], note: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    failure = _failure_mode(row, note).lower()
    if failure and (failure in FAILURE_TYPES or failure not in {"none", "na", "n/a"}):
        return True
    codes = {str(x) for x in (note.get("error_codes") or [])}
    if codes & CRITICAL_ERROR_CODES:
        return True
    return candidate.get("experimental_status") == "experimental_failed"


def _has_numeric_result(row: Dict[str, Any]) -> bool:
    return bool(str(row.get("cof_steady_mean") or "").strip())


def _candidate_formula_scope(candidate: Dict[str, Any]) -> Dict[str, Any]:
    formulation = candidate.get("formulation") or {}
    additives = []
    for additive in formulation.get("additives") or []:
        if isinstance(additive, dict) and additive.get("name") not in ("", None, "none"):
            additives.append(str(additive.get("name")))
    return {
        "network_type": formulation.get("network_type"),
        "method": formulation.get("crosslink_or_phys_method"),
        "additives": additives,
    }


def _factor_label(variable: str, value: Any, scope: Dict[str, Any]) -> str:
    method = scope.get("method") or scope.get("network_type") or "current network"
    return f"{variable}={value} in {method}"


def _factor_id(tree_id: str, variable: str, value: Any, failure_mode: str) -> str:
    raw = f"{tree_id}|{variable}|{value}|{failure_mode}".lower()
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"ff_{digest}"


def _status_for(candidate: Dict[str, Any], row: Dict[str, Any], note: Dict[str, Any]) -> str:
    design_type = str(candidate.get("design_type") or "").strip().lower()
    failed = _is_failed(row, note, candidate)
    if design_type == "failure_factor_verification":
        return "confirmed" if failed else "disproved"
    return "suspected" if failed else "mixed"


def _record_from_failure(
    out_dir: Path,
    artifact_dir: Path,
    candidate: Dict[str, Any],
    parent: Dict[str, Any] | None,
    row: Dict[str, Any],
    note: Dict[str, Any],
    change: Dict[str, Any],
) -> Dict[str, Any]:
    cid = candidate.get("candidate_id", "")
    tree_id = candidate.get("tree_id") or candidate.get("tree_label") or artifact_dir.name
    variable = str(change.get("variable") or "").strip()
    value = change.get("new_value")
    if value in (None, ""):
        value = change.get("value")
    old_value = change.get("old_value")
    failure_mode = _failure_mode(row, note) or "failed"
    scope = _candidate_formula_scope(candidate)
    status = _status_for(candidate, row, note)
    evidence = f"{_source_label(out_dir, artifact_dir)}/{cid}".lstrip("./")
    confidence = {"suspected": 0.35, "mixed": 0.25, "confirmed": 0.85, "disproved": 0.75}.get(status, 0.3)
    return {
        "factor_id": _factor_id(str(tree_id), variable, value, failure_mode),
        "factor": _factor_label(variable, value, scope),
        "variable": variable,
        "suspected_value": value,
        "parent_value": old_value,
        "status": status,
        "failure_mode": failure_mode,
        "error_codes": note.get("error_codes") or [],
        "scope": scope,
        "tree_id": tree_id,
        "source_dir": _source_label(out_dir, artifact_dir),
        "candidate_id": cid,
        "parent_candidate_id": candidate.get("parent_candidate_id"),
        "parent_experimental_status": (parent or {}).get("experimental_status"),
        "design_type": candidate.get("design_type"),
        "evidence": [evidence],
        "counter_evidence": [] if status != "disproved" else [evidence],
        "avoid_policy": "do_not_reuse" if status == "confirmed" else "verify_before_reuse",
        "confidence": confidence,
    }


def _merge_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        key = (
            rec.get("tree_id"),
            rec.get("variable"),
            str(rec.get("suspected_value")),
            rec.get("failure_mode"),
        )
        grouped[key].append(rec)

    merged: List[Dict[str, Any]] = []
    precedence = {"confirmed": 4, "disproved": 3, "suspected": 2, "mixed": 1}
    for items in grouped.values():
        base = max(items, key=lambda r: precedence.get(str(r.get("status")), 0)).copy()
        evidence: list[str] = []
        counter: list[str] = []
        statuses = {str(r.get("status")) for r in items}
        for item in items:
            evidence.extend(str(x) for x in item.get("evidence", []))
            counter.extend(str(x) for x in item.get("counter_evidence", []))
        if "confirmed" in statuses:
            status = "confirmed"
        elif "disproved" in statuses and "suspected" not in statuses:
            status = "disproved"
        elif len(statuses) > 1:
            status = "mixed"
        else:
            status = next(iter(statuses))
        base["status"] = status
        base["evidence"] = sorted(set(evidence))
        base["counter_evidence"] = sorted(set(counter))
        base["evidence_count"] = len(base["evidence"])
        base["avoid_policy"] = "do_not_reuse" if status == "confirmed" else "verify_before_reuse"
        base["confidence"] = min(0.95, round(float(base.get("confidence") or 0.3) + 0.08 * (base["evidence_count"] - 1), 3))
        merged.append(base)
    merged.sort(key=lambda r: (str(r.get("status")), str(r.get("tree_id")), str(r.get("variable"))))
    return merged


def _numeric_value(row: Dict[str, Any], key: str) -> float | None:
    try:
        value = row.get(key)
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _contrast_id(source: str, parent_id: str, child_id: str) -> str:
    raw = f"{source}|{parent_id}|{child_id}".lower()
    return "cx_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def build_contrast_records(out_dir: Path) -> List[Dict[str, Any]]:
    """Build parent-child positive/negative contrast samples."""
    records: List[Dict[str, Any]] = []
    for artifact_dir in _artifact_dirs(out_dir):
        source = _source_label(out_dir, artifact_dir)
        candidates = _load_candidates(artifact_dir)
        results = _load_results(artifact_dir)
        notes = _load_notes(artifact_dir)
        for cid, child in candidates.items():
            parent_id = child.get("parent_candidate_id")
            if not parent_id:
                continue
            child_row = results.get(cid, {})
            child_note = notes.get(cid, {})
            failed = _is_failed(child_row, child_note, child)
            child_cof = _numeric_value(child_row, "cof_steady_mean")
            if not failed and child_cof is None:
                continue
            parent = candidates.get(parent_id)
            parent_row = results.get(parent_id, {})
            parent_cof = _numeric_value(parent_row, "cof_steady_mean")
            changed = _changed_variables(child)
            label = "negative" if failed else "positive"
            records.append(
                {
                    "contrast_id": _contrast_id(source, str(parent_id), str(cid)),
                    "label": label,
                    "source_dir": source,
                    "tree_id": child.get("tree_id") or child.get("tree_label") or artifact_dir.name,
                    "parent_candidate_id": parent_id,
                    "child_candidate_id": cid,
                    "design_type": child.get("design_type"),
                    "changed_variables": changed,
                    "failure_mode": _failure_mode(child_row, child_note) if failed else "",
                    "error_codes": child_note.get("error_codes") or [],
                    "parent_cof": parent_cof,
                    "child_cof": child_cof,
                    "delta_cof": (child_cof - parent_cof) if child_cof is not None and parent_cof is not None else None,
                    "learning_signal": (
                        "negative_sample: changed factors produced unusable material; use for avoid/verification"
                        if failed
                        else "positive_sample: changed factors produced measurable material; use for safe-region learning"
                    ),
                    "parent_scope": _candidate_formula_scope(parent or {}),
                    "child_scope": _candidate_formula_scope(child),
                }
            )
    records.sort(key=lambda r: (r["label"], r["source_dir"], r["child_candidate_id"]))
    return records


def write_contrast_memory(out_dir: Path, records: List[Dict[str, Any]]) -> None:
    with (out_dir / CONTRAST_JSONL).open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_contrast_records(out_dir: Path) -> List[Dict[str, Any]]:
    path = out_dir / CONTRAST_JSONL
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def build_failure_factor_memory(out_dir: Path) -> List[Dict[str, Any]]:
    """Scan a workspace and write failure factor memory artifacts."""
    records: List[Dict[str, Any]] = []
    for artifact_dir in _artifact_dirs(out_dir):
        candidates = _load_candidates(artifact_dir)
        results = _load_results(artifact_dir)
        notes = _load_notes(artifact_dir)
        for cid, candidate in candidates.items():
            row = results.get(cid, {})
            note = notes.get(cid, {})
            if not row and not note and candidate.get("experimental_status") != "experimental_failed":
                continue
            failed = _is_failed(row, note, candidate)
            if not failed and candidate.get("design_type") != "failure_factor_verification":
                continue
            changes = _changed_variables(candidate)
            if not changes:
                continue
            parent = candidates.get(candidate.get("parent_candidate_id"))
            for change in changes:
                rec = _record_from_failure(out_dir, artifact_dir, candidate, parent, row, note, change)
                if not failed and _has_numeric_result(row):
                    rec["status"] = "disproved"
                    rec["avoid_policy"] = "allowed"
                records.append(rec)

    merged = _merge_records(records)
    write_failure_factor_memory(out_dir, merged)
    contrast_records = build_contrast_records(out_dir)
    write_contrast_memory(out_dir, contrast_records)
    write_failure_factor_reports(out_dir, merged, contrast_records)
    try:
        from pva_work_flow.memory.vector_rag import build_project_vector_index
        from pva_work_flow.memory.formulation_rag import resolve_formulation_rag_db

        db_path = resolve_formulation_rag_db()
        build_project_vector_index(out_dir, formulation_db=db_path if db_path.exists() else None)
    except Exception:
        pass
    return merged


def write_failure_factor_memory(out_dir: Path, records: List[Dict[str, Any]]) -> None:
    with (out_dir / MEMORY_JSONL).open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_failure_factors(out_dir: Path) -> List[Dict[str, Any]]:
    path = out_dir / MEMORY_JSONL
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def select_suspected_factors_for_parent(out_dir: Path, parent: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
    records = load_failure_factors(out_dir)
    if not records:
        try:
            records = build_failure_factor_memory(out_dir)
        except Exception:
            records = []
    tree_id = parent.get("tree_id") or parent.get("tree_label") or parent.get("root_candidate_id")
    selected = [
        r for r in records
        if r.get("status") == "suspected"
        and (not tree_id or r.get("tree_id") == tree_id)
        and r.get("variable")
    ]
    selected.sort(key=lambda r: (-float(r.get("confidence") or 0), -int(r.get("evidence_count") or 0), str(r.get("variable"))))
    return selected[:limit]


def build_failure_factor_context(out_dir: Path, max_records: int = 10) -> str:
    records = load_failure_factors(out_dir)
    if not records:
        try:
            records = build_failure_factor_memory(out_dir)
        except Exception:
            records = []
    if not records:
        return ""

    lines = [
        "=== FAILURE FACTOR MEMORY ===",
        "Use this as project evidence. Confirmed failure factors must be avoided unless explicitly doing failure-factor verification.",
        "Suspected factors should be tested one at a time before being used for optimization.",
        "Successful and failed experiments are both training samples: positives define useful regions; negatives define avoid/verify boundaries.",
    ]
    contrasts = load_contrast_records(out_dir)
    if not contrasts:
        try:
            contrasts = build_contrast_records(out_dir)
        except Exception:
            contrasts = []
    if contrasts:
        positives = [r for r in contrasts if r.get("label") == "positive"]
        negatives = [r for r in contrasts if r.get("label") == "negative"]
        lines.append(f"Contrast samples: positive={len(positives)}, negative={len(negatives)}.")
        for rec in negatives[: min(5, max_records)]:
            changed = ", ".join(
                str(x.get("variable"))
                for x in rec.get("changed_variables", [])
                if isinstance(x, dict) and x.get("variable")
            )
            lines.append(
                f"- NEGATIVE {rec.get('parent_candidate_id')} -> {rec.get('child_candidate_id')}: "
                f"{changed or 'unknown change'} ended with {rec.get('failure_mode') or 'failure'}."
            )
    try:
        from pva_work_flow.memory.vector_rag import ensure_project_vector_index, query_vector_index, render_vector_hits
        from pva_work_flow.memory.formulation_rag import resolve_formulation_rag_db

        query_terms = "PVA hydrogel rupture broken no gelation failure negative sample mechanical integrity"
        db_path = resolve_formulation_rag_db()
        index = ensure_project_vector_index(out_dir, formulation_db=db_path if db_path.exists() else None)
        vector_hits = query_vector_index(
            index,
            query_terms,
            top_k=5,
            source_types={"experiment_contrast", "failure_factor", "tree_memory"},
        )
        rendered = render_vector_hits(vector_hits, title="LOCAL VECTOR RAG: SIMILAR PROJECT MEMORY")
        if rendered:
            lines.append(rendered)
    except Exception:
        pass
    for status in ("confirmed", "suspected", "disproved", "mixed"):
        subset = [r for r in records if r.get("status") == status][:max_records]
        if not subset:
            continue
        lines.append(f"{status.upper()} factors:")
        for rec in subset:
            evidence = ", ".join(rec.get("evidence", [])[:3])
            lines.append(
                f"- {rec.get('factor')} [{rec.get('failure_mode')}], "
                f"confidence={rec.get('confidence')}, policy={rec.get('avoid_policy')}, evidence={evidence}"
            )
    return "\n".join(lines)


def write_failure_factor_reports(
    out_dir: Path,
    records: List[Dict[str, Any]],
    contrast_records: List[Dict[str, Any]] | None = None,
) -> None:
    contrast_records = contrast_records if contrast_records is not None else load_contrast_records(out_dir)
    by_status: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_status[str(rec.get("status") or "unknown")].append(rec)

    summary = ["# Failure Factor Summary", ""]
    positives = [r for r in contrast_records if r.get("label") == "positive"]
    negatives = [r for r in contrast_records if r.get("label") == "negative"]
    summary.extend(
        [
            "## Positive/Negative Sample Balance",
            f"- positive_sample_count: {len(positives)}",
            f"- negative_sample_count: {len(negatives)}",
            "- Principle: successful and failed wet-lab samples are both useful training evidence.",
            "",
        ]
    )
    for status in ("confirmed", "suspected", "disproved", "mixed"):
        summary.append(f"## {status.title()}")
        items = by_status.get(status, [])
        if not items:
            summary.append("- _none_")
        for rec in items:
            evidence = ", ".join(rec.get("evidence", [])[:4])
            summary.append(
                f"- `{rec.get('factor_id')}` {rec.get('factor')} -> {rec.get('failure_mode')}; "
                f"policy={rec.get('avoid_policy')}; confidence={rec.get('confidence')}; evidence={evidence}"
            )
        summary.append("")
    (out_dir / SUMMARY_MD).write_text("\n".join(summary).rstrip() + "\n", encoding="utf-8")

    plan = ["# Next Verification Plan", ""]
    suspected = by_status.get("suspected", [])
    if not suspected:
        plan.append("_No suspected factors currently require verification._")
    else:
        plan.append("## Priority Single-Factor Checks")
        for rec in suspected[:12]:
            plan.append(
                f"- Verify `{rec.get('factor_id')}` by changing only `{rec.get('variable')}` "
                f"to `{rec.get('suspected_value')}` from parent `{rec.get('parent_candidate_id')}`; "
                f"hold network/additives/process otherwise fixed. If it fails again, mark confirmed and avoid."
            )
    confirmed = by_status.get("confirmed", [])
    if confirmed:
        plan.append("")
        plan.append("## Avoid In Future Optimization")
        for rec in confirmed[:12]:
            plan.append(f"- Avoid {rec.get('factor')} unless the design is explicitly a rescue/verification experiment.")
    (out_dir / PLAN_MD).write_text("\n".join(plan).rstrip() + "\n", encoding="utf-8")
