from enum import Enum
from typing import Dict, List, Any, TypedDict


class CandidateDict(TypedDict, total=False):
    """TypedDict for candidate formulations flowing through the pipeline.

    Fields are added incrementally: generation populates core fields,
    audit adds status/check fields, diagnosis adds repair fields.
    All fields are NotRequired (total=False) to reflect this.
    """
    # Core identity
    candidate_id: str
    generation_mode: str
    parent_candidate_id: str
    parent_candidates: List[str]

    # Iteration metadata
    iteration_metadata: Dict[str, Any]
    diagnosis_evidence_used: List[str]
    mutation_rationale: str
    diagnosis_levers_used: List[str]
    doe_factor_levels: Dict[str, str]
    doe_factor_levels_used: Dict[str, str]
    doe_compliance: bool
    outside_doe_space: bool
    is_extension: bool
    extension_reason: str

    # Formulation
    formulation: Dict[str, Any]
    materials: List[Dict[str, Any]]
    materials_complete: bool
    formulation_complete: bool
    materials_vs_formulation_consistency: bool
    formulation_role_mapping_complete: bool

    # Processing
    process: Dict[str, Any]
    processing: Dict[str, Any]
    total_prep_time_hours: float
    fits_one_day_requirement: bool

    # Risk & mechanism
    expected_mechanism: List[str]
    risks_and_mitigations: List[Dict[str, str]]
    notes: str
    predicted_tradeoff: Dict[str, str]
    confidence: float
    missing_info: List[str]

    # Audit fields
    audit_status: str
    rejection_reason: str | None
    hard_constraint_failures: List[str]

    # Design fields
    design_role: str
    recommended_repeats: int
    optimization_phase: str
    ratio_source: str
    ratio_rationale: List[str]
    ratio_planner: Dict[str, Any]

    # Meta
    _material_suggestions: Dict[str, Any]
    _auto_corrections: List[str]
    last_valid_experimental_round: int | None
    last_failed_audit_round: int | None


# -------------------- Fixed constraints --------------------
CONSTRAINTS = {
    "load_N": 10.0,
    "counterface": "stainless_steel_ball",
    "medium": "DI_water",
    "temperature": "25℃_room_temp",
    "speed_mm_s": 5.0,
}

# Acceptance criteria (example; adjust to your own target)
ACCEPTANCE = {
    "cof_steady_max": 0.06,
    "cof_std_max": 0.01,
    "no_failure": True,  # failure_type must be "none"
}

# Convergence criteria — multi-round optimization stop conditions.
# When ALL criteria are met, the system declares convergence and recommends
# entering robustness/repeatability validation instead of further iteration.
CONVERGENCE = {
    "cof_max": 0.02,             # steady-state COF must be <= this value
    "modulus_min_mpa": 1.5,      # compression modulus lower bound (MPa)
    "modulus_max_mpa": 2.5,      # compression modulus upper bound (MPa)
    "stable_proportion_min": 0.6, # fraction of half-cycles with clear plateaus
    "stick_slip_max": 0.2,       # high-frequency oscillation score upper bound
    "cof_trend_delta": 0.005,    # |COF_round - COF_prev_round| below this = flat
    "cof_trend_consecutive": 2,  # number of consecutive flat rounds to declare convergence
}

# Budget and stage configuration (mirrors constrained_planning_policy.yaml)
BUDGET = {
    "total_formula_budget": 100,
    "max_formulas_per_round": 8,
    "min_replicates_for_best": 3,
    "max_limited_exploration_fraction": 0.25,
    "max_exploration_total": 25,
    "stage_thresholds": {
        "screening": (0, 20),
        "local_optimization": (20, 50),
        "mechanism_validation": (50, 75),
        "robustness_validation": (75, 101),
    },
}


class GenerationMode(str, Enum):
    FALLBACK = "fallback"
    RESULT_DRIVEN = "result_driven"
    DIAGNOSIS_DRIVEN = "diagnosis_driven"


class FrictionPattern(str, Enum):
    GOOD = "good"
    TRIANGULAR = "triangular"
    IRREGULAR = "irregular"
    STICK_SLIP = "stick_slip"
    ASYMMETRIC = "asymmetric"
