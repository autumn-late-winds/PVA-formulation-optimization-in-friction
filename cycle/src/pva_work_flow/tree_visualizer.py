"""Formatted ASCII optimization-tree diagram generator.

Produces a human-friendly tree diagram (``TREE_DIAGRAM.md``) that is
automatically refreshed after every generate / diagnose / build_results
operation.  The diagram is deliberately *not* a replacement for the
machine-readable ``formula_tree.md`` or ``formula_branch_decisions.json`` —
it is an experimenter-friendly overview.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .formula_tree import infer_branch_decisions

# ══════════════════════════════════════════════════════════════════════
# Self-contained helpers (duplicated from formula_tree / tree_reports to
# avoid import failures on older deployed codebases).
# ══════════════════════════════════════════════════════════════════════

_EPSILON = 1e-6


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


def _normalize_tree_label(raw: Any) -> str:
    """Minimal label normalizer; avoids import of tree_naming."""
    if not raw:
        return "?"
    s = str(raw).strip().lower()
    if s.startswith("r1-") or s.startswith("r2-") or s.startswith("r3-"):
        return s  # it's a candidate_id, not a tree_id
    # map R1-01 → root-01 etc.
    import re
    m = re.match(r"^r(\d+)-(\d+)$", s)
    if m:
        return f"root-{int(m.group(2)):02d}"
    return s


# ── helpers ──────────────────────────────────────────────────────────

def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
        for c in obj.get("candidates", []) or []:
            if c.get("candidate_id"):
                entry = dict(c)
                entry["_round_idx"] = round_idx
                rows.append(entry)
    if not rows:
        root = _load_json(artifact_dir / "root_candidate.json")
        root_candidates = root.get("candidates") if isinstance(root.get("candidates"), list) else None
        if root_candidates:
            root = root_candidates[0] if root_candidates else {}
        if root:
            entry = dict(root)
            entry.setdefault("candidate_id", root.get("root_candidate_id") or artifact_dir.name)
            entry.setdefault("tree_id", artifact_dir.name)
            entry.setdefault("root_candidate_id", entry.get("candidate_id"))
            entry.setdefault("tree_depth", 0)
            entry["_round_idx"] = 1
            rows.append(entry)
    return rows


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def _load_notes(artifact_dir: Path) -> Dict[str, Any]:
    notes: Dict[str, Any] = {}
    for path in sorted(artifact_dir.glob("R*_experiment_notes.json"), key=_round_from_path):
        try:
            notes.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return notes


# ── label helpers ────────────────────────────────────────────────────

def _network_label(candidate: Dict[str, Any]) -> str:
    formulation = candidate.get("formulation") or {}
    method = formulation.get("crosslink_or_phys_method", "")
    net = formulation.get("network_type", "")
    if method:
        return method
    return net or "?"


def _additive_summary(candidate: Dict[str, Any]) -> str:
    adds = (candidate.get("formulation") or {}).get("additives", [])
    if not adds:
        return "无添加剂"
    parts = []
    for a in adds:
        name = str(a.get("name", "") or "")[:28]
        wt = a.get("wt_percent")
        if wt not in (None, ""):
            parts.append(f"{name} {wt}%")
        else:
            parts.append(name)
    return ", ".join(parts) if parts else "无添加剂"


def _pva_str(candidate: Dict[str, Any]) -> str:
    pva = (candidate.get("formulation") or {}).get("pva_wt_percent")
    if pva is None:
        return "PVA?%"
    return f"PVA {pva}%"


def _design_type_short(candidate: Dict[str, Any]) -> str:
    dt = str(candidate.get("design_type") or "")
    mapping = {
        "baseline_reproduction": "baseline",
        "single_factor_perturbation": "single_factor",
        "local_optimization": "local_opt",
        "limited_exploration": "exploration",
        "failure_verification": "fail_verify",
    }
    return mapping.get(dt, dt or "root")


def _changed_short(candidate: Dict[str, Any]) -> str:
    ch = candidate.get("changed_variable_names") or []
    if not ch:
        return "—"
    # abbreviate long variable paths
    short = []
    for v in ch:
        parts = v.split(".")
        short.append(parts[-1] if len(parts) > 1 else v)
    return ", ".join(short)


def _cof_emoji(cof: Optional[float], best_cof: float = 0.03) -> str:
    """Return a visual indicator for COF quality."""
    if cof is None:
        return "⬜"  # no data
    if cof < 0.01:
        return "🟢"  # excellent
    if cof < 0.02:
        return "🟡"  # good
    if cof < 0.03:
        return "🟠"  # moderate
    return "🔴"  # poor


def _status_icon(status: str) -> str:
    return {
        "root": "🌱",
        "continue": "✅",
        "rescue_candidate": "🔄",
        "kill": "❌",
        "pending": "⏳",
        "hold": "⏸️",
    }.get(status, "❓")


def _note_errors(notes: Dict[str, Any], cid: str) -> List[str]:
    entry = notes.get(cid)
    if not isinstance(entry, dict):
        return []
    errors = entry.get("error_codes", [])
    if not isinstance(errors, list):
        return []
    return [str(x) for x in errors if str(x).strip()]


# ── tree building ────────────────────────────────────────────────────

def _build_tree_structure(
    candidates: List[Dict[str, Any]],
    decisions: Dict[str, Dict[str, Any]],
    results: Dict[str, Dict[str, str]],
    notes: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Return (roots, children_of)."""
    children_of: Dict[str, List[Dict[str, Any]]] = {}
    all_ids = {c.get("candidate_id") for c in candidates if c.get("candidate_id")}

    for c in candidates:
        pid = c.get("parent_candidate_id")
        if pid and pid in all_ids:
            children_of.setdefault(pid, []).append(c)

    roots = [
        c
        for c in candidates
        if not c.get("parent_candidate_id") or c.get("parent_candidate_id") not in all_ids
    ]
    # Sort roots by candidate_id for stable order
    roots.sort(key=lambda c: str(c.get("candidate_id", "")))
    return roots, children_of


