"""Human-readable reports for tree-mode optimization workspaces."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from pva_work_flow.tree.formula_tree import infer_branch_decisions
from pva_work_flow.tree.tree_naming import normalize_tree_label


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
    build_chain_report(out_dir)
    try:
        from pva_work_flow.memory.failure_factor_memory import build_failure_factor_memory

        build_failure_factor_memory(out_dir)
    except Exception:
        pass
    return {
        "global_tree_summary": out_dir / "GLOBAL_TREE_SUMMARY.md",
        "experiment_formula_summary": out_dir / "EXPERIMENT_FORMULA_SUMMARY.md",
        "chain_report": out_dir / "GREEDY_CHAINS_R2.md",
        "failure_factor_summary": out_dir / "FAILURE_FACTOR_SUMMARY.md",
        "next_verification_plan": out_dir / "NEXT_VERIFICATION_PLAN.md",
    }


def build_chain_report(out_dir: Path) -> str:
    """Generate an accurate greedy-chain report from actual DOE CSV data.

    Unlike the LLM-generated version, this reads real DOE values so there is
    no drift between what the report claims and what the experiment protocol says.
    """
    reports_dir = out_dir / "tree_reports"
    trees_dir = out_dir / "trees"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Find the latest round with DOE CSVs in trees
    tree_dirs = sorted(
        [p for p in trees_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    ) if trees_dir.exists() else []

    # Load parent COF data
    parent_cofs: dict[str, str] = {}
    r1_results = out_dir / "run_state_files" / "R1_results_filled.csv"
    if r1_results.exists():
        with r1_results.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("candidate_id") or "").strip()
                if cid:
                    parent_cofs[cid] = row.get("cof_steady_mean", "?")

    lines = [
        "# R2 Greedy Chains — code-generated from DOE CSV data",
        "",
        f"Source directory: `{out_dir.name}`",
        "",
    ]

    # Collect chain data
    chains: list[dict] = []
    for tree_dir in tree_dirs:
        doe_csv = tree_dir / "R2_doe.csv"
        r1_id = "R1-" + tree_dir.name.split("-")[1]
        parent_cof = parent_cofs.get(r1_id, "?")

        if not doe_csv.exists():
            continue

        children: list[dict] = []
        with doe_csv.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("candidate_id") or "").strip()
                if not cid:
                    continue
                pva = row.get("pva_wt_percent", "?")
                soak = row.get("post_soak_hours", "?")
                adds = (row.get("additives") or "").strip()
                children.append({"cid": cid, "pva": pva, "soak": soak, "adds": adds})

        if children:
            chains.append({
                "tree": tree_dir.name,
                "parent_id": r1_id,
                "parent_cof": parent_cof,
                "children": children,
            })

    if not chains:
        lines.append("_No R2 DOE CSV files found in tree directories._")
        text = "\n".join(lines) + "\n"
        (reports_dir / "GREEDY_CHAINS_R2.md").write_text(text, encoding="utf-8")
        return text

    # ---- Text chain diagram ----
    lines.append("## Chain Diagram (from DOE CSV)")
    lines.append("")
    lines.append("```text")

    for chain in chains:
        cof_str = f"COF={chain['parent_cof']}"
        lines.append(f"{chain['parent_id']}  [{chain['tree']}] | parent {cof_str}")
        for i, child in enumerate(chain["children"]):
            adds_str = f"additives: {child['adds']}" if child["adds"] else "⚠ additives EMPTY"
            marker = ""
            if not child["adds"]:
                marker = " ⚠"
            lines.append(f"  |")
            lines.append(f"  +-- {child['cid']} [pending] PVA={child['pva']}%  soak={child['soak']}h  {adds_str}{marker}")
        lines.append("")

    lines.append("```")

    # ---- Mermaid diagram ----
    lines.append("")
    lines.append("## Mermaid View")
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart TD")

    for chain in chains:
        node_id = chain["parent_id"].replace("-", "")
        cof_str = chain["parent_cof"]
        lines.append(f'    {node_id}["{chain["parent_id"]} [{chain["tree"]}]<br/>COF={cof_str}"]')
        for i, child in enumerate(chain["children"]):
            child_node = f"{node_id}_{i + 1}"
            has_adds = "✅" if child["adds"] else "⚠️"
            lines.append(
                f'    {node_id} --> {child_node}["{child["cid"]}<br/>'
                f'PVA={child["pva"]}% soak={child["soak"]}h<br/>{has_adds}"]'
            )
        lines.append("")

    lines.append("```")

    # ---- Summary table ----
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Tree | Parent | Parent COF | #R2 | Additives OK? |")
    lines.append("| --- | --- | ---: | ---: | --- |")
    for chain in chains:
        has_empty = any(not c["adds"] for c in chain["children"])
        status = "❌ EMPTY" if has_empty else "✅"
        lines.append(
            f"| {chain['tree']} | {chain['parent_id']} | {chain['parent_cof']} | "
            f"{len(chain['children'])} | {status} |"
        )

    text = "\n".join(lines) + "\n"
    (reports_dir / "GREEDY_CHAINS_R2.md").write_text(text, encoding="utf-8")
    return text
