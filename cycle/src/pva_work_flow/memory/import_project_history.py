"""Import validated project experiment history into the formulation RAG SQLite.

Measured facts and model hypotheses are stored in separate tables.  Re-running
the importer is idempotent because stable record IDs are upserted.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pva_work_flow.memory.formulation_rag import resolve_formulation_rag_db
from pva_work_flow.wetlab.wetlab_outcomes import compute_cvs


DEFAULT_ROOTS = ("root-02", "root-03", "root-04", "root-06", "root-07")
CRITICAL_CODES = {"ERROR1", "ERROR2", "ERROR3"}
DAMAGE_CODES = {"ERROR4"}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _candidate_materials(candidate: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for material in candidate.get("materials") or []:
        if isinstance(material, dict) and material.get("name"):
            names.append(str(material["name"]))
    formulation = candidate.get("formulation") or {}
    for additive in formulation.get("additives") or []:
        if isinstance(additive, dict) and additive.get("name"):
            names.append(str(additive["name"]))
    crosslinker = formulation.get("crosslinker") or {}
    if isinstance(crosslinker, dict) and crosslinker.get("name"):
        names.append(str(crosslinker["name"]))
    return list(dict.fromkeys(name for name in names if name.strip()))


def _changed_variables(candidate: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in candidate.get("changed_variables") or []:
        if isinstance(item, dict):
            value = item.get("variable") or item.get("name")
        else:
            value = item
        if value:
            values.append(str(value))
    return values


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_history_imports (
            import_id TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            study_id TEXT NOT NULL,
            source_dir TEXT NOT NULL,
            root_count INTEGER NOT NULL,
            record_count INTEGER NOT NULL,
            hypothesis_count INTEGER NOT NULL,
            importer_version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_experiment_records (
            record_id TEXT PRIMARY KEY,
            study_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            root_id TEXT NOT NULL,
            round_idx INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            parent_candidate_id TEXT,
            design_type TEXT,
            evidence_type TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            error_codes_json TEXT NOT NULL,
            manual_observation TEXT,
            formulation_json TEXT NOT NULL,
            process_json TEXT NOT NULL,
            materials_json TEXT NOT NULL,
            changed_variables_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            cvs REAL,
            cvs_grade TEXT,
            source_files_json TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            UNIQUE(study_id, root_id, round_idx, candidate_id)
        );

        CREATE INDEX IF NOT EXISTS idx_project_experiment_root_round
        ON project_experiment_records(study_id, root_id, round_idx);
        CREATE INDEX IF NOT EXISTS idx_project_experiment_outcome
        ON project_experiment_records(outcome_status);

        CREATE TABLE IF NOT EXISTS project_experiment_hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            study_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            round_idx INTEGER NOT NULL,
            hypothesis TEXT NOT NULL,
            supporting_evidence_json TEXT NOT NULL,
            contradicting_evidence_json TEXT NOT NULL,
            source_file TEXT NOT NULL,
            trust_level TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        """
    )


