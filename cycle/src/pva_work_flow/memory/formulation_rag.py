"""External formulation-literature RAG for the PVA optimization loop.

This module bridges the cleaned formulation-optimization SQLite database under
``数据库/`` into the ``cycle`` workflow. It is intentionally prompt-only: the
retrieved literature cases provide scientific priors, but never alter tree
lineage, parent ids, DOE skeletons, or hard rule checks.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from typing import Any

from pva_work_flow.artifacts.artifact_store import RunWorkspace
from pva_work_flow.artifacts.io_artifacts import read_results_filled
from pva_work_flow.core.utils import read_json
from pva_work_flow.wetlab.wetlab_outcomes import has_failure


DEFAULT_REL_DB = (
    "数据库/formulation_optimization_cases_agent_reviewed/"
    "formulation_rag_agent_reviewed.sqlite"
)


def formulation_rag_enabled() -> bool:
    raw = os.environ.get("PVA_FORMULATION_RAG_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_formulation_rag_db(db_path: str | Path | None = None) -> Path:
    raw = str(db_path or os.environ.get("PVA_FORMULATION_RAG_DB", "")).strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (_repo_root() / p)
    return _repo_root() / DEFAULT_REL_DB


def _load_query_module() -> Any | None:
    script = _repo_root() / "数据库" / "scripts" / "query_formulation_rag.py"
    if not script.exists():
        return None
    spec = importlib.util.spec_from_file_location("_pva_query_formulation_rag", script)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _candidate_material_terms(out_dir: Path, round_idx: int, max_terms: int = 8) -> list[str]:
    terms: list[str] = ["PVA"]
    workspace = RunWorkspace(out_dir)
    for rr in range(round_idx, 0, -1):
        path = workspace.candidates_path(rr)
        if not path.exists():
            continue
        try:
            obj = read_json(path)
        except Exception:
            continue
        for c in obj.get("candidates", []) or []:
            formulation = c.get("formulation") or {}
            for a in formulation.get("additives") or []:
                if isinstance(a, dict) and a.get("name"):
                    terms.append(str(a["name"]))
            crosslinker = formulation.get("crosslinker") or {}
            if isinstance(crosslinker, dict) and crosslinker.get("name"):
                terms.append(str(crosslinker["name"]))
            for m in c.get("materials") or []:
                if isinstance(m, dict) and m.get("name"):
                    terms.append(str(m["name"]))
        break

    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.strip().lower()
        if not key or key in {"none", "di water", "water", "deionized water"}:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(term.strip())
        if len(unique) >= max_terms:
            break
    return unique


def _problem_from_results(out_dir: Path, round_idx: int) -> str:
    labels = ["低摩擦高承载设计", "强度润滑权衡"]
    result_paths = RunWorkspace(out_dir).round_artifact_paths(round_idx, "results_filled.csv")
    if not result_paths:
        return " ".join(labels + ["PVA水凝胶疲劳失效原因", "网络结构稳定性"])

    try:
        rows: list[dict[str, str]] = []
        for results_path in result_paths:
            rows.extend(read_results_filled(results_path))
    except Exception:
        return " ".join(labels)

    saw_high_friction = False
    saw_wear = False
    saw_instability = False
    saw_failure = False
    saw_modulus_issue = False
    for row in rows:
        try:
            cof = float(str(row.get("cof_steady_mean") or ""))
            if cof > 0.03:
                saw_high_friction = True
        except ValueError:
            pass
        if str(row.get("wear_proxy") or "").strip() not in {"", "0", "none", "na"}:
            saw_wear = True
        if has_failure(row):
            saw_failure = True
        pattern_blob = " ".join(
            str(row.get(k) or "")
            for k in ("friction_pattern", "stick_slip_score", "stable_proportion")
        ).lower()
        if "stick" in pattern_blob or "irregular" in pattern_blob or "unstable" in pattern_blob:
            saw_instability = True
        try:
            modulus = float(str(row.get("compression_modulus_MPa") or ""))
            if modulus < 0.5 or modulus > 2.5:
                saw_modulus_issue = True
        except ValueError:
            pass

    if saw_high_friction:
        labels.extend(["摩擦高", "润滑差"])
    if saw_wear:
        labels.append("容易磨损")
    if saw_instability:
        labels.extend(["润滑层耗尽", "网络结构稳定性"])
    if saw_failure:
        labels.extend(["疲劳失效", "PVA水凝胶疲劳失效原因"])
    if saw_modulus_issue:
        labels.append("力学弱")
    return " ".join(dict.fromkeys(labels))


def _query_rows(
    db_path: Path,
    problem: str,
    material_terms: list[str],
    limit: int,
    out_dir: Path | None = None,
) -> list[dict[str, Any]]:
    query_mod = _load_query_module()
    if query_mod is None:
        rows: list[dict[str, Any]] = []
    else:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = []
            problem_terms = query_mod.expand_problem(problem)
            rows.extend(
                query_mod.query_cases(
                    conn,
                    problem_terms,
                    max(3, limit),
                    match_mode="any",
                    query_type="problem",
                )
            )
            for term in material_terms[:5]:
                rows.extend(
                    query_mod.query_cases(
                        conn,
                        [term],
                        2,
                        match_mode="any",
                        query_type="material",
                    )
                )
        finally:
            conn.close()

    try:
        rows.extend(_query_vector_rows(db_path, problem, material_terms, limit, out_dir))
    except Exception:
        pass

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: -float(r.get("stage2_score") or 0)):
        key = str(row.get("case_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def _fetch_cases_by_id(db_path: Path, case_ids: list[str]) -> list[dict[str, Any]]:
    if not case_ids:
        return []
    placeholders = ",".join("?" for _ in case_ids)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT
              fc.case_id, p.source_title, p.source_year, fc.case_type,
              fc.baseline_formulation, fc.optimized_formulation, fc.changed_factor,
              fc.property_targets, fc.direction, fc.numeric_evidence_json,
              fc.mechanism_or_failure_reason, fc.tradeoff_or_risk, fc.optimization_use,
              fc.evidence_text, fc.source_locator
            FROM formulation_cases fc
            LEFT JOIN papers p ON p.paper_id = fc.paper_id
            WHERE fc.case_id IN ({placeholders})
            """,
            case_ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {str(row["case_id"]): dict(row) for row in rows}
    return [by_id[cid] for cid in case_ids if cid in by_id]


