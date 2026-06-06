"""Human-readable reports for tree-mode optimization workspaces."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from .formula_tree import infer_branch_decisions
from .tree_naming import normalize_tree_label


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _artifact_dirs(out_dir: Path) -> List[Path]:
    trees_dir = out_dir / "trees"
    if not trees_dir.exists():
        return [out_dir]
    tree_dirs = sorted(p for p in trees_dir.iterdir() if p.is_dir())
    return tree_dirs or [out_dir]


def _source_label(out_dir: Path, artifact_dir: Path) -> str:
    try:
        label = str(artifact_dir.relative_to(out_dir)).replace("\\", "/")
    except ValueError:
        label = str(artifact_dir).replace("\\", "/")
    return "." if label == "." else label


def _round_from_path(path: Path) -> int:
    try:
        return int(path.stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return 0


def _load_candidates(artifact_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(artifact_dir.glob("R*_candidates.json"), key=_round_from_path):
        round_idx = _round_from_path(path)
        obj = _load_json(path)
        for candidate in obj.get("candidates", []) or []:
            if candidate.get("candidate_id"):
                c = dict(candidate)
                c["_round_idx"] = round_idx
                rows.append(c)
    if not rows:
        root = _load_json(artifact_dir / "root_candidate.json")
        root_candidates = root.get("candidates") if isinstance(root.get("candidates"), list) else None
        if root_candidates:
            root = root_candidates[0] if root_candidates else {}
        if root:
            c = dict(root)
            c.setdefault("candidate_id", root.get("root_candidate_id") or artifact_dir.name)
            c.setdefault("tree_id", artifact_dir.name)
            c.setdefault("root_candidate_id", c.get("candidate_id"))
            c.setdefault("tree_depth", 0)
            c["_round_idx"] = 1
            rows.append(c)
    return rows


def _load_results(artifact_dir: Path) -> Dict[str, Dict[str, str]]:
    results: Dict[str, Dict[str, str]] = {}
    for path in sorted(artifact_dir.glob("R*_results_filled.csv"), key=_round_from_path):
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    cid = (row.get("candidate_id") or "").strip()
                    if cid:
                        results[cid] = row
        except Exception:
            continue
    return results


def _short_formula(candidate: Dict[str, Any]) -> str:
    formulation = candidate.get("formulation") or {}
    pva = formulation.get("pva_wt_percent")
    method = formulation.get("crosslink_or_phys_method") or formulation.get("network_type") or "unknown"
    additives = []
    for additive in formulation.get("additives") or []:
        if not isinstance(additive, dict):
            continue
        name = additive.get("name") or "unknown"
        wt = additive.get("wt_percent")
        additives.append(f"{name} {wt}%" if wt not in (None, "") else str(name))
    additive_text = "; ".join(additives) if additives else "none"
    return f"PVA {pva if pva is not None else '?'}%; {method}; additives: {additive_text}"


def _materials_table(candidate: Dict[str, Any]) -> List[str]:
    materials = candidate.get("materials") or []
    lines = ["| material | role | amount | unit | basis |", "|---|---|---:|---|---|"]
    for material in materials:
        if not isinstance(material, dict):
            continue
        lines.append(
            "| {name} | {role} | {amount} | {unit} | {basis} |".format(
                name=material.get("name", ""),
                role=material.get("role", ""),
                amount=material.get("amount", ""),
                unit=material.get("unit", ""),
                basis=material.get("basis", ""),
            )
        )
    if len(lines) == 2:
        lines.append("| _not specified_ |  |  |  |  |")
    return lines


def _process_steps(candidate: Dict[str, Any]) -> List[str]:
    process = candidate.get("process") or {}
    steps = process.get("steps")
    if not steps:
        steps = (candidate.get("processing") or {}).get("steps")
    if not isinstance(steps, list):
        return ["- _not specified_"]
    lines: List[str] = []
    for idx, step in enumerate(steps, start=1):
        if isinstance(step, dict):
            name = step.get("name") or step.get("description") or "step"
            temp = step.get("temperature_C")
            duration = step.get("duration_hours")
            suffix = []
            if temp not in (None, ""):
                suffix.append(f"{temp} C")
            if duration not in (None, ""):
                suffix.append(f"{duration} h")
            meta = f" ({', '.join(suffix)})" if suffix else ""
            lines.append(f"{idx}. {name}{meta}")
        else:
            lines.append(f"{idx}. {step}")
    return lines or ["- _not specified_"]


def _candidate_line(
    candidate: Dict[str, Any],
    decisions: Dict[str, Dict[str, Any]],
    results: Dict[str, Dict[str, str]],
) -> str:
    cid = candidate.get("candidate_id", "")
    decision = decisions.get(cid, {})
    result = results.get(cid, {})
    status = decision.get("branch_status") or candidate.get("branch_status") or "pending"
    cof = result.get("cof_steady_mean")
    cof_text = f", COF={cof}" if cof not in (None, "") else ""
    changed = candidate.get("changed_variable_names") or []
    changed_text = ", ".join(changed) if changed else "baseline/root"
    return f"- **{cid}** [{status}{cof_text}] `{candidate.get('design_type', '?')}`: {changed_text}; {_short_formula(candidate)}"


def build_simple_tree_report(artifact_dir: Path, root_out_dir: Path | None = None) -> str:
    candidates = _load_candidates(artifact_dir)
    decisions = infer_branch_decisions(artifact_dir, write=True)
    results = _load_results(artifact_dir)
    label = artifact_dir.name if root_out_dir else normalize_tree_label((candidates[0] if candidates else {}).get("tree_id"))

    lines = [f"# Simple Optimization Tree: {label}", ""]
    if not candidates:
        lines.append("_No candidates found._")
    else:
        by_parent: Dict[str | None, List[Dict[str, Any]]] = {}
        for candidate in candidates:
            by_parent.setdefault(candidate.get("parent_candidate_id"), []).append(candidate)
        candidate_ids = {c.get("candidate_id") for c in candidates if c.get("candidate_id")}

        def emit(candidate: Dict[str, Any], depth: int = 0) -> None:
            lines.append("  " * depth + _candidate_line(candidate, decisions, results))
            for child in sorted(by_parent.get(candidate.get("candidate_id"), []), key=lambda c: c.get("candidate_id", "")):
                emit(child, depth + 1)

        roots = list(by_parent.get(None, []))
        roots.extend(
            c
            for c in candidates
            if c.get("parent_candidate_id") and c.get("parent_candidate_id") not in candidate_ids
        )
        roots = sorted(roots, key=lambda c: (int(c.get("_round_idx") or 0), c.get("candidate_id", "")))
        for root in roots:
            emit(root)

    text = "\n".join(lines) + "\n"
    (artifact_dir / "SIMPLE_TREE.md").write_text(text, encoding="utf-8")
    return text


def build_global_tree_summary(out_dir: Path) -> str:
    lines = ["# Global Optimization Tree Summary", ""]
    has_tree_dirs = (out_dir / "trees").is_dir() and any(p.is_dir() for p in (out_dir / "trees").iterdir())
    for artifact_dir in _artifact_dirs(out_dir):
        source = _source_label(out_dir, artifact_dir)
        if source == "." and has_tree_dirs:
            continue
        if source == ".":
            source = "workspace"
        report = build_simple_tree_report(artifact_dir, out_dir)
        body = "\n".join(report.splitlines()[2:])
        lines.append(f"## {source}")
        lines.append("")
        lines.append(body.strip() or "_No candidates found._")
        lines.append("")
    if len(lines) <= 2:
        lines.append("_No tree directories found._")
    text = "\n".join(lines).rstrip() + "\n"
    (out_dir / "GLOBAL_TREE_SUMMARY.md").write_text(text, encoding="utf-8")
    return text


def build_experiment_formula_summary(out_dir: Path) -> str:
    lines = ["# Experiment Formula And Steps Summary", ""]
    has_tree_dirs = (out_dir / "trees").is_dir() and any(p.is_dir() for p in (out_dir / "trees").iterdir())
    for artifact_dir in _artifact_dirs(out_dir):
        source = _source_label(out_dir, artifact_dir)
        if source == "." and has_tree_dirs:
            continue
        if source == ".":
            source = "workspace"
        candidates = _load_candidates(artifact_dir)
        if not candidates:
            continue
        lines.append(f"## {source}")
        lines.append("")
        for candidate in sorted(candidates, key=lambda c: (int(c.get("_round_idx") or 0), c.get("candidate_id", ""))):
            cid = candidate.get("candidate_id", "")
            lines.append(f"### {cid}")
            lines.append("")
            lines.append(f"- Parent: {candidate.get('parent_candidate_id') or 'none'}")
            lines.append(f"- Tree: {normalize_tree_label(candidate.get('tree_id') or candidate.get('root_candidate_id') or cid)}")
            lines.append(f"- Formula: {_short_formula(candidate)}")
            changed = candidate.get("changed_variable_names") or []
            lines.append(f"- Changed variables: {', '.join(changed) if changed else 'baseline/root'}")
            lines.append("")
            lines.append("Materials:")
            lines.extend(_materials_table(candidate))
            lines.append("")
            lines.append("Procedure:")
            lines.extend(_process_steps(candidate))
            lines.append("")
    if len(lines) <= 2:
        lines.append("_No tree candidate files found._")
    text = "\n".join(lines).rstrip() + "\n"
    (out_dir / "EXPERIMENT_FORMULA_SUMMARY.md").write_text(text, encoding="utf-8")
    return text


def build_tree_reports(out_dir: Path) -> Dict[str, Path]:
    """Build all human-facing tree reports for a run workspace."""
    build_global_tree_summary(out_dir)
    build_experiment_formula_summary(out_dir)
    return {
        "global_tree_summary": out_dir / "GLOBAL_TREE_SUMMARY.md",
        "experiment_formula_summary": out_dir / "EXPERIMENT_FORMULA_SUMMARY.md",
    }
