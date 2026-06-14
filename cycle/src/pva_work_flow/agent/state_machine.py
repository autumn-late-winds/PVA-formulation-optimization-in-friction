"""Read-only workspace state machine for the outer operation agent."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from pva_work_flow.artifacts.artifact_store import RunWorkspace
from pva_work_flow.core.utils import read_json


@dataclass
class AgentState:
    state: str
    out_dir: str
    round: int | None
    evidence: list[str]
    missing: list[str]
    recommended_action: str
    safe_to_auto_run: bool
    requires_confirmation: bool
    command: str
    warnings: list[str]
    budget: dict[str, Any]
    statuses: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_agent_state(out_dir: Path) -> AgentState:
    """Classify the run directory without modifying any artifacts."""

    ws = RunWorkspace(out_dir)
    statuses = ws.all_statuses()
    action = ws.next_action()
    budget = action.get("budget", {})
    evidence: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []

    if not statuses:
        return AgentState(
            state="empty_workspace",
            out_dir=str(out_dir),
            round=None,
            evidence=["No R*_candidates.json or R*_results_filled.csv artifacts were found."],
            missing=["R1_candidates.json"],
            recommended_action="create_r1",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command="python -m pva_work_flow.cli --mode generate --round 1 --out_dir <run_dir>",
            warnings=["Initial R1 generation may create new candidate formulas."],
            budget=budget,
            statuses=[],
        )

    latest = statuses[-1]
    round_idx = int(latest["round"])
    evidence.extend(_status_evidence(latest))

    if latest["raw_friction_csv_count"] and not latest["results_filled"]:
        missing.append(f"R{round_idx}_results_filled.csv")
        return AgentState(
            state="raw_csv_ready",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence,
            missing=missing,
            recommended_action="sync_results",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command=f"python -m pva_work_flow.cli --sync_results {out_dir}",
            warnings=["This writes derived results and refreshes memories/reports from raw CSV files."],
            budget=budget,
            statuses=statuses,
        )

    if latest["candidates"] and not latest["audits"]:
        missing.append(f"R{round_idx}_audits.json")
        return AgentState(
            state="candidates_ready",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence,
            missing=missing,
            recommended_action="prepare_wetlab",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command=f"python -m pva_work_flow.cli --mode prepare --round {round_idx} --out_dir {out_dir}",
            warnings=["Preparation writes audit, DOE, and wet-lab template artifacts."],
            budget=budget,
            statuses=statuses,
        )

    if latest["results_filled"] and not latest["diagnosis"]:
        missing.append(f"R{round_idx}_diagnosis.json")
        return AgentState(
            state="results_synced",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence,
            missing=missing,
            recommended_action="diagnose_round",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command=f"python -m pva_work_flow.cli --mode diagnose --round {round_idx} --out_dir {out_dir}",
            warnings=["Diagnosis calls the configured LLM engine and writes diagnosis/KPI artifacts."],
            budget=budget,
            statuses=statuses,
        )

    if latest["doe_plan_kind"] == "legacy_or_llm" and round_idx >= 2:
        warnings.append("This round appears to use a legacy or LLM-only DOE plan.")
        return AgentState(
            state="needs_human_review",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence,
            missing=[],
            recommended_action="review_or_regenerate_round",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command=f"python -m pva_work_flow.cli --regenerate_round {round_idx} --archive_old --out_dir {out_dir}",
            warnings=warnings + ["Regeneration archives and rewrites generated artifacts."],
            budget=budget,
            statuses=statuses,
        )

    convergence = _latest_convergence(ws, round_idx)
    if convergence.get("is_converged") is True or convergence.get("converged") is True:
        return AgentState(
            state="converged_candidate_found",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence + ["Latest diagnosis reports convergence."],
            missing=[],
            recommended_action="human_confirm_convergence",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command="Review R{N}_diagnosis.json and wet-lab repeats before stopping.",
            warnings=["Do not stop the experiment solely from an automated convergence flag."],
            budget=budget,
            statuses=statuses,
        )

    if latest["diagnosis"]:
        next_round = round_idx + 1
        branch_warning = _branch_warnings(out_dir)
        return AgentState(
            state="ready_for_next_round",
            out_dir=str(out_dir),
            round=round_idx,
            evidence=evidence,
            missing=[],
            recommended_action="generate_round",
            safe_to_auto_run=False,
            requires_confirmation=True,
            command=f"python -m pva_work_flow.cli --mode generate --round {next_round} --out_dir {out_dir}",
            warnings=branch_warning + [
                "Choose a target_parent_id explicitly for R2+ tree expansion before generation."
            ],
            budget=budget,
            statuses=statuses,
        )

    return AgentState(
        state="needs_human_review",
        out_dir=str(out_dir),
        round=round_idx,
        evidence=evidence,
        missing=[],
        recommended_action=action.get("action", "inspect_missing_artifacts"),
        safe_to_auto_run=False,
        requires_confirmation=True,
        command=str(action.get("command", "inspect artifacts manually")),
        warnings=warnings or ["The artifact combination does not match a simple automatic path."],
        budget=budget,
        statuses=statuses,
    )


def _status_evidence(status: dict[str, Any]) -> list[str]:
    round_idx = status.get("round", "?")
    evidence = []
    if status.get("candidates"):
        evidence.append(f"R{round_idx}_candidates.json present")
    if status.get("audits"):
        evidence.append(f"R{round_idx}_audits.json present")
    if status.get("raw_friction_csv_count"):
        evidence.append(f"R{round_idx}/ has {status['raw_friction_csv_count']} raw friction CSV files")
    if status.get("raw_compression_csv_count"):
        evidence.append(f"R{round_idx}_compression/ has {status['raw_compression_csv_count']} compression CSV files")
    if status.get("results_filled"):
        evidence.append(f"R{round_idx}_results_filled.csv present")
    if status.get("diagnosis"):
        evidence.append(f"R{round_idx}_diagnosis.json present")
    if status.get("inheritance_table"):
        evidence.append(f"R{round_idx}_inheritance_table.md present")
    if status.get("doe_plan_kind") != "missing":
        evidence.append(f"DOE plan: {status.get('doe_plan_kind')}")
    return evidence


def _latest_convergence(ws: RunWorkspace, round_idx: int) -> dict[str, Any]:
    diag_path = ws.diagnosis_path(round_idx)
    if not diag_path.exists():
        return {}
    try:
        diag = read_json(diag_path)
    except Exception:
        return {}
    convergence = diag.get("convergence")
    return convergence if isinstance(convergence, dict) else {}


def _branch_warnings(out_dir: Path) -> list[str]:
    warnings: list[str] = []
    decisions_path = out_dir / "formula_branch_decisions.json"
    if not decisions_path.exists():
        return warnings
    try:
        obj = read_json(decisions_path)
    except Exception as exc:
        return [f"Could not read formula_branch_decisions.json: {exc}"]
    decisions = obj.get("decisions", obj if isinstance(obj, list) else [])
    killed = [
        str(d.get("candidate_id") or d.get("node_id"))
        for d in decisions
        if isinstance(d, dict) and d.get("branch_status") == "kill"
    ]
    if killed:
        warnings.append(
            "Do not expand killed branch nodes: " + ", ".join(killed[:8])
        )
    return warnings