def _query_vector_rows(
    db_path: Path,
    problem: str,
    material_terms: list[str],
    limit: int,
    out_dir: Path | None,
) -> list[dict[str, Any]]:
    if out_dir is None:
        return []
    from pva_work_flow.memory.vector_rag import ensure_project_vector_index, query_vector_index

    query = " ".join([problem, *material_terms])
    index = ensure_project_vector_index(out_dir, formulation_db=db_path)
    hits = query_vector_index(index, query, top_k=max(limit, 8), source_types={"formulation_case"})
    case_ids = [str((hit.get("metadata") or {}).get("case_id") or "") for hit in hits]
    case_ids = [cid for cid in case_ids if cid]
    score_by_case = {str((hit.get("metadata") or {}).get("case_id")): hit.get("score") for hit in hits}
    rows = _fetch_cases_by_id(db_path, case_ids)
    for row in rows:
        vector_score = float(score_by_case.get(str(row.get("case_id"))) or 0)
        row["vector_score"] = vector_score
        row["stage2_score"] = max(float(row.get("stage2_score") or 0), 20.0 * vector_score)
        row["is_reference_only"] = 0
    return rows


def _compact(text: Any, limit: int = 220) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def render_formulation_rag_context(
    rows: list[dict[str, Any]],
    phase: str,
    db_path: Path,
) -> str:
    if not rows:
        return ""
    lines = [
        "=== EXTERNAL FORMULATION LITERATURE RAG ===",
        f"Source DB: {db_path.name}",
        f"Use in this phase: {phase}",
        "Priority rule: project experimental results and tree lineage override literature priors.",
        "Do not cite a literature prior as if it were project data.",
        "",
        "Relevant formulation optimization cases:",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {row.get('changed_factor') or row.get('optimized_formulation') or row.get('case_id')}: "
            f"target={_compact(row.get('property_targets'), 90)}; "
            f"direction={_compact(row.get('direction'), 70)}; "
            f"risk={_compact(row.get('tradeoff_or_risk'), 120)}"
        )
        opt = _compact(row.get("optimization_use"), 180)
        mech = _compact(row.get("mechanism_or_failure_reason"), 180)
        source = _compact(row.get("source_title"), 110)
        year = row.get("source_year") or ""
        if opt:
            lines.append(f"   optimization prior: {opt}")
        if mech:
            lines.append(f"   mechanism: {mech}")
        if row.get("vector_score") is not None:
            lines.append(f"   vector_score: {float(row.get('vector_score') or 0):.4f}")
        lines.append(f"   source: {source} ({year}), locator={row.get('source_locator') or '?'}")
    return "\n".join(lines)


def build_formulation_rag_context(
    out_dir: Path,
    round_idx: int,
    phase: str,
    limit: int = 6,
    db_path: str | Path | None = None,
    problem: str | None = None,
) -> str:
    if not formulation_rag_enabled():
        return ""
    resolved_db = resolve_formulation_rag_db(db_path)
    if not resolved_db.exists():
        return ""

    material_terms = _candidate_material_terms(out_dir, round_idx)
    query_problem = problem or _problem_from_results(out_dir, max(1, round_idx - 1))
    rows = _query_rows(resolved_db, query_problem, material_terms, limit, out_dir=out_dir)
    return render_formulation_rag_context(rows, phase=phase, db_path=resolved_db)