# ── rendering ────────────────────────────────────────────────────────

def _render_node(
    lines: List[str],
    candidate: Dict[str, Any],
    children_of: Dict[str, List[Dict[str, Any]]],
    decisions: Dict[str, Dict[str, Any]],
    results: Dict[str, Dict[str, str]],
    notes: Dict[str, Any],
    prefix: str = "",
    is_last: bool = False,
    is_root: bool = True,
    depth: int = 0,
    _visited: Optional[set[str]] = None,
) -> None:
    cid = candidate.get("candidate_id", "")
    if _visited is None:
        _visited = set()
    cycle_detected = bool(cid and cid in _visited)

    decision = decisions.get(cid, {})
    status = decision.get("branch_status") or candidate.get("branch_status") or "pending"
    result = results.get(cid, {})

    cof_raw = result.get("cof_steady_mean") or result.get("COF_mean_1")
    cof = _float_or_none(cof_raw)
    modulus = result.get("compression_modulus_MPa", "")
    pattern = result.get("friction_pattern", "")
    errors = _note_errors(notes, cid)

    # Build the node label
    parts: List[str] = []

    # Status icon
    parts.append(_status_icon(status))

    # Candidate ID + round
    r = candidate.get("_round_idx", "?")
    parts.append(f"**{cid}** (R{r})")

    # Design type
    dt = _design_type_short(candidate)
    parts.append(f"[{dt}]")

    # Changed variables
    ch = _changed_short(candidate)
    if depth > 0:
        parts.append(f"Δ: {ch}")

    # PVA + network
    pva = _pva_str(candidate)
    net = _network_label(candidate)
    parts.append(f"{pva} | {net}")

    # COF
    if cof is not None:
        icon = _cof_emoji(cof)
        parts.append(f"COF={cof:.4f} {icon}")
        if modulus and modulus not in ("", "None"):
            parts.append(f"E={modulus} MPa")

    # Pattern
    if pattern:
        parts.append(f"[{pattern}]")

    # Errors
    if errors:
        parts.append(f"!!{','.join(errors)}")

    # Status label
    if status not in ("root", "continue"):
        parts.append(f"<{status}>")

    if cycle_detected:
        parts.append("<cycle_detected>")

    # Additive summary (root level only, to keep it clean)
    if depth == 0:
        adds = _additive_summary(candidate)
        # Truncate long additive lists
        if len(adds) > 60:
            adds = adds[:57] + "..."
        parts.append(f"┃ {adds}")

    line = prefix
    if not is_root:
        branch = "└── " if is_last else "├── "
        line += branch
    lines.append(line + " ".join(parts))

    if cycle_detected:
        return

    next_visited = set(_visited)
    if cid:
        next_visited.add(cid)

    # Render children
    children = children_of.get(cid, [])
    if children:
        children.sort(key=lambda c: str(c.get("candidate_id", "")))
        for i, child in enumerate(children):
            child_is_last = (i == len(children) - 1)
            if is_root:
                child_prefix = "    " if child_is_last else "│   "
            else:
                child_prefix = prefix + ("    " if is_last else "│   ")
            _render_node(
                lines, child, children_of, decisions, results, notes,
                prefix=child_prefix,
                is_last=child_is_last,
                is_root=False,
                depth=depth + 1,
                _visited=next_visited,
            )


