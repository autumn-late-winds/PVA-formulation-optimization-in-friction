"""Local vectorized RAG index for experiment and formulation memories.

The project mostly stores structured wet-lab evidence.  This module adds a
lightweight local vector layer on top of those records without requiring a
remote embedding service or model download.  It uses sparse TF-IDF vectors and
cosine similarity, so it can run in offline lab environments and later be
replaced by dense embeddings behind the same document/query interface.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


VECTOR_INDEX_JSON = "rag_vector_index.json"
MAX_DOC_TEXT_CHARS = 1800
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}|[0-9]+(?:\.[0-9]+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _compact_text(value: Any, limit: int = MAX_DOC_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _stringify_changes(changes: Any) -> str:
    if not isinstance(changes, list):
        return _compact_text(changes)
    parts: list[str] = []
    for item in changes:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        variable = item.get("variable") or item.get("name") or "unknown"
        old_value = item.get("old_value")
        new_value = item.get("new_value", item.get("value"))
        parts.append(f"{variable}: {old_value} -> {new_value}")
    return "; ".join(parts)


def _tokenize(text: str) -> list[str]:
    lower = text.lower()
    tokens = _WORD_RE.findall(lower)
    for block in _CJK_RE.findall(text):
        tokens.extend(block)
        tokens.extend(block[i : i + 2] for i in range(max(0, len(block) - 1)))
        tokens.extend(block[i : i + 3] for i in range(max(0, len(block) - 2)))
    return [t for t in tokens if len(t.strip()) >= 2]


def _normalize_sparse(counts: Counter[str], idf: dict[str, float]) -> dict[str, float]:
    weighted: dict[str, float] = {}
    for token, count in counts.items():
        weight = (1.0 + math.log(float(count))) * idf.get(token, 1.0)
        if weight > 0:
            weighted[token] = weight
    norm = math.sqrt(sum(v * v for v in weighted.values()))
    if norm <= 0:
        return {}
    return {k: round(v / norm, 8) for k, v in weighted.items()}


def _dot_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def collect_experiment_documents(out_dir: Path) -> list[dict[str, Any]]:
    """Collect vectorizable project experiment memories from JSONL artifacts."""
    docs: list[dict[str, Any]] = []

    for rec in _read_jsonl(out_dir / "failure_factor_memory.jsonl"):
        text = " ".join(
            [
                "failure factor",
                str(rec.get("status") or ""),
                str(rec.get("factor") or ""),
                str(rec.get("variable") or ""),
                str(rec.get("suspected_value") or ""),
                str(rec.get("failure_mode") or ""),
                " ".join(str(x) for x in rec.get("error_codes") or []),
                json.dumps(rec.get("scope") or {}, ensure_ascii=False),
            ]
        )
        docs.append(
            {
                "doc_id": f"failure_factor:{rec.get('factor_id')}",
                "source_type": "failure_factor",
                "text": _compact_text(text),
                "metadata": {
                    "factor_id": rec.get("factor_id"),
                    "status": rec.get("status"),
                    "variable": rec.get("variable"),
                    "failure_mode": rec.get("failure_mode"),
                    "candidate_id": rec.get("candidate_id"),
                    "source_dir": rec.get("source_dir"),
                },
            }
        )

    for rec in _read_jsonl(out_dir / "experiment_contrast_memory.jsonl"):
        text = " ".join(
            [
                "experiment contrast",
                str(rec.get("label") or ""),
                str(rec.get("learning_signal") or ""),
                _stringify_changes(rec.get("changed_variables")),
                str(rec.get("failure_mode") or ""),
                " ".join(str(x) for x in rec.get("error_codes") or []),
                json.dumps(rec.get("parent_scope") or {}, ensure_ascii=False),
                json.dumps(rec.get("child_scope") or {}, ensure_ascii=False),
            ]
        )
        docs.append(
            {
                "doc_id": f"experiment_contrast:{rec.get('contrast_id')}",
                "source_type": "experiment_contrast",
                "text": _compact_text(text),
                "metadata": {
                    "contrast_id": rec.get("contrast_id"),
                    "label": rec.get("label"),
                    "parent_candidate_id": rec.get("parent_candidate_id"),
                    "child_candidate_id": rec.get("child_candidate_id"),
                    "failure_mode": rec.get("failure_mode"),
                    "source_dir": rec.get("source_dir"),
                },
            }
        )

    for rec in _read_jsonl(out_dir / "tree_memory_cards.jsonl"):
        text = json.dumps(rec, ensure_ascii=False)
        docs.append(
            {
                "doc_id": f"tree_memory:{rec.get('card_id') or len(docs)}",
                "source_type": "tree_memory",
                "text": _compact_text(text),
                "metadata": {
                    "card_id": rec.get("card_id"),
                    "tree_id": rec.get("tree_id"),
                    "memory_type": rec.get("memory_type"),
                },
            }
        )
    return docs


def collect_formulation_documents(db_path: Path | None) -> list[dict[str, Any]]:
    """Collect vectorizable formulation literature cases from SQLite."""
    if db_path is None or not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              fc.case_id, p.source_title, p.source_year, fc.case_type,
              fc.baseline_formulation, fc.optimized_formulation, fc.changed_factor,
              fc.property_targets, fc.direction, fc.numeric_evidence_json,
              fc.mechanism_or_failure_reason, fc.tradeoff_or_risk, fc.optimization_use,
              fc.evidence_text, fc.source_locator
            FROM formulation_cases fc
            LEFT JOIN papers p ON p.paper_id = fc.paper_id
            """
        ).fetchall()
        project_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_experiment_records'"
        ).fetchone()
        project_rows = conn.execute(
            "SELECT * FROM project_experiment_records WHERE evidence_type='measured_fact'"
        ).fetchall() if project_exists else []
    finally:
        conn.close()

    docs: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        text = " ".join(
            [
                str(rec.get("source_title") or ""),
                str(rec.get("case_type") or ""),
                str(rec.get("baseline_formulation") or ""),
                str(rec.get("optimized_formulation") or ""),
                str(rec.get("changed_factor") or ""),
                str(rec.get("property_targets") or ""),
                str(rec.get("direction") or ""),
                str(rec.get("mechanism_or_failure_reason") or ""),
                str(rec.get("tradeoff_or_risk") or ""),
                str(rec.get("optimization_use") or ""),
                str(rec.get("numeric_evidence_json") or ""),
                str(rec.get("evidence_text") or ""),
            ]
        )
        docs.append(
            {
                "doc_id": f"formulation_case:{rec.get('case_id')}",
                "source_type": "formulation_case",
                "text": _compact_text(text),
                "metadata": {
                    "case_id": rec.get("case_id"),
                    "source_title": rec.get("source_title"),
                    "source_year": rec.get("source_year"),
                    "changed_factor": rec.get("changed_factor"),
                    "property_targets": rec.get("property_targets"),
                    "tradeoff_or_risk": rec.get("tradeoff_or_risk"),
                    "source_locator": rec.get("source_locator"),
                },
            }
        )
    for row in project_rows:
        rec = dict(row)
        docs.append(
            {
                "doc_id": f"project_experiment:{rec.get('record_id')}",
                "source_type": "project_experiment",
                "text": _compact_text(str(rec.get("searchable_text") or "")),
                "metadata": {
                    "record_id": rec.get("record_id"),
                    "study_id": rec.get("study_id"),
                    "root_id": rec.get("root_id"),
                    "round_idx": rec.get("round_idx"),
                    "candidate_id": rec.get("candidate_id"),
                    "outcome_status": rec.get("outcome_status"),
                    "cvs": rec.get("cvs"),
                },
            }
        )
    return docs


