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
from .generator import run_generator, build_generator_prompt
from .constrained_doe import build_constrained_doe_skeleton
from .formula_materializer import materialize_constrained_candidates
from .artifact_store import RunWorkspace
from .audit import run_auditor_rulebased
from .workflow import (
    run_prepare_wetlab,
    run_diagnose,
    run_text_only_diagnose,
    check_material_completeness,
    normalize_materials_and_formulation,
)
from .simulation import simulate_results
from .experiment_notes import (
    load_notes,
    save_notes,
    write_notes_template,
    apply_notes_to_candidates,
    build_notes_context_for_diagnosis,
    known_error_codes,
    error_label,
)
from .llm_engines import MockLLM, TransformersLLM, VllmOpenAIChatLLM
from .config import (
    CONSTRAINTS,
    ACCEPTANCE,
    GenerationMode,
    FrictionPattern,
    CandidateDict,
)
from .utils import (
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