def _render_round_summary(
    lines: List[str],
    all_candidates: List[Dict[str, Any]],
    results: Dict[str, Dict[str, str]],
) -> None:
    """Append a per-round COF ranking table."""
    by_round: Dict[int, List[Dict[str, Any]]] = {}
    for c in all_candidates:
        r = c.get("_round_idx", 0)
        by_round.setdefault(r, []).append(c)

    for r in sorted(by_round):
        round_cands = by_round[r]
        scored = []
        for c in round_cands:
            cid = c.get("candidate_id", "")
            res = results.get(cid, {})
            cof = _float_or_none(res.get("cof_steady_mean") or res.get("COF_mean_1"))
            modulus = res.get("compression_modulus_MPa", "")
            pattern = res.get("friction_pattern", "")
            if cof is not None:
                scored.append((cof, pattern, modulus, cid))
        if not scored:
            continue

        scored.sort(key=lambda x: x[0])
        lines.append(f"### R{r} COF 排名")
        lines.append("")
        lines.append("| 排名 | 配方 | COF | 模量 (MPa) | 摩擦模式 |")
        lines.append("|------|------|-----|-----------|----------|")
        for rank, (cof, pattern, mod, cid) in enumerate(scored, 1):
            icon = _cof_emoji(cof)
            mod_str = f"{mod}" if mod else "—"
            pat_str = pattern if pattern else "—"
            lines.append(f"| {rank} | {cid} | {cof:.4f} {icon} | {mod_str} | {pat_str} |")
        lines.append("")


# ── public API ───────────────────────────────────────────────────────

