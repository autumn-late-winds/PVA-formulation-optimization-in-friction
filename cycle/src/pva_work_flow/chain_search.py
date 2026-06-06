"""Greedy chain-search parent selection for wet-lab comparison runs.

The normal project workflow keeps full trees. Chain search is a thin selection
policy on top of those artifacts: from one root, accept the best improving child
as the next parent; if no child improves, keep the current parent and try a new
single-step perturbation next round.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_round(path: Path) -> int | None:
    try:
        return int(path.stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return None


def _tree_dir_for_root(out_dir: Path, root_id: str | None) -> Path | None:
    if not root_id:
        return None
    if root_id.startswith("R1-"):
        suffix = root_id.split("-", 1)[1]
        label = f"root-{int(suffix):02d}" if suffix.isdigit() else ""
    elif root_id.startswith("root-"):
        suffix = root_id.split("-", 1)[1]
        label = f"root-{int(suffix):02d}" if suffix.isdigit() else root_id
    else:
        label = ""
    tree_dir = out_dir / "trees" / label if label else None
    return tree_dir if tree_dir and tree_dir.is_dir() else None


def _artifact_dirs(out_dir: Path, root_id: str | None = None) -> list[Path]:
    dirs: list[Path] = []
    state_dir = out_dir / "run_state_files"
    if state_dir.is_dir():
        dirs.append(state_dir)
    if out_dir.is_dir():
        dirs.append(out_dir)
    tree_dir = _tree_dir_for_root(out_dir, root_id)
    if tree_dir:
        dirs.append(tree_dir)
    return dirs


def _candidate_paths(out_dir: Path, root_id: str | None = None) -> list[Path]:
    paths: list[Path] = []
    for artifact_dir in _artifact_dirs(out_dir, root_id):
        paths.extend(sorted(artifact_dir.glob("R*_candidates.json")))
    return paths


def _results_paths(out_dir: Path, root_id: str | None = None) -> list[Path]:
    paths: list[Path] = []
    for artifact_dir in _artifact_dirs(out_dir, root_id):
        paths.extend(sorted(artifact_dir.glob("R*_results_filled.csv")))
    return paths


def _load_candidates(out_dir: Path, root_id: str | None = None) -> tuple[dict[str, dict], dict[str, int], dict[str, list[str]]]:
    by_id: dict[str, dict] = {}
    round_by_id: dict[str, int] = {}
    children: dict[str, list[str]] = {}
    for path in _candidate_paths(out_dir, root_id):
        round_idx = _candidate_round(path)
        if round_idx is None:
            continue
        obj = _read_json(path)
        for candidate in obj.get("candidates", []) or []:
            cid = candidate.get("candidate_id")
            if not cid:
                continue
            by_id[cid] = candidate
            round_by_id[cid] = round_idx
            parent_id = candidate.get("parent_candidate_id")
            if parent_id:
                children.setdefault(parent_id, []).append(cid)
    return by_id, round_by_id, children


def _load_cof(out_dir: Path, root_id: str | None = None) -> dict[str, float]:
    cofs: dict[str, float] = {}
    for path in _results_paths(out_dir, root_id):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                cid = (row.get("candidate_id") or "").strip()
                raw = (row.get("cof_steady_mean") or "").strip()
                if not cid or not raw:
                    continue
                try:
                    cofs[cid] = float(raw)
                except ValueError:
                    continue
    return cofs


def _choose_default_root(round_by_id: dict[str, int], cofs: dict[str, float]) -> str | None:
    roots = sorted(cid for cid, round_idx in round_by_id.items() if round_idx == 1)
    measured = [cid for cid in roots if cid in cofs]
    if measured:
        return min(measured, key=lambda cid: cofs[cid])
    return roots[0] if roots else None


def resolve_chain_parent(
    out_dir: Path,
    root_id: str | None = None,
    accept_delta: float = -1e-6,
) -> dict[str, Any]:
    """Return the current greedy-chain parent and a trace of accept/reject steps."""
    by_id, round_by_id, children = _load_candidates(out_dir, root_id=root_id)
    cofs = _load_cof(out_dir, root_id=root_id)
    root = root_id or _choose_default_root(round_by_id, cofs)
    if not root:
        raise RuntimeError("Chain search needs R1 candidates before it can select a parent.")
    if root not in by_id:
        raise RuntimeError(f"Chain root not found in candidates: {root}")

    current = root
    trace: list[dict[str, Any]] = []
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        parent_cof = cofs.get(current)
        measured_children = [
            cid for cid in children.get(current, [])
            if cid in cofs
        ]
        if parent_cof is None or not measured_children:
            break
        best_child = min(measured_children, key=lambda cid: cofs[cid])
        delta = cofs[best_child] - parent_cof
        accepted = delta < accept_delta
        trace.append({
            "parent_id": current,
            "parent_cof": parent_cof,
            "best_child_id": best_child,
            "best_child_cof": cofs[best_child],
            "delta_cof": round(delta, 6),
            "decision": "accept" if accepted else "reject_and_retry_parent",
        })
        if not accepted:
            break
        current = best_child

    return {
        "root_id": root,
        "current_parent_id": current,
        "current_parent_round": round_by_id.get(current),
        "current_parent_cof": cofs.get(current),
        "accept_delta": accept_delta,
        "trace": trace,
    }


def write_chain_state(out_dir: Path, state: dict[str, Any]) -> Path:
    path = out_dir / "chain_search_state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
