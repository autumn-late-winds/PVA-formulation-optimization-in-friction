"""Automatic formula optimization tree generator.

The tree is both human-readable (`formula_tree.md`) and machine-readable
(`formula_branch_decisions.json`).  The decision layer mirrors the wet-lab
workflow: improved branches continue, worsened branches get one rescue chance,
and failed rescue attempts are killed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tree_naming import normalize_tree_label


ACTIVE_STATUSES = {"", "active", "auto", "pending", "not_measured"}
CONTINUE_STATUSES = {"continue", "promoted", "keep", "kept"}
RESCUE_STATUSES = {"rescue", "rescue_candidate", "rescued"}
KILL_STATUSES = {"kill", "killed", "stop", "stopped", "dead"}
EPSILON_COF = 1e-6


def _artifact_dirs(out_dir: Path) -> List[Path]:
    trees_dir = out_dir / "trees"
    if not trees_dir.exists():
        return [out_dir]
    tree_dirs = sorted(p for p in trees_dir.iterdir() if p.is_dir())
    return tree_dirs or [out_dir]


def _source_label(out_dir: Path, artifact_dir: Path) -> str:
    try:
        rel = artifact_dir.relative_to(out_dir)
    except ValueError:
        rel = artifact_dir
    label = str(rel).replace("\\", "/")
    return "." if label == "." else label


def _load_round_candidates(out_dir: Path, round_idx: int) -> List[Dict[str, Any]]:
    path = out_dir / f"R{round_idx}_candidates.json"
    if not path.exists():
        return []
    obj = json.loads(path.read_text(encoding="utf-8"))
    return obj.get("candidates", [])


def _load_results(out_dir: Path, round_idx: int) -> Dict[str, Dict[str, str]]:
    path = out_dir / f"R{round_idx}_results_filled.csv"
    result: Dict[str, Dict[str, str]] = {}
    if not path.exists():
        return result
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("candidate_id") or "").strip()
                if cid:
                    result[cid] = {
                        "cof": row.get("cof_steady_mean", ""),
                        "pattern": row.get("friction_pattern", ""),
                    }
    except Exception:
        pass
    return result


def _load_notes(out_dir: Path, round_idx: int) -> Dict[str, Any]:
    path = out_dir / f"R{round_idx}_experiment_notes.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _short_adds(candidate: Dict[str, Any]) -> str:
    adds = candidate.get("formulation", {}).get("additives", [])
    if not adds:
        return "none"
    parts = []
    for a in adds:
        name = (a.get("name") or "")[:30]
        wt = a.get("wt_percent", "?")
        parts.append(f"{name}:{wt}%")
    return ", ".join(parts)


def _short_network(candidate: Dict[str, Any]) -> str:
    return candidate.get("formulation", {}).get("crosslink_or_phys_method", "?")


def _short_pva(candidate: Dict[str, Any]) -> str:
    pva = candidate.get("formulation", {}).get("pva_wt_percent")
    return f"{pva}%" if pva is not None else "?%"


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _note_errors(notes: Dict[str, Any], cid: str) -> List[str]:
    note_entry = notes.get(cid)
    if not isinstance(note_entry, dict):
        return []
    errors = note_entry.get("error_codes", [])
    if not isinstance(errors, list):
        return []
    return [str(x) for x in errors if str(x).strip()]


def _manual_status(candidate: Dict[str, Any]) -> str:
    status = str(candidate.get("branch_status") or "").strip().lower()
    if status in CONTINUE_STATUSES:
        return "continue"
    if status in RESCUE_STATUSES:
        return "rescue_candidate"
    if status in KILL_STATUSES:
        return "kill"
    if status in ACTIVE_STATUSES:
        return ""
    return status


def _is_rescue_attempt(
    candidate: Dict[str, Any],
    parent_decision: Dict[str, Any] | None,
) -> bool:
    design_type = str(candidate.get("design_type") or "").lower()
    rationale = str(candidate.get("mutation_rationale") or "").lower()
    explicit = str(candidate.get("branch_status") or "").lower()
    parent_status = str((parent_decision or {}).get("branch_status") or "").lower()
    return (
        "rescue" in design_type
        or design_type == "failure_verification"
        or "rescue" in rationale
        or explicit in RESCUE_STATUSES
        or parent_status in {"rescue_candidate", "rescue"}
    )


def _candidate_decision(
    candidate: Dict[str, Any],
    results: Dict[str, Dict[str, str]],
    notes: Dict[str, Any],
    parent_decision: Dict[str, Any] | None,
    children_count: int,
) -> Dict[str, Any]:
    cid = candidate.get("candidate_id", "")
    pid = candidate.get("parent_candidate_id")
    tree_id = normalize_tree_label(candidate.get("tree_id") or candidate.get("root_candidate_id") or cid)
    manual = _manual_status(candidate)

    cof = _float_or_none((results.get(cid) or {}).get("cof"))
    parent_cof = _float_or_none((results.get(pid) or {}).get("cof")) if pid else None
    delta = cof - parent_cof if cof is not None and parent_cof is not None else None
    errors = _note_errors(notes, cid)
    rescue_attempt = _is_rescue_attempt(candidate, parent_decision)

    if manual:
        status = manual
        action = {
            "continue": "continue_branch",
            "rescue_candidate": "rescue_once",
            "kill": "kill_branch",
        }.get(status, status)
        reason = f"manual branch_status={candidate.get('branch_status')}"
    elif not pid:
        status = "root"
        action = "start_tree" if children_count else "await_first_branch"
        reason = "root formula"
    elif errors:
        if rescue_attempt:
            status = "kill"
            action = "kill_branch"
            reason = "rescue attempt still has experimental error"
        else:
            status = "rescue_candidate"
            action = "rescue_once"
            reason = "experimental error; allow one targeted rescue"
    elif delta is None:
        status = "pending"
        action = "await_result"
        reason = "missing child or parent COF"
    elif delta < -EPSILON_COF:
        status = "continue"
        action = "continue_branch"
        reason = "COF improved versus parent"
    elif delta > EPSILON_COF:
        if rescue_attempt:
            status = "kill"
            action = "kill_branch"
            reason = "rescue attempt worsened versus parent"
        else:
            status = "rescue_candidate"
            action = "rescue_once"
            reason = "COF worsened versus parent"
    else:
        status = "hold"
        action = "repeat_or_hold"
        reason = "COF unchanged within epsilon"

    return {
        "candidate_id": cid,
        "tree_id": tree_id,
        "node_id": candidate.get("node_id") or cid,
        "parent_candidate_id": pid,
        "parent_node_id": candidate.get("parent_node_id") or pid,
        "tree_depth": candidate.get("tree_depth", 0),
        "branch_status": status,
        "action": action,
        "reason": reason,
        "cof": cof,
        "parent_cof": parent_cof,
        "delta_cof": delta,
        "rescue_attempt": rescue_attempt,
        "errors": errors,
        "children_count": children_count,
    }


def _collect_tree_inputs(
    out_dir: Path,
) -> tuple[
    Dict[int, List[Dict[str, Any]]],
    Dict[str, Dict[str, Any]],
    Dict[str, int],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, Dict[str, str]],
    Dict[str, Any],
]:
    rounds: Dict[int, List[Dict[str, Any]]] = {}
    for p in sorted(out_dir.glob("R*_candidates.json")):
        try:
            r = int(p.stem.split("_")[0][1:])
        except (ValueError, IndexError):
            continue
        rounds[r] = _load_round_candidates(out_dir, r)

    children_of: Dict[str, List[Dict[str, Any]]] = {}
    all_candidates: Dict[str, Dict[str, Any]] = {}
    candidate_round: Dict[str, int] = {}
    for r in sorted(rounds):
        for c in rounds[r]:
            cid = c.get("candidate_id", "")
            if not cid:
                continue
            all_candidates[cid] = c
            candidate_round[cid] = r
            pid = c.get("parent_candidate_id")
            if pid:
                children_of.setdefault(pid, []).append(c)

    results: Dict[str, Dict[str, str]] = {}
    notes: Dict[str, Any] = {}
    for r in rounds:
        results.update(_load_results(out_dir, r))
        notes.update(_load_notes(out_dir, r))

    return rounds, all_candidates, candidate_round, children_of, results, notes


def infer_branch_decisions(out_dir: Path, write: bool = False) -> Dict[str, Dict[str, Any]]:
    """Infer branch decisions for every node in a run workspace."""
    rounds, all_candidates, _candidate_round, children_of, results, notes = _collect_tree_inputs(out_dir)
    if not rounds:
        return {}

    decisions: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(
        all_candidates.values(),
        key=lambda c: (int(c.get("tree_depth") or 0), str(c.get("candidate_id") or "")),
    )
    for c in ordered:
        cid = c.get("candidate_id", "")
        pid = c.get("parent_candidate_id")
        parent_decision = decisions.get(pid) if pid else None
        decisions[cid] = _candidate_decision(
            c,
            results,
            notes,
            parent_decision,
            len(children_of.get(cid, [])),
        )

    if write:
        payload = {"decisions": [decisions[k] for k in sorted(decisions)]}
        (out_dir / "formula_branch_decisions.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return decisions


def _render_tree_lines(out_dir: Path) -> List[str]:
    rounds, all_candidates, candidate_round, children_of, results, notes = _collect_tree_inputs(out_dir)
    if not rounds:
        return ["(no candidates yet)"]

    decisions = infer_branch_decisions(out_dir, write=True)
    roots = [c for c in all_candidates.values() if not c.get("parent_candidate_id")]
    lines: List[str] = []

    def _candidate_line(cid: str, indent: str, is_last: bool) -> None:
        c = all_candidates.get(cid)
        if not c:
            return

        branch = "`-" if is_last else "|-"
        pva = _short_pva(c)
        net = _short_network(c)
        adds = _short_adds(c)
        tree_id = normalize_tree_label(c.get("tree_id") or c.get("root_candidate_id") or cid)
        decision = decisions.get(cid, {})
        status = decision.get("branch_status") or c.get("branch_status") or "pending"
        action = decision.get("action") or "await_result"
        reason = decision.get("reason") or ""

        res = results.get(cid, {})
        cof = res.get("cof", "")
        cof_str = f" COF={cof}" if cof else ""
        pattern = res.get("pattern", "")
        pattern_str = f" [{pattern}]" if pattern else ""
        delta = decision.get("delta_cof")
        delta_str = f" dCOF={delta:+.4f}" if isinstance(delta, (int, float)) else ""

        errors = _note_errors(notes, cid)
        err_str = f" !!{','.join(errors)}" if errors else ""
        children = children_of.get(cid, [])
        child_str = f" -> {len(children)} children" if children else ""
        ch = c.get("changed_variable_names", [])
        dt = c.get("design_type", "?")
        ch_str = ", ".join(ch) if ch else "root/no change"
        r = candidate_round.get(cid, "?")

        lines.append(
            f"{indent}{branch} {cid} R{r} | tree={tree_id} | status={status} | action={action} | "
            f"{dt}: {ch_str} | PVA{pva} | {net} | {adds}{cof_str}{delta_str}"
            f"{pattern_str}{err_str} | reason={reason}{child_str}"
        )

        for i, child in enumerate(children):
            child_cid = child.get("candidate_id", "")
            child_indent = indent + ("   " if is_last else "|  ")
            _candidate_line(child_cid, child_indent, i == len(children) - 1)

    for i, root in enumerate(roots):
        cid = root.get("candidate_id", "")
        _candidate_line(cid, "", i == len(roots) - 1)

    return lines


def build_tree(out_dir: Path) -> str:
    """Build a markdown inheritance tree for all rounds in the workspace.

    If <out_dir>/trees/root-* exists, the root report is a global index that
    renders each tree directory independently. This keeps local node IDs such
    as R2-01 from different trees from overwriting each other.
    """
    lines: List[str] = []
    lines.append("# PVA Hydrogel Formula Optimization Tree")
    lines.append("")
    lines.append("```")

    artifact_dirs = _artifact_dirs(out_dir)
    if len(artifact_dirs) == 1 and artifact_dirs[0] == out_dir:
        lines.extend(_render_tree_lines(out_dir))
    else:
        aggregate_decisions: List[Dict[str, Any]] = []
        rendered_any = False
        for artifact_dir in artifact_dirs:
            section_lines = _render_tree_lines(artifact_dir)
            if section_lines == ["(no candidates yet)"]:
                continue
            rendered_any = True
            source = _source_label(out_dir, artifact_dir)
            lines.append(f"== {source} ==")
            lines.extend(section_lines)
            for decision in infer_branch_decisions(artifact_dir, write=True).values():
                row = dict(decision)
                row["artifact_source"] = source
                aggregate_decisions.append(row)
        if not rendered_any:
            lines.append("(no candidates yet)")
        (out_dir / "formula_branch_decisions.json").write_text(
            json.dumps({"decisions": aggregate_decisions}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    lines.append("```")
    text = "\n".join(lines)
    (out_dir / "formula_tree.md").write_text(text, encoding="utf-8")
    return text