def _score_lookup(out_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    lookup: dict[tuple[str, str], dict[str, str]] = {}
    for name in ("R6_chain_node_scores_current.csv", "R5_chain_node_scores_current.csv"):
        path = out_dir / name
        if not path.exists():
            continue
        for row in _read_csv(path):
            key = (str(row.get("root") or ""), str(row.get("cid") or ""))
            if all(key) and key not in lookup:
                lookup[key] = row
    return lookup


def _outcome(row: dict[str, str] | None, codes: list[str], observation: str) -> str:
    upper_codes = {code.upper() for code in codes}
    text = observation.lower()
    if not row and not codes and ("not tested" in text or "未测试" in text):
        return "not_tested"
    if upper_codes & CRITICAL_CODES:
        return "critical_failure"
    if upper_codes & DAMAGE_CODES or "permanent" in text or "永久" in text:
        return "permanent_damage"
    if codes:
        return "measured_with_issues" if row else "observed_issue_no_numeric_result"
    if row:
        return "measured"
    return "not_measured"


def collect_records(out_dir: Path, roots: tuple[str, ...], study_id: str) -> list[dict[str, Any]]:
    scores = _score_lookup(out_dir)
    records: list[dict[str, Any]] = []
    trees_dir = out_dir / "trees"
    now = datetime.now().isoformat(timespec="seconds")

    for root in roots:
        tree_dir = trees_dir / root
        for round_idx in range(1, 7):
            candidates_path = tree_dir / f"R{round_idx}_candidates.json"
            candidates_obj = _read_json(candidates_path)
            candidates = candidates_obj.get("candidates") or []
            if not candidates and round_idx == 1:
                root_obj = _read_json(tree_dir / "root_candidate.json")
                candidates = root_obj.get("candidates") or ([root_obj] if root_obj.get("candidate_id") else [])
            results = {
                str(row.get("candidate_id")): row
                for row in _read_csv(tree_dir / f"R{round_idx}_results_filled.csv")
                if row.get("candidate_id")
            }
            notes = _read_json(tree_dir / f"R{round_idx}_experiment_notes.json")

            for candidate in candidates:
                cid = str(candidate.get("candidate_id") or "").strip()
                if not cid:
                    continue
                result = results.get(cid)
                note = notes.get(cid, {}) if isinstance(notes.get(cid), dict) else {}
                codes = [str(code).upper() for code in note.get("error_codes") or []]
                observation = str(note.get("free_text") or "")
                score_row = scores.get((root, cid), {})
                if not codes and score_row.get("errors"):
                    codes = [
                        code.strip().upper()
                        for code in str(score_row["errors"]).replace(",", ";").split(";")
                        if code.strip()
                    ]
                cvs_obj = None
                if result or codes:
                    cvs_obj = compute_cvs(result or {"notes": " ".join(codes)}, codes)
                cvs_raw = score_row.get("cvs") or (cvs_obj or {}).get("cvs")
                try:
                    cvs = float(cvs_raw) if cvs_raw not in (None, "") else None
                except (TypeError, ValueError):
                    cvs = None
                grade = str(score_row.get("grade") or (cvs_obj or {}).get("grade") or "")
                metrics = dict(result or {})
                if score_row:
                    metrics["historical_score_record"] = score_row
                formulation = candidate.get("formulation") or {}
                process = candidate.get("process") or candidate.get("processing") or {}
                materials = _candidate_materials(candidate)
                changed = _changed_variables(candidate)
                score_status = str(score_row.get("status") or "")
                historical_measurement = score_row if score_status not in {"", "not_measured"} else None
                status = _outcome(result or historical_measurement, codes, observation)
                source_files = [
                    str(path.relative_to(out_dir))
                    for path in (
                        candidates_path,
                        tree_dir / f"R{round_idx}_results_filled.csv",
                        tree_dir / f"R{round_idx}_experiment_notes.json",
                        tree_dir / f"R{round_idx}_inheritance_table.md",
                    )
                    if path.exists()
                ]
                searchable = " ".join(
                    [
                        "PVA hydrogel project experiment measured_fact",
                        study_id, root, cid, str(candidate.get("parent_candidate_id") or ""),
                        str(candidate.get("design_type") or candidate.get("design_role") or ""),
                        status, " ".join(codes), observation, " ".join(materials),
                        " ".join(changed), _compact_json(formulation), _compact_json(process),
                        _compact_json(metrics),
                    ]
                )
                records.append(
                    {
                        "record_id": f"{study_id}:{root}:{cid}",
                        "study_id": study_id,
                        "model_name": "qwen3-14b",
                        "root_id": root,
                        "round_idx": round_idx,
                        "candidate_id": cid,
                        "parent_candidate_id": str(candidate.get("parent_candidate_id") or ""),
                        "design_type": str(candidate.get("design_type") or candidate.get("design_role") or ""),
                        "evidence_type": "measured_fact",
                        "outcome_status": status,
                        "error_codes_json": _compact_json(codes),
                        "manual_observation": observation,
                        "formulation_json": _compact_json(formulation),
                        "process_json": _compact_json(process),
                        "materials_json": _compact_json(materials),
                        "changed_variables_json": _compact_json(changed),
                        "metrics_json": _compact_json(metrics),
                        "cvs": cvs,
                        "cvs_grade": grade,
                        "source_files_json": _compact_json(source_files),
                        "searchable_text": searchable,
                        "imported_at": now,
                    }
                )
    return records


def collect_hypotheses(out_dir: Path, roots: tuple[str, ...], study_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = datetime.now().isoformat(timespec="seconds")
    for root in roots:
        path = out_dir / "trees" / root / "R6_diagnosis.json"
        obj = _read_json(path)
        for index, item in enumerate(obj.get("inferred_mechanisms") or [], 1):
            rows.append(
                {
                    "hypothesis_id": f"{study_id}:{root}:R6:H{index:02d}",
                    "study_id": study_id,
                    "root_id": root,
                    "round_idx": 6,
                    "hypothesis": str(item.get("hypothesis") or ""),
                    "supporting_evidence_json": _compact_json(item.get("supporting_evidence") or []),
                    "contradicting_evidence_json": _compact_json(item.get("contradicting_evidence") or []),
                    "source_file": str(path.relative_to(out_dir)),
                    "trust_level": "model_hypothesis_unverified",
                    "imported_at": now,
                }
            )
    return rows


def import_history(db_path: Path, out_dir: Path, roots: tuple[str, ...], study_id: str, backup: bool) -> dict[str, Any]:
    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.stem}.before_project_history_{stamp}{db_path.suffix}")
        shutil.copy2(db_path, backup_path)
    else:
        backup_path = None

    records = collect_records(out_dir, roots, study_id)
    hypotheses = collect_hypotheses(out_dir, roots, study_id)
    conn = sqlite3.connect(db_path)
    try:
        _init_schema(conn)
        record_columns = list(records[0]) if records else []
        if records:
            sql = f"INSERT OR REPLACE INTO project_experiment_records ({','.join(record_columns)}) VALUES ({','.join('?' for _ in record_columns)})"
            conn.executemany(sql, [[row[col] for col in record_columns] for row in records])
        hypothesis_columns = list(hypotheses[0]) if hypotheses else []
        if hypotheses:
            sql = f"INSERT OR REPLACE INTO project_experiment_hypotheses ({','.join(hypothesis_columns)}) VALUES ({','.join('?' for _ in hypothesis_columns)})"
            conn.executemany(sql, [[row[col] for col in hypothesis_columns] for row in hypotheses])
        imported_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO project_history_imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{study_id}:v1", imported_at, study_id, str(out_dir), len(roots), len(records), len(hypotheses), 1),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "database": str(db_path),
        "backup": str(backup_path) if backup_path else None,
        "study_id": study_id,
        "roots": list(roots),
        "records": len(records),
        "hypotheses": len(hypotheses),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Qwen project experiment history into formulation RAG.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--db", default="")
    parser.add_argument("--study-id", default="qwen3_14b_cycles_2026")
    parser.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    summary = import_history(
        resolve_formulation_rag_db(args.db or None),
        Path(args.out_dir).resolve(),
        tuple(args.roots),
        args.study_id,
        backup=not args.no_backup,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
