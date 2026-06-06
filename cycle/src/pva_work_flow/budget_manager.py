"""Experiment budget and stage management.

Tracks completed wet-lab experiments, infers the current optimization
stage, and recommends round shapes to keep the project within its budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .utils import read_json

# ---- Budget configuration ----
TOTAL_FORMULA_BUDGET = 100
MAX_FORMULAS_PER_ROUND = 8
MIN_REPLICATES_FOR_BEST = 3
MAX_LIMITED_EXPLORATION_FRACTION = 0.25
MAX_EXPLORATION_TOTAL = 25

STAGE_THRESHOLDS = {
    "screening": (0, 20),
    "local_optimization": (20, 50),
    "mechanism_validation": (50, 75),
    "robustness_validation": (75, 101),
}


# ---- Stage inference ----
def infer_stage(
    completed_unique_formulas: int,
    best_candidate_repeats: int = 0,
    remaining_budget: int | None = None,
) -> str:
    """Return the current experiment stage.

    Thresholds from the project difficulty-reduction codex plan §11:
        screening:             0–19 completed
        local_optimization:   20–49 completed
        mechanism_validation: 50–74 completed
        robustness_validation: 75–100 completed
    """
    if remaining_budget is None:
        remaining_budget = TOTAL_FORMULA_BUDGET - completed_unique_formulas

    for stage, (lo, hi) in STAGE_THRESHOLDS.items():
        if lo <= completed_unique_formulas < hi:
            return stage
    return "robustness_validation"


# ---- Budget counting ----
def _result_files(run_dir: Path) -> list[Path]:
    files = list(run_dir.glob("R*_results_filled.csv"))
    state_dir = run_dir / "run_state_files"
    if state_dir.is_dir():
        files.extend(state_dir.glob("R*_results_filled.csv"))
    trees_dir = run_dir / "trees"
    if trees_dir.is_dir():
        for tree_dir in sorted(p for p in trees_dir.iterdir() if p.is_dir()):
            files.extend(tree_dir.glob("R*_results_filled.csv"))
    return sorted(set(files))


def count_completed_formulas(run_dir: Path) -> int:
    """Count unique candidate IDs with a measured results row across all rounds.

    Walks R{N}_results_filled.csv files; each row with a non-empty
    cof_steady_mean is counted as one completed wet-lab experiment.
    """
    import csv

    seen: set[tuple[str, str]] = set()
    for p in _result_files(run_dir):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    cid = (row.get("candidate_id") or row.get("formula_id") or "").strip()
                    cof = row.get("cof_steady_mean") or row.get("COF_steady_mean") or ""
                    if cid and cof:
                        seen.add((p.parent.name, cid))
        except Exception:
            continue
    return len(seen)


def count_completed_by_design_type(run_dir: Path) -> Dict[str, int]:
    """Count completed experiments grouped by design_type."""
    import csv

    counts: Dict[str, int] = {}
    for p in _result_files(run_dir):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    cof = row.get("cof_steady_mean") or row.get("COF_steady_mean") or ""
                    if not cof:
                        continue
                    dt = (row.get("design_type") or "unknown").strip()
                    counts[dt] = counts.get(dt, 0) + 1
        except Exception:
            continue
    return counts


def get_remaining_budget(completed: int, total: int = TOTAL_FORMULA_BUDGET) -> int:
    return max(0, total - completed)


def count_repeats_for_best(run_dir: Path) -> tuple[int, str | None]:
    """Return (repeat_count, best_candidate_id) for the lowest-COF candidate.

    Walks all results CSVs; the candidate with the lowest cof_steady_mean
    is considered best.
    """
    import csv

    best_id: str | None = None
    best_cof: float = float("inf")
    counts: Dict[str, int] = {}

    for p in _result_files(run_dir):
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    cid = (row.get("candidate_id") or row.get("formula_id") or "").strip()
                    cof_str = row.get("cof_steady_mean") or row.get("COF_steady_mean") or ""
                    if not cid or not cof_str:
                        continue
                    try:
                        cof = float(cof_str)
                    except ValueError:
                        continue
                    counts[cid] = counts.get(cid, 0) + 1
                    if cof < best_cof:
                        best_cof = cof
                        best_id = cid
        except Exception:
            continue
    return (counts.get(best_id or "", 0), best_id)


# ---- Round shape recommendation ----
def recommend_round_shape(
    stage: str,
    remaining_budget: int,
    best_cof: float | None = None,
) -> Dict[str, Any]:
    """Return a recommended round configuration based on budget and stage."""
    shape: Dict[str, Any] = {
        "stage": stage,
        "remaining_budget": remaining_budget,
        "round_size": 4,
        "allow_limited_exploration": False,
        "required_design_types": [
            "baseline_reproduction",
            "single_factor_perturbation",
            "local_optimization",
        ],
        "min_repeats_for_best": 0,
        "warnings": [],
    }

    if remaining_budget <= 0:
        shape["round_size"] = 0
        shape["allow_limited_exploration"] = False
        shape["warnings"].append(
            "No experiment budget remains; stop experiments and do not generate a new round."
        )
        return shape

    # Shrink round size near budget exhaustion
    if remaining_budget <= 10:
        shape["round_size"] = 2
        shape["warnings"].append(
            f"Only {remaining_budget} experiments remain; round size reduced to 2."
        )
    elif remaining_budget <= 20:
        shape["round_size"] = 3
        shape["warnings"].append(
            f"Only {remaining_budget} experiments remain; consider focusing on replication."
        )

    # Stage-specific adjustments
    if stage == "screening":
        shape["allow_limited_exploration"] = remaining_budget > 50
    elif stage == "local_optimization":
        shape["allow_limited_exploration"] = remaining_budget > 30
    elif stage == "mechanism_validation":
        shape["round_size"] = min(shape["round_size"], 3)
        shape["allow_limited_exploration"] = False
    elif stage == "robustness_validation":
        shape["round_size"] = min(shape["round_size"], 3)
        shape["allow_limited_exploration"] = False
        shape["min_repeats_for_best"] = MIN_REPLICATES_FOR_BEST
        shape["required_design_types"] = ["baseline_reproduction"]
        if remaining_budget >= 5:
            shape["required_design_types"].append("single_factor_perturbation")

    return shape


# ---- Gate checks ----
def exploration_allowed(
    stage: str,
    completed_exploration: int,
    max_fraction: float = MAX_LIMITED_EXPLORATION_FRACTION,
    max_total: int = MAX_EXPLORATION_TOTAL,
) -> bool:
    if stage in ("mechanism_validation", "robustness_validation"):
        return False
    if completed_exploration >= max_total:
        return False
    return True


def replication_needed(best_id: str | None, run_dir: Path, min_repeats: int = MIN_REPLICATES_FOR_BEST) -> bool:
    if not best_id:
        return False
    repeats, _ = count_repeats_for_best(run_dir)
    return repeats < min_repeats


def budget_exhaustion_warnings(completed: int, total: int = TOTAL_FORMULA_BUDGET) -> List[str]:
    warnings: List[str] = []
    remaining = total - completed
    if remaining <= 5:
        warnings.append("CRITICAL: 5 or fewer experiments remain. Run replicates only.")
    elif remaining <= 10:
        warnings.append("WARNING: 10 or fewer experiments remain. Disable exploration.")
    elif remaining <= 25:
        warnings.append("Budget below 25%%; consider narrowing to replication and confirmation.")
    return warnings