def build_vector_index(
    documents: Iterable[dict[str, Any]],
    *,
    index_kind: str = "tfidf",
) -> dict[str, Any]:
    docs = [d for d in documents if str(d.get("text") or "").strip()]
    token_counts = [Counter(_tokenize(str(doc.get("text") or ""))) for doc in docs]
    df: Counter[str] = Counter()
    for counts in token_counts:
        df.update(counts.keys())
    n_docs = len(docs)
    idf = {token: math.log((1.0 + n_docs) / (1.0 + freq)) + 1.0 for token, freq in df.items()}
    vectors = [_normalize_sparse(counts, idf) for counts in token_counts]
    return {
        "schema_version": 1,
        "index_kind": index_kind,
        "embedding_backend": "local_tfidf_sparse",
        "doc_count": n_docs,
        "idf": {k: round(v, 8) for k, v in idf.items()},
        "documents": [
            {
                "doc_id": doc.get("doc_id"),
                "source_type": doc.get("source_type"),
                "text": doc.get("text"),
                "metadata": doc.get("metadata") or {},
                "vector": vectors[i],
            }
            for i, doc in enumerate(docs)
        ],
    }


def write_vector_index(out_dir: Path, index: dict[str, Any]) -> Path:
    path = out_dir / VECTOR_INDEX_JSON
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_vector_index(out_dir: Path) -> dict[str, Any]:
    path = out_dir / VECTOR_INDEX_JSON
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_project_vector_index(out_dir: Path, formulation_db: Path | None = None) -> dict[str, Any]:
    docs = collect_experiment_documents(out_dir)
    docs.extend(collect_formulation_documents(formulation_db))
    index = build_vector_index(docs)
    write_vector_index(out_dir, index)
    return index


