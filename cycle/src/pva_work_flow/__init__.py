"""PVA hydrogel closed-loop experimental design workflow.

Main entry points:
- ``python -m pva_work_flow.cli`` — CLI for the full pipeline
- Programmatic use: import from the top-level package.

Modules:
- ``generator`` — LLM-driven candidate generation and prompt building
- ``constrained_doe`` — Code-generated small-step DOE skeletons for R2+
- ``audit`` — Chemical feasibility rules and audit pipeline
- ``workflow`` — Formulation checks, diagnosis, wet-lab preparation
- ``ratio_planner`` — Deterministic LHS-based composition planning
- ``utils`` — JSON/CSV I/O, Bruker UMT data parsing, friction analysis
- ``llm_engines`` — LLM backends (Mock, vLLM, HuggingFace Transformers)
- ``io_artifacts`` — CSV export/import and KPI computation
- ``config`` — Constants, enums, and TypedDict definitions
- ``simulation`` — Wet-lab result simulation for workflow testing
"""

# ── Top-level public API ───────────────────────────────────────────
from pva_work_flow.planning.generator import run_generator, build_generator_prompt
from pva_work_flow.planning.constrained_doe import build_constrained_doe_skeleton
from pva_work_flow.planning.formula_materializer import materialize_constrained_candidates
from pva_work_flow.artifacts.artifact_store import RunWorkspace
from pva_work_flow.planning.audit import run_auditor_rulebased
from pva_work_flow.orchestration.workflow import (
    run_prepare_wetlab,
    run_diagnose,
    run_text_only_diagnose,
    check_material_completeness,
    normalize_materials_and_formulation,
)
from pva_work_flow.wetlab.simulation import simulate_results
from pva_work_flow.artifacts.experiment_notes import (
    load_notes,
    save_notes,
    write_notes_template,
    apply_notes_to_candidates,
    build_notes_context_for_diagnosis,
    known_error_codes,
    error_label,
)
from pva_work_flow.core.llm_engines import MockLLM, TransformersLLM, VllmOpenAIChatLLM
from pva_work_flow.core.config import (
    CONSTRAINTS,
    ACCEPTANCE,
    GenerationMode,
    FrictionPattern,
    CandidateDict,
)
from pva_work_flow.core.utils import (
    ensure_dir,
    read_json,
    write_json,
    safe_json_loads,
    load_allowed_materials,
    parse_bruker_csv,
    discriminate_pattern,
    plot_fx_vs_t,
    build_results_from_bruker_csvs,
)


# Backward-compatible module aliases.
#
# Older scripts may still import modules from the package root, for example
# ``pva_work_flow.generator``. The implementations now live in functional
# subpackages, but these aliases keep those imports working.
import importlib as _importlib
import sys as _sys

_LEGACY_MODULE_ALIASES = {
    "artifact_store": "artifacts.artifact_store",
    "audit": "planning.audit",
    "bruker_parser": "wetlab.bruker_parser",
    "budget_manager": "orchestration.budget_manager",
    "candidate_critic": "planning.candidate_critic",
    "candidate_rules": "planning.candidate_rules",
    "chain_memory": "memory.chain_memory",
    "chain_search": "tree.chain_search",
    "config": "core.config",
    "constrained_doe": "planning.constrained_doe",
    "experiment_notes": "artifacts.experiment_notes",
    "experiment_rag": "memory.experiment_rag",
    "experiment_state": "artifacts.experiment_state",
    "failure_factor_memory": "memory.failure_factor_memory",
    "formulation_checks": "planning.formulation_checks",
    "formulation_rag": "memory.formulation_rag",
    "formula_materializer": "planning.formula_materializer",
    "formula_tree": "tree.formula_tree",
    "generator": "planning.generator",
    "io_artifacts": "artifacts.io_artifacts",
    "llm_engines": "core.llm_engines",
    "pipeline_agents": "planning.pipeline_agents",
    "ratio_planner": "planning.ratio_planner",
    "rule_checker": "planning.rule_checker",
    "simulation": "wetlab.simulation",
    "tree_naming": "tree.tree_naming",
    "tree_reports": "tree.tree_reports",
    "tree_statistics": "tree.tree_statistics",
    "tree_visualizer": "tree.tree_visualizer",
    "utils": "core.utils",
    "vector_rag": "memory.vector_rag",
    "wetlab_metrics": "wetlab.wetlab_metrics",
    "wetlab_outcomes": "wetlab.wetlab_outcomes",
    "workflow": "orchestration.workflow",
}

for _old_name, _new_name in _LEGACY_MODULE_ALIASES.items():
    _sys.modules.setdefault(
        f"{__name__}.{_old_name}",
        _importlib.import_module(f".{_new_name}", __name__),
    )

del _importlib, _sys, _old_name, _new_name
