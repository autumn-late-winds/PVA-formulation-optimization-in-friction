"""Agent policy for safe operation around the constrained PVA workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentPolicy:
    """Hard operating boundaries for the outer project agent."""

    can_edit_candidate_json: bool = False
    can_bypass_constrained_doe: bool = False
    can_auto_introduce_new_materials: bool = False
    project_data_priority: str = "project_experiment_results_before_literature_priors"
    require_confirmation_actions: tuple[str, ...] = (
        "generate_round",
        "prepare_wetlab",
        "regenerate_round",
        "enable_limited_exploration",
        "declare_converged",
        "change_convergence_thresholds",
    )
    low_risk_auto_actions: tuple[str, ...] = (
        "inspect_workspace",
        "refresh_reports",
        "build_failure_memory",
        "build_vector_index",
    )


DEFAULT_POLICY = AgentPolicy()


def load_policy_text() -> str:
    """Return the human-readable policy if docs are installed."""

    docs_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "agent"
        / "agent_policy.md"
    )
    if docs_path.exists():
        return docs_path.read_text(encoding="utf-8")
    return (
        "The outer agent must preserve parent_candidate_id lineage, use the "
        "existing constrained workflow for R2+ generation, distinguish audit "
        "failure from experimental failure, and request confirmation for "
        "generation, regeneration, new materials, threshold changes, and "
        "convergence termination."
    )