def query_vector_index(
    index: dict[str, Any],
    query: str,
    *,
    top_k: int = 6,
    source_types: set[str] | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not index or not query.strip():
        return []
    idf = {str(k): float(v) for k, v in (index.get("idf") or {}).items()}
    query_vec = _normalize_sparse(Counter(_tokenize(query)), idf)
    if not query_vec:
        return []
    hits: list[dict[str, Any]] = []
    for doc in index.get("documents") or []:
        source_type = str(doc.get("source_type") or "")
        if source_types and source_type not in source_types:
            continue
        metadata = doc.get("metadata") or {}
        if metadata_filter:
            skip = False
            for key, expected in metadata_filter.items():
                if metadata.get(key) != expected:
                    skip = True
                    break
            if skip:
                continue
        score = _dot_sparse(query_vec, {str(k): float(v) for k, v in (doc.get("vector") or {}).items()})
        if score <= 0:
            continue
        hit = {
            "score": round(score, 6),
            "doc_id": doc.get("doc_id"),
            "source_type": source_type,
            "text": doc.get("text"),
            "metadata": metadata,
        }
        hits.append(hit)
    hits.sort(key=lambda h: -float(h.get("score") or 0))
    return hits[:top_k]


def ensure_project_vector_index(out_dir: Path, formulation_db: Path | None = None) -> dict[str, Any]:
    index = load_vector_index(out_dir)
    expected_has_formulation = bool(formulation_db and formulation_db.exists())
    expected_has_project_experiments = False
    if expected_has_formulation and formulation_db is not None:
        conn = sqlite3.connect(formulation_db)
        try:
            expected_has_project_experiments = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_experiment_records'"
                ).fetchone()
            )
        finally:
            conn.close()
    if index:
        source_types = {str(d.get("source_type") or "") for d in index.get("documents") or []}
        has_required_formulation = not expected_has_formulation or "formulation_case" in source_types
        has_required_project = not expected_has_project_experiments or "project_experiment" in source_types
        if has_required_formulation and has_required_project:
            return index
    return build_project_vector_index(out_dir, formulation_db=formulation_db)


def render_vector_hits(hits: list[dict[str, Any]], title: str = "VECTOR RAG MATCHES") -> str:
    if not hits:
        return ""
    lines = [f"=== {title} ==="]
    for i, hit in enumerate(hits, 1):
        meta = hit.get("metadata") or {}
        label = (
            meta.get("case_id")
            or meta.get("child_candidate_id")
            or meta.get("factor_id")
            or meta.get("card_id")
            or hit.get("doc_id")
        )
        detail = meta.get("changed_factor") or meta.get("failure_mode") or meta.get("label") or meta.get("status") or ""
        lines.append(f"{i}. {label} score={hit.get('score')} type={hit.get('source_type')} {detail}")
        text = _compact_text(hit.get("text"), 220)
        if text:
            lines.append(f"   evidence: {text}")
    return "\n".join(lines)
