"""Planner for the outer operation agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .policy import DEFAULT_POLICY
from .state_machine import AgentState, inspect_agent_state
from .tools import TOOL_REGISTRY


def build_agent_advice(out_dir: Path) -> dict[str, Any]:
    """Return structured advice for the next safe operation."""

    state = inspect_agent_state(out_dir)
    tool = TOOL_REGISTRY.get(state.recommended_action)
    advice = {
        "state": state.to_dict(),
        "recommended_tool": tool.name if tool else state.recommended_action,
        "risk_level": tool.risk_level if tool else "unknown",
        "requires_confirmation": (
            state.requires_confirmation
            or (tool.requires_confirmation if tool else True)
            or state.recommended_action in DEFAULT_POLICY.require_confirmation_actions
        ),
        "can_auto_execute": (
            state.safe_to_auto_run
            and tool is not None
            and not tool.requires_confirmation
            and tool.risk_level == "low"
        ),
        "command": _fill_command(state, tool.command_template if tool else state.command),
        "reasoning": _reasoning_for_state(state),
        "policy_reminders": [
            "Do not edit candidate JSON directly.",
            "Do not bypass constrained DOE for R2+ generation.",
            "Project experiment results outrank literature priors.",
            "New materials and convergence termination require human confirmation.",
        ],
    }
    return advice


def _fill_command(state: AgentState, template: str) -> str:
    command = template.replace("<run_dir>", state.out_dir)
    if state.round is not None:
        command = command.replace("<N>", str(state.round))
        command = command.replace("R{N}", f"R{state.round}")
    return command


def _reasoning_for_state(state: AgentState) -> list[str]:
    if state.state == "empty_workspace":
        return [
            "No round artifacts were found.",
            "The workflow must start from R1 candidates or an explicit R1 generation step.",
        ]
    if state.state == "raw_csv_ready":
        return [
            "Raw Bruker CSV files are present but structured results are missing.",
            "Diagnosis and next-round planning require R*_results_filled.csv.",
        ]
    if state.state == "candidates_ready":
        return [
            "Candidates exist but audit/DOE/template artifacts are missing.",
            "Wet-lab execution should use audited candidates and exported templates.",
        ]
    if state.state == "results_synced":
        return [
            "Structured experimental results exist.",
            "The next information-producing step is diagnosis and convergence assessment.",
        ]
    if state.state == "ready_for_next_round":
        return [
            "The latest round has diagnosis output.",
            "A new generation step is possible, but R2+ should use an explicit single parent node.",
        ]
    if state.state == "converged_candidate_found":
        return [
            "The latest diagnosis reports convergence.",
            "Stopping or locking a formula is a research decision and needs human review.",
        ]
    return [
        "The artifact combination is not a simple low-risk path.",
        "Inspect the listed evidence and missing artifacts before executing writes.",
    ]