def build_tree_diagram(out_dir: Path) -> str:
    """Build a formatted tree diagram for the entire workspace.

    Writes ``TREE_DIAGRAM.md`` into *out_dir* and returns its content.
    """
    lines: List[str] = []
    lines.append("# PVA 水凝胶配方优化树")
    lines.append("")
    lines.append(f"> 自动生成于 `{out_dir.name}` — 每次 generate / diagnose / build_results 后自动刷新")
    lines.append("")

    artifact_dirs = _artifact_dirs(out_dir)
    has_tree_dirs = (
        (out_dir / "trees").is_dir()
        and any(p.is_dir() for p in (out_dir / "trees").iterdir())
    )

    # Load root-level results as fallback (tree subdirs may not have their own)
    root_results: Dict[str, Dict[str, str]] = _load_results(out_dir)
    root_notes: Dict[str, Any] = _load_notes(out_dir)

    # ── Per-tree rendering ──
    for artifact_dir in artifact_dirs:
        source = _source_label(out_dir, artifact_dir)
        if source == "." and has_tree_dirs:
            continue

        candidates = _load_candidates(artifact_dir)
        if not candidates:
            continue

        decisions = infer_branch_decisions(artifact_dir, write=True)
        # Per-tree results take precedence; fall back to root results
        results = _load_results(artifact_dir)
        if not results:
            results = root_results
        notes = _load_notes(artifact_dir)
        if not notes:
            notes = root_notes

        roots, children_of = _build_tree_structure(candidates, decisions, results, notes)
        if not roots:
            continue

        label = artifact_dir.name if has_tree_dirs else "workspace"
        lines.append(f"## 🌲 {label}")
        lines.append("")

        for i, root in enumerate(roots):
            _render_node(
                lines, root, children_of, decisions, results, notes,
                prefix="",
                is_last=(i == len(roots) - 1),
                is_root=True,
                depth=0,
            )

        lines.append("")

    # ── Cross-tree summary ──
    if has_tree_dirs:
        lines.append("---")
        lines.append("")
        lines.append("## 📊 跨树汇总")
        lines.append("")

        # Collect all candidates from all trees
        all_candidates = []
        all_results: Dict[str, Dict[str, str]] = dict(root_results)
        for artifact_dir in artifact_dirs:
            source = _source_label(out_dir, artifact_dir)
            if source == ".":
                continue
            all_candidates.extend(_load_candidates(artifact_dir))
            # Per-tree results override root-level
            all_results.update(_load_results(artifact_dir))

        _render_round_summary(lines, all_candidates, all_results)

        # Tree comparison table
        lines.append("### 各树最佳 COF 对比")
        lines.append("")
        lines.append("| Tree | Root ID | 最佳 COF | 网络类型 | 添加剂体系 | 节点数 |")
        lines.append("|------|---------|----------|----------|-----------|--------|")

        for artifact_dir in artifact_dirs:
            source = _source_label(out_dir, artifact_dir)
            if source == ".":
                continue
            candidates = _load_candidates(artifact_dir)
            results = _load_results(artifact_dir)
            if not results:
                results = root_results
            if not candidates:
                continue

            # Find root and best COF
            root = next((c for c in candidates if not c.get("parent_candidate_id")), candidates[0])
            root_id = root.get("candidate_id", "?")
            net = _network_label(root)
            adds = _additive_summary(root)
            if len(adds) > 40:
                adds = adds[:37] + "..."

            best_cof = None
            for c in candidates:
                cid = c.get("candidate_id", "")
                res = results.get(cid, {})
                cof = _float_or_none(res.get("cof_steady_mean") or res.get("COF_mean_1"))
                if cof is not None and (best_cof is None or cof < best_cof):
                    best_cof = cof
                # Also check children results via candidate_id
            best_str = f"{best_cof:.4f}" if best_cof is not None else "—"
            lines.append(f"| {source} | {root_id} | {best_str} | {net} | {adds} | {len(candidates)} |")

        lines.append("")

        # Legend
        lines.append("### 图例")
        lines.append("")
        lines.append("| 符号 | 含义 |")
        lines.append("|------|------|")
        lines.append("| 🌱 root | 初始根配方 |")
        lines.append("| ✅ continue | 分支改善，可继续展开 |")
        lines.append("| 🔄 rescue_candidate | 分支恶化，等待定向修复 |")
        lines.append("| ❌ kill | 分支终止 |")
        lines.append("| ⏳ pending | 等待实验结果 |")
        lines.append("| 🟢 COF<0.01 | 极低摩擦 |")
        lines.append("| 🟡 COF<0.02 | 良好 |")
        lines.append("| 🟠 COF<0.03 | 一般 |")
        lines.append("| 🔴 COF≥0.03 | 较差 |")
        lines.append("| ⬜ 无数据 | 实验未完成 |")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    (out_dir / "TREE_DIAGRAM.md").write_text(text, encoding="utf-8")
    return text
