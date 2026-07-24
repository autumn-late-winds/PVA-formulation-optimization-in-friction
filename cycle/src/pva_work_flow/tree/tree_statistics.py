"""Cross-tree statistical memory for formula optimization.

Each tree still expands one parent node at a time.  This module shares
statistical knowledge across trees so the fifth tree can learn from the first
four without mixing their parent lineages.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List

from pva_work_flow.tree.formula_tree import infer_branch_decisions
from pva_work_flow.tree.tree_naming import normalize_tree_label
from pva_work_flow.wetlab.wetlab_outcomes import compute_cvs, has_failure


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _artifact_dirs(out_dir: Path) -> List[Path]:
    """Return run directories that may contain tree artifacts.

    The root run directory supports the original flat layout.  When the CLI
    stores one root tree per subdirectory, the global statistics must also read
    out_dir/trees/* so later trees can learn from earlier trees.
    """
    dirs = [out_dir]
    trees_dir = out_dir / "trees"
    if trees_dir.exists():
        dirs.extend(sorted(p for p in trees_dir.iterdir() if p.is_dir()))
    return dirs


def _source_label(out_dir: Path, artifact_dir: Path) -> str:
    try:
        rel = artifact_dir.relative_to(out_dir)
    except ValueError:
        rel = artifact_dir
    label = str(rel).replace("\\", "/")
    return "." if label == "." else label


def _load_candidate_records(out_dir: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for artifact_dir in _artifact_dirs(out_dir):
        source = _source_label(out_dir, artifact_dir)
        for p in sorted(artifact_dir.glob("R*_candidates.json")):
            obj = _load_json(p)
            for c in obj.get("candidates", []) or []:
                cid = c.get("candidate_id")
                if cid:
                    records.append({"source_dir": artifact_dir, "source": source, "candidate": c})
    return records


def _load_candidates(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for p in sorted(out_dir.glob("R*_candidates.json")):
        obj = _load_json(p)
        for c in obj.get("candidates", []) or []:
            cid = c.get("candidate_id")
            if cid:
                candidates[cid] = c
    return candidates


def _load_results(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    for p in sorted(out_dir.glob("R*_results_filled.csv")):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    cid = (row.get("candidate_id") or "").strip()
                    if cid:
                        results[cid] = row
        except Exception:
            continue
    return results


def _load_results_by_dir(out_dir: Path) -> Dict[Path, Dict[str, Dict[str, Any]]]:
    return {artifact_dir: _load_results(artifact_dir) for artifact_dir in _artifact_dirs(out_dir)}


def _infer_decisions_by_dir(out_dir: Path) -> Dict[Path, Dict[str, Dict[str, Any]]]:
    decisions_by_dir: Dict[Path, Dict[str, Dict[str, Any]]] = {}
    for artifact_dir in _artifact_dirs(out_dir):
        decisions_by_dir[artifact_dir] = infer_branch_decisions(artifact_dir, write=True)
    return decisions_by_dir


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _changed_names(candidate: Dict[str, Any]) -> List[str]:
    names = candidate.get("changed_variable_names") or []
    if isinstance(names, list) and names:
        return [str(x) for x in names if str(x).strip()]
    changes = candidate.get("changed_variables") or []
    out: List[str] = []
    if isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict) and item.get("variable"):
                out.append(str(item["variable"]))
            elif isinstance(item, str):
                out.append(item)
    return out


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _summarize_variable_stats(rows: list[dict]) -> list[dict]:
    by_var: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for var in row["changed_variables"]:
            by_var[var].append(row)

    summaries: list[dict] = []
    for var, items in sorted(by_var.items()):
        measured = [x for x in items if x.get("delta_cof") is not None]
        deltas = [float(x["delta_cof"]) for x in measured]
        improved = [x for x in measured if float(x["delta_cof"]) < 0]
        worsened = [x for x in measured if float(x["delta_cof"]) > 0]
        killed = [x for x in items if x.get("branch_status") == "kill"]
        rescue = [x for x in items if x.get("branch_status") == "rescue_candidate"]
        summaries.append(
            {
                "variable": var,
                "n": len(items),
                "measured_n": len(measured),
                "improved_n": len(improved),
                "worsened_n": len(worsened),
                "rescue_candidate_n": len(rescue),
                "kill_n": len(killed),
                "improvement_rate": _rate(len(improved), len(measured)),
                "kill_rate": _rate(len(killed), len(items)),
                "mean_delta_cof": round(mean(deltas), 6) if deltas else None,
                "best_delta_cof": round(min(deltas), 6) if deltas else None,
            }
        )
    summaries.sort(
        key=lambda x: (
            -(x["improvement_rate"] or 0),
            x["mean_delta_cof"] if x["mean_delta_cof"] is not None else 999,
            -x["n"],
        )
    )
    return summaries


def _summarize_tree_stats(rows: list[dict]) -> list[dict]:
    by_tree: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_tree[row["tree_id"]].append(row)

    summaries: list[dict] = []
    for tree_id, items in sorted(by_tree.items()):
        cofs = [x["cof"] for x in items if x.get("cof") is not None]
        measured = [x for x in items if x.get("cof") is not None]
        valid_measured = [x for x in measured if not x.get("experimental_failed")]
        valid_cofs = [x["cof"] for x in valid_measured]
        cvs_rows = [x for x in items if x.get("cvs") is not None]
        best_cvs_row = max(cvs_rows, key=lambda x: x.get("cvs") or 0.0) if cvs_rows else None
        summaries.append(
            {
                "tree_id": tree_id,
                "nodes": len(items),
                "measured_n": len(measured),
                "valid_measured_n": len(valid_measured),
                "best_cof": round(min(cofs), 6) if cofs else None,
                "best_valid_cof": round(min(valid_cofs), 6) if valid_cofs else None,
                "best_cvs": best_cvs_row.get("cvs") if best_cvs_row else None,
                "best_cvs_candidate": best_cvs_row.get("candidate_id") if best_cvs_row else None,
                "best_cvs_grade": best_cvs_row.get("cvs_grade") if best_cvs_row else None,
                "median_like_cof": round(sorted(cofs)[len(cofs) // 2], 6) if cofs else None,
                "continue_n": sum(1 for x in items if x.get("branch_status") == "continue"),
                "rescue_candidate_n": sum(1 for x in items if x.get("branch_status") == "rescue_candidate"),
                "kill_n": sum(1 for x in items if x.get("branch_status") == "kill"),
            }
        )
    summaries.sort(
        key=lambda x: (
            -(x["best_cvs"] or 0.0),
            x["best_valid_cof"] if x["best_valid_cof"] is not None else 999,
            x["best_cof"] if x["best_cof"] is not None else 999,
        )
    )
    return summaries


def build_tree_statistics(out_dir: Path) -> Dict[str, Any]:
    """Build cross-tree statistics and write json/md/jsonl artifacts."""
    candidate_records = _load_candidate_records(out_dir)
    results_by_dir = _load_results_by_dir(out_dir)
    decisions_by_dir = _infer_decisions_by_dir(out_dir)

    rows: list[dict] = []
    for record in sorted(
        candidate_records,
        key=lambda x: (x["source"], str(x["candidate"].get("candidate_id") or "")),
    ):
        artifact_dir = record["source_dir"]
        source = record["source"]
        c = record["candidate"]
        cid = c.get("candidate_id")
        decision = decisions_by_dir.get(artifact_dir, {}).get(cid, {})
        result = results_by_dir.get(artifact_dir, {}).get(cid, {})
        cof = _float_or_none(result.get("cof_steady_mean"))
        cvs_result = compute_cvs(result, error_codes=decision.get("errors") or None) if result else {}
        row = {
            "artifact_source": source,
            "candidate_id": cid,
            "parent_candidate_id": c.get("parent_candidate_id"),
            "tree_id": normalize_tree_label(c.get("tree_id") or c.get("root_candidate_id") or cid),
            "tree_label": normalize_tree_label(c.get("tree_label") or c.get("tree_id") or c.get("root_candidate_id") or cid),
            "root_candidate_id": c.get("root_candidate_id") or cid,
            "tree_depth": c.get("tree_depth", 0),
            "design_type": c.get("design_type"),
            "changed_variables": _changed_names(c),
            "branch_status": decision.get("branch_status") or c.get("branch_status"),
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "cof": cof,
            "experimental_failed": has_failure(result) if result else False,
            "cvs": cvs_result.get("cvs") if cvs_result else None,
            "cvs_grade": cvs_result.get("grade") if cvs_result else None,
            "cvs_i_multiplier": cvs_result.get("i_multiplier") if cvs_result else None,
            "delta_cof": decision.get("delta_cof"),
            "rescue_attempt": decision.get("rescue_attempt", False),
            "friction_pattern": result.get("friction_pattern", ""),
            "failure_type": result.get("failure_type", ""),
            "errors": decision.get("errors", []),
        }
        rows.append(row)

    variable_stats = _summarize_variable_stats(rows)
    tree_stats = _summarize_tree_stats(rows)
    rescue_attempts = [x for x in rows if x.get("rescue_attempt")]
    rescue_success = [
        x for x in rescue_attempts
        if x.get("branch_status") == "continue" and x.get("delta_cof") is not None and float(x["delta_cof"]) < 0
    ]
    killed = [x for x in rows if x.get("branch_status") == "kill"]
    negative_samples = [
        x for x in rows
        if x.get("branch_status") == "kill" or str(x.get("failure_type") or "").strip()
    ]
    positive_samples = [
        x for x in rows
        if x.get("cof") is not None and x not in negative_samples
    ]
    failure_modes: Counter = Counter()
    for row in negative_samples:
        mode = str(row.get("failure_type") or "").strip()
        if not mode and row.get("errors"):
            mode = ",".join(str(x) for x in row.get("errors") or [])
        failure_modes[mode or "failed"] += 1
    try:
        from pva_work_flow.memory.failure_factor_memory import build_failure_factor_memory

        failure_factors = build_failure_factor_memory(out_dir)
    except Exception:
        failure_factors = []
    unsafe_factor_count = sum(1 for x in failure_factors if x.get("status") in {"suspected", "confirmed"})

    payload = {
        "summary": {
            "candidate_count": len(rows),
            "measured_count": sum(1 for x in rows if x.get("cof") is not None),
            "valid_measured_count": sum(
                1 for x in rows if x.get("cof") is not None and not x.get("experimental_failed")
            ),
            "positive_sample_count": len(positive_samples),
            "negative_sample_count": len(negative_samples),
            "unsafe_factor_count": unsafe_factor_count,
            "failure_mode_distribution": dict(sorted(failure_modes.items())),
            "tree_count": len({x["tree_id"] for x in rows}),
            "artifact_dir_count": len(_artifact_dirs(out_dir)),
            "continue_count": sum(1 for x in rows if x.get("branch_status") == "continue"),
            "rescue_candidate_count": sum(1 for x in rows if x.get("branch_status") == "rescue_candidate"),
            "kill_count": len(killed),
            "rescue_attempt_count": len(rescue_attempts),
            "rescue_success_count": len(rescue_success),
            "rescue_success_rate": _rate(len(rescue_success), len(rescue_attempts)),
        },
        "tree_stats": tree_stats,
        "variable_stats": variable_stats,
        "candidate_rows": rows,
    }

    (out_dir / "tree_statistics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "tree_statistics.md").write_text(
        tree_statistics_markdown(payload), encoding="utf-8"
    )
    _write_memory_cards(out_dir, payload)
    return payload


def tree_statistics_markdown(stats: Dict[str, Any]) -> str:
    summary = stats.get("summary", {})
    lines = ["# Cross-Tree Statistical Memory", ""]
    lines.append("## Summary")
    for key in [
        "candidate_count",
        "measured_count",
        "valid_measured_count",
        "positive_sample_count",
        "negative_sample_count",
        "unsafe_factor_count",
        "failure_mode_distribution",
        "tree_count",
        "artifact_dir_count",
        "continue_count",
        "rescue_candidate_count",
        "kill_count",
        "rescue_attempt_count",
        "rescue_success_rate",
    ]:
        lines.append(f"- {key}: {summary.get(key)}")

    lines.append("")
    lines.append("## Variable Effects")
    lines.append("| variable | n | measured | improvement_rate | mean_delta_cof | best_delta_cof | kill_rate |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in stats.get("variable_stats", [])[:20]:
        lines.append(
            f"| {row['variable']} | {row['n']} | {row['measured_n']} | "
            f"{row['improvement_rate']} | {row['mean_delta_cof']} | "
            f"{row['best_delta_cof']} | {row['kill_rate']} |"
        )

    lines.append("")
    lines.append("## Tree Ranking")
    lines.append("| tree_id | nodes | measured | valid_measured | best_cvs | best_cvs_candidate | best_valid_cof | raw_best_cof | continue | rescue_candidate | kill |")
    lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|")
    for row in stats.get("tree_stats", [])[:20]:
        lines.append(
            f"| {row['tree_id']} | {row['nodes']} | {row['measured_n']} | "
            f"{row.get('valid_measured_n')} | {row.get('best_cvs')} | "
            f"{row.get('best_cvs_candidate')} | {row.get('best_valid_cof')} | "
            f"{row['best_cof']} | {row['continue_n']} | "
            f"{row['rescue_candidate_n']} | {row['kill_n']} |"
        )
    return "\n".join(lines) + "\n"


def _write_memory_cards(out_dir: Path, stats: Dict[str, Any]) -> None:
    cards: list[dict] = []
    summary = stats.get("summary", {})
    if summary.get("negative_sample_count"):
        cards.append(
            {
                "card_type": "sample_balance",
                "key": "positive_negative_samples",
                "evidence": {
                    "positive_sample_count": summary.get("positive_sample_count"),
                    "negative_sample_count": summary.get("negative_sample_count"),
                    "failure_mode_distribution": summary.get("failure_mode_distribution"),
                },
                "retrieval_text": (
                    "Successful and failed wet-lab samples are both training evidence. "
                    f"positive={summary.get('positive_sample_count')}, "
                    f"negative={summary.get('negative_sample_count')}, "
                    f"failure_modes={summary.get('failure_mode_distribution')}"
                ),
            }
        )
    for row in stats.get("variable_stats", [])[:20]:
        direction = "promising" if (row.get("mean_delta_cof") is not None and row["mean_delta_cof"] < 0) else "risky_or_uncertain"
        cards.append(
            {
                "card_type": "variable_effect",
                "key": row["variable"],
                "direction": direction,
                "evidence": row,
                "retrieval_text": (
                    f"{row['variable']}: n={row['n']}, improvement_rate={row['improvement_rate']}, "
                    f"mean_delta_cof={row['mean_delta_cof']}, kill_rate={row['kill_rate']}"
                ),
            }
        )
    for row in stats.get("tree_stats", [])[:20]:
        cards.append(
            {
                "card_type": "tree_summary",
                "key": row["tree_id"],
                "evidence": row,
                "retrieval_text": (
                    f"tree {row['tree_id']}: best_cvs={row.get('best_cvs')} "
                    f"({row.get('best_cvs_candidate')}), "
                    f"best_valid_cof={row.get('best_valid_cof')}, "
                    f"raw_best_cof={row['best_cof']}, "
                    f"continue={row['continue_n']}, rescue={row['rescue_candidate_n']}, kill={row['kill_n']}"
                ),
            }
        )
    with open(out_dir / "tree_memory_cards.jsonl", "w", encoding="utf-8") as fh:
        for card in cards:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")


def build_tree_statistics_context(out_dir: Path, max_variables: int = 6, max_trees: int = 6) -> str:
    """Return a concise statistical RAG block for LLM prompt injection."""
    try:
        stats = build_tree_statistics(out_dir)
    except Exception:
        stats = _load_json(out_dir / "tree_statistics.json")
        if not stats:
            return ""

    if not stats.get("candidate_rows"):
        return ""

    summary = stats.get("summary", {})
    lines = [
        "=== CROSS-TREE STATISTICAL MEMORY ===",
        "Use this as statistical prior only. Do not mix parent lineages within a single tree expansion.",
        (
            f"trees={summary.get('tree_count')}, artifact_dirs={summary.get('artifact_dir_count')}, "
            f"measured={summary.get('measured_count')}, valid_measured={summary.get('valid_measured_count')}, "
            f"continue={summary.get('continue_count')}, rescue={summary.get('rescue_candidate_count')}, "
            f"kill={summary.get('kill_count')}, rescue_success_rate={summary.get('rescue_success_rate')}"
        ),
    ]

    variable_stats = stats.get("variable_stats", [])[:max_variables]
    if variable_stats:
        lines.append("Variable effect priors:")
        for row in variable_stats:
            lines.append(
                f"- {row['variable']}: n={row['n']}, improvement_rate={row['improvement_rate']}, "
                f"mean_delta_cof={row['mean_delta_cof']}, kill_rate={row['kill_rate']}"
            )

    tree_stats = stats.get("tree_stats", [])[:max_trees]
    if tree_stats:
        lines.append("Best root trees so far:")
        for row in tree_stats:
            lines.append(
                f"- {row['tree_id']}: best_cvs={row.get('best_cvs')} "
                f"({row.get('best_cvs_candidate')}), best_valid_cof={row.get('best_valid_cof')}, "
                f"raw_best_cof={row['best_cof']}, nodes={row['nodes']}, "
                f"continue={row['continue_n']}, kill={row['kill_n']}"
            )

    lines.append("Implication: when starting a later tree, prefer variables with repeated negative mean_delta_cof and avoid high kill_rate changes.")
    return "\n".join(lines)
