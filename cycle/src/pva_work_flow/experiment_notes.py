"""Manual experiment observation notes.

Bruker CSV files cannot capture phenomena like gel rupture, no-gelation,
or unusual noise.  This module lets users record per-candidate observations
via a JSON file, which are then injected into diagnosis prompts and used
by the parent-selection logic in constrained_doe.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---- Error-code registry ---------------------------------------------------
# Canonical definitions live in prompts/experiment_errors.yaml.
# A hardcoded fallback is provided so the registry works even when PyYAML
# is not installed on the deployment server (common in AutoDL / Docker).
_ERROR_REGISTRY: Dict[str, Dict[str, str]] = {}

_FALLBACK_REGISTRY: Dict[str, Dict[str, str]] = {
    "ERROR1": {
        "label": "凝胶在摩擦过程中破裂 (Gel rupture during friction)",
        "severity": "critical",
        "impact": "Prioritize modulus optimization over COF. Current formulation lacks mechanical integrity.",
        "suggested_action": "increase_crosslink_density_or_pva_wt",
    },
    "ERROR2": {
        "label": "PVA水凝胶完全未成胶 (No gelation)",
        "severity": "critical",
        "impact": "Formulation cannot be tested. Check crosslinking chemistry and processing.",
        "suggested_action": "verify_crosslinker_and_catalyst",
    },
    "ERROR3": {
        "label": "凝胶过软无法夹持测试 (Gel too soft to clamp)",
        "severity": "critical",
        "impact": "Modulus too low. Need higher PVA wt% or more crosslinking.",
        "suggested_action": "increase_pva_wt_or_crosslink_density",
    },
    "ERROR4": {
        "label": "摩擦后表面可见损伤/剥落 (Visible surface damage after test)",
        "severity": "high",
        "impact": "Wear resistance insufficient. Consider additives for surface integrity.",
        "suggested_action": "add_wear_resistant_additive_or_adjust_crosslink",
    },
    "ERROR5": {
        "label": "测试中异常噪音/振动 (Unusual noise/vibration during test)",
        "severity": "medium",
        "impact": "Possible stick-slip or irregular contact. Check surface uniformity.",
        "suggested_action": "improve_surface_uniformity_or_lubrication",
    },
    "ERROR6": {
        "label": "凝胶在浸泡后过度溶胀变形 (Excessive swelling after soak)",
        "severity": "high",
        "impact": "Crosslink density too low. Water uptake uncontrolled.",
        "suggested_action": "increase_crosslink_density_or_reduce_soak_time",
    },
    "ERROR7": {
        "label": "摩擦系数随测试时间持续上升 (COF rises continuously during test)",
        "severity": "medium",
        "impact": "Boundary lubrication layer not stable. Consider lubricating additives.",
        "suggested_action": "add_or_increase_lubricating_additive",
    },
    "ERROR8": {
        "label": "制备过程中出现沉淀/相分离 (Precipitate/phase separation during preparation)",
        "severity": "high",
        "impact": "Material incompatibility. Check additive solubility and mixing order.",
        "suggested_action": "adjust_mixing_order_or_replace_incompatible_additive",
    },
    "ERROR9": {
        "label": "批次间重复性差 (Poor batch-to-batch reproducibility)",
        "severity": "medium",
        "impact": "Process control needed. Document every step timing and temperature precisely.",
        "suggested_action": "improve_process_control_and_documentation",
    },
    "ERROR10": {
        "label": "其他异常 (Other anomaly — see free-text notes)",
        "severity": "info",
        "impact": "Check free-text notes for details.",
        "suggested_action": "review_notes",
    },
}


def _load_error_registry() -> Dict[str, Dict[str, str]]:
    """Try YAML first; fall back to hardcoded registry silently."""
    _ERROR_YAML = Path(__file__).parent / "prompts" / "experiment_errors.yaml"
    try:
        import yaml
        _raw = yaml.safe_load(_ERROR_YAML.read_text(encoding="utf-8"))
        loaded = _raw.get("errors", {}) if isinstance(_raw, dict) else {}
        if loaded:
            return loaded
    except Exception:
        pass
    return dict(_FALLBACK_REGISTRY)


_ERROR_REGISTRY = _load_error_registry()


def error_label(code: str) -> str:
    """Return the human-readable label for an error code, or the code itself."""
    return _ERROR_REGISTRY.get(code, {}).get("label", code)


def error_severity(code: str) -> str:
    return _ERROR_REGISTRY.get(code, {}).get("severity", "info")


def error_impact(code: str) -> str:
    return _ERROR_REGISTRY.get(code, {}).get("impact", "")


def error_suggested_action(code: str) -> str:
    return _ERROR_REGISTRY.get(code, {}).get("suggested_action", "")


def known_error_codes() -> List[str]:
    return sorted(_ERROR_REGISTRY.keys())


# ---- Notes file I/O ----
def _notes_path(out_dir: Path, round_idx: int) -> Path:
    return out_dir / f"R{round_idx}_experiment_notes.json"


def load_notes(out_dir: Path, round_idx: int) -> Dict[str, Any]:
    """Load per-candidate experiment notes for a round.

    Returns {candidate_id: {error_codes: [...], free_text: "...", operator: "..."}}
    """
    path = _notes_path(out_dir, round_idx)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_notes(out_dir: Path, round_idx: int, notes: Dict[str, Any]) -> Path:
    path = _notes_path(out_dir, round_idx)
    path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_notes_template(out_dir: Path, round_idx: int, candidate_ids: List[str]) -> Path:
    """Create a template notes file that users can edit."""
    template: Dict[str, Any] = {
        "_instructions": {
            "purpose": "Record manual observations that Bruker CSV cannot capture.",
            "how_to_fill": "For each candidate, fill error_codes (e.g., ERROR1) and optionally free_text and operator name.",
            "available_error_codes": {
                code: _ERROR_REGISTRY.get(code, {}).get("label", "")
                for code in known_error_codes()
            },
        }
    }
    for cid in candidate_ids:
        template[cid] = {
            "error_codes": [],
            "free_text": "",
            "operator": "",
        }
    return save_notes(out_dir, round_idx, template)


def apply_notes_to_candidates(
    candidates: List[Dict[str, Any]],
    round_idx: int,
    out_dir: Path,
) -> None:
    """Mutate candidate dicts with experimental_status and failure_mode from notes."""
    notes = load_notes(out_dir, round_idx)
    if not notes:
        return

    for c in candidates:
        cid = c.get("candidate_id", "")
        entry = notes.get(cid)
        if not entry or not isinstance(entry, dict):
            continue

        error_codes: List[str] = entry.get("error_codes") or []
        free_text: str = entry.get("free_text") or ""
        operator: str = entry.get("operator") or ""

        # Determine experimental_status from error codes
        has_critical = any(error_severity(e) == "critical" for e in error_codes)
        has_high = any(error_severity(e) == "high" for e in error_codes)

        if has_critical:
            c["experimental_status"] = "experimental_failed"
            c.setdefault("failure_mode", [])
            if isinstance(c["failure_mode"], list):
                for e in error_codes:
                    label = error_label(e)
                    if label not in c["failure_mode"]:
                        c["failure_mode"].append(label)
        elif has_high:
            c["experimental_status"] = "measured_with_issues"
            c.setdefault("failure_mode", [])
            if isinstance(c["failure_mode"], list):
                for e in error_codes:
                    label = error_label(e)
                    if label not in c["failure_mode"]:
                        c["failure_mode"].append(label)
        elif not c.get("experimental_status") or c["experimental_status"] == "not_measured":
            # Don't override if already set from Bruker data
            pass

        # Attach notes to candidate for downstream use
        c["_experiment_notes"] = {
            "error_codes": error_codes,
            "free_text": free_text,
            "operator": operator,
        }


def build_notes_context_for_diagnosis(out_dir: Path, round_idx: int) -> str:
    """Produce a text block for injection into the diagnosis prompt."""
    notes = load_notes(out_dir, round_idx)
    if not notes:
        return ""

    lines = ["## Manual Experiment Observations", ""]
    for cid, entry in sorted(notes.items()):
        if cid.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        error_codes = entry.get("error_codes") or []
        free_text = entry.get("free_text") or ""
        if not error_codes and not free_text:
            continue

        lines.append(f"### {cid}")
        if error_codes:
            for code in error_codes:
                lines.append(f"  - **{code}**: {error_label(code)}")
                impact = error_impact(code)
                if impact:
                    lines.append(f"    Impact: {impact}")
        if free_text:
            lines.append(f"  - Notes: {free_text}")
        lines.append("")

    return "\n".join(lines)


def is_candidate_mechanically_failed(
    candidate: Dict[str, Any],
    out_dir: Path,
    round_idx: int,
) -> bool:
    """Check if a candidate has critical error codes (rupture, no-gel, etc.)."""
    notes = load_notes(out_dir, round_idx)
    cid = candidate.get("candidate_id", "")
    entry = notes.get(cid)
    if not entry or not isinstance(entry, dict):
        return False
    error_codes: List[str] = entry.get("error_codes") or []
    return any(error_severity(e) == "critical" for e in error_codes)


def notes_inject_into_diagnosis_prompt(
    prompt: str,
    out_dir: Path,
    round_idx: int,
) -> str:
    """Inject experiment notes context into an existing diagnosis prompt."""
    ctx = build_notes_context_for_diagnosis(out_dir, round_idx)
    if not ctx:
        return prompt
    # Insert before the final JSON-output instruction
    marker = "请按照 JSON 格式输出"
    if marker in prompt:
        return prompt.replace(marker, ctx + "\n\n" + marker)
    return prompt + "\n\n" + ctx
