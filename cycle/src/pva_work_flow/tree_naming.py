"""Helpers that keep tree labels separate from candidate IDs."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT_LABEL_RE = re.compile(r"^root-(\d+)$")
R1_CANDIDATE_RE = re.compile(r"^R1-(\d+)$")


def root_label_from_candidate_id(candidate_id: str | None) -> str | None:
    """Convert an R1 candidate ID to a display/storage tree label."""
    cid = (candidate_id or "").strip()
    match = R1_CANDIDATE_RE.match(cid)
    if not match:
        return None
    return f"root-{int(match.group(1)):02d}"


def candidate_id_from_root_label(label: str | None) -> str | None:
    """Convert a root-* tree label back to the root candidate ID."""
    value = (label or "").strip()
    match = ROOT_LABEL_RE.match(value)
    if not match:
        return None
    return f"R1-{int(match.group(1)):02d}"


def normalize_tree_label(value: str | None) -> str:
    """Return the canonical tree label without changing candidate IDs elsewhere."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if ROOT_LABEL_RE.match(raw):
        return f"root-{int(ROOT_LABEL_RE.match(raw).group(1)):02d}"
    return root_label_from_candidate_id(raw) or raw


def resolve_target_parent_id(value: str | None) -> str | None:
    """Accept root-* convenience input, but keep internal parent IDs as R*-*."""
    raw = (value or "").strip()
    if not raw:
        return None
    return candidate_id_from_root_label(raw) or raw


def _candidate_file_contains(path: Path, candidate_id: str) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    for candidate in obj.get("candidates", []) or []:
        if candidate.get("candidate_id") == candidate_id:
            return True
    return False


def find_tree_dir_for_parent(out_dir: Path, parent_candidate_id: str) -> Path | None:
    """Find the per-tree artifact directory for a target parent candidate.

    New trees use root-* directory names. Legacy R1-* directories remain
    readable so existing workspaces do not break.
    """
    trees_dir = out_dir / "trees"
    if not trees_dir.is_dir():
        return None

    root_label = root_label_from_candidate_id(parent_candidate_id)
    if root_label:
        for dirname in (root_label, parent_candidate_id):
            candidate = trees_dir / dirname
            if candidate.is_dir():
                return candidate

    for tree_dir in sorted(p for p in trees_dir.iterdir() if p.is_dir()):
        for candidates_path in tree_dir.glob("R*_candidates.json"):
            if _candidate_file_contains(candidates_path, parent_candidate_id):
                return tree_dir
    return None
