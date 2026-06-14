"""Tool registry and low-risk executors for the outer operation agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pva_work_flow.artifacts.artifact_store import RunWorkspace


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    risk_level: str
    requires_confirmation: bool
    writes_artifacts: bool
    command_template: str
    executor: Callable[..., dict[str, Any]] | None = None


def inspect_workspace(out_dir: Path) -> dict[str, Any]:
    ws = RunWorkspace(out_dir)
    return {
        "tool": "inspect_workspace",
        "out_dir": str(out_dir),
        "statuses": ws.all_statuses(),
        "next_action": ws.next_action(),
        "report": ws.format_status_report(),
    }


def refresh_reports(out_dir: Path) -> dict[str, Any]:
    written: list[str] = []
    warnings: list[str] = []
    builders = [
        ("pva_work_flow.tree.formula_tree", "build_tree"),
        ("pva_work_flow.tree.tree_statistics", "build_tree_statistics"),
        ("pva_work_flow.memory.chain_memory", "build_chain_memory"),
        ("pva_work_flow.tree.tree_reports", "build_tree_reports"),
        ("pva_work_flow.tree.tree_visualizer", "build_tree_diagram"),
    ]
    for module_name, fn_name in builders:
        try:
            module = __import__(module_name, fromlist=[fn_name])
            fn = getattr(module, fn_name)
            fn(out_dir)
            written.append(fn_name)
        except Exception as exc:  # report all rebuild problems without hiding others
            warnings.append(f"{fn_name}: {exc}")
    return {
        "tool": "refresh_reports",
        "out_dir": str(out_dir),
        "executed": written,
        "warnings": warnings,
    }


def build_failure_memory(out_dir: Path) -> dict[str, Any]:
    from pva_work_flow.memory.failure_factor_memory import build_failure_factor_memory

    records = build_failure_factor_memory(out_dir)
    return {
        "tool": "build_failure_memory",
        "out_dir": str(out_dir),
        "record_count": len(records),
        "artifacts": [
            str(out_dir / "failure_factor_memory.jsonl"),
            str(out_dir / "experiment_contrast_memory.jsonl"),
            str(out_dir / "FAILURE_FACTOR_SUMMARY.md"),
            str(out_dir / "NEXT_VERIFICATION_PLAN.md"),
        ],
    }


def build_vector_index(out_dir: Path) -> dict[str, Any]:
    from pva_work_flow.memory.formulation_rag import resolve_formulation_rag_db
    from pva_work_flow.memory.vector_rag import build_project_vector_index

    db_path = resolve_formulation_rag_db()
    index = build_project_vector_index(
        out_dir,
        formulation_db=db_path if db_path.exists() else None,
    )
    return {
        "tool": "build_vector_index",
        "out_dir": str(out_dir),
        "doc_count": index.get("doc_count", 0),
        "backend": index.get("embedding_backend"),
        "artifact": str(out_dir / "rag_vector_index.json"),
    }


TOOL_REGISTRY: dict[str, AgentTool] = {
    "inspect_workspace": AgentTool(
        name="inspect_workspace",
        description="Read run artifacts and summarize current workflow state.",
        risk_level="low",
        requires_confirmation=False,
        writes_artifacts=False,
        command_template="python -m pva_work_flow.cli --agent --out_dir <run_dir>",
        executor=inspect_workspace,
    ),
    "sync_results": AgentTool(
        name="sync_results",
        description="Build R*_results_filled.csv from raw Bruker CSV directories.",
        risk_level="medium",
        requires_confirmation=True,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --sync_results <run_dir>",
    ),
    "diagnose_round": AgentTool(
        name="diagnose_round",
        description="Run LLM diagnosis for a round that already has results_filled.",
        risk_level="medium",
        requires_confirmation=True,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --mode diagnose --round <N> --out_dir <run_dir>",
    ),
    "generate_round": AgentTool(
        name="generate_round",
        description="Generate next-round candidates through the constrained workflow.",
        risk_level="high",
        requires_confirmation=True,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --mode generate --round <N> --target_parent_id <R*-*> --out_dir <run_dir>",
    ),
    "prepare_wetlab": AgentTool(
        name="prepare_wetlab",
        description="Audit candidates and export DOE/template files for wet-lab execution.",
        risk_level="medium",
        requires_confirmation=True,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --mode prepare --round <N> --out_dir <run_dir>",
    ),
    "refresh_reports": AgentTool(
        name="refresh_reports",
        description="Rebuild tree, statistics, chain memory, and report artifacts.",
        risk_level="low",
        requires_confirmation=False,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --agent --agent_execute refresh_reports --out_dir <run_dir>",
        executor=refresh_reports,
    ),
    "build_failure_memory": AgentTool(
        name="build_failure_memory",
        description="Rebuild failure-factor memory and verification reports.",
        risk_level="low",
        requires_confirmation=False,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --agent --agent_execute build_failure_memory --out_dir <run_dir>",
        executor=build_failure_memory,
    ),
    "build_vector_index": AgentTool(
        name="build_vector_index",
        description="Build the local TF-IDF RAG vector index.",
        risk_level="low",
        requires_confirmation=False,
        writes_artifacts=True,
        command_template="python -m pva_work_flow.cli --agent --agent_execute build_vector_index --out_dir <run_dir>",
        executor=build_vector_index,
    ),
}


def run_low_risk_tool(tool_name: str, out_dir: Path) -> dict[str, Any]:
    tool = TOOL_REGISTRY.get(tool_name)
    if tool is None:
        raise ValueError(f"Unknown agent tool: {tool_name}")
    if tool.requires_confirmation or tool.risk_level != "low":
        raise PermissionError(
            f"{tool_name} is {tool.risk_level}-risk and requires explicit workflow confirmation. "
            f"Suggested command: {tool.command_template}"
        )
    if tool.executor is None:
        raise RuntimeError(f"{tool_name} has no in-process executor")
    return tool.executor(out_dir)
