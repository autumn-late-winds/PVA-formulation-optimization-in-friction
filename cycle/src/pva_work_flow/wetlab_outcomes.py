"""Shared wet-lab outcome classification and ranking helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .io_artifacts import aggregate_cof_from_row


NO_FAILURE_VALUES = {"", "none", "na", "n/a", "null", "no", "0"}
CRITICAL_NOTE_CODES = {"ERROR1", "ERROR2"}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def failure_type(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    return _norm(row.get("failure_type")).lower()


def has_failure(row: Mapping[str, Any] | None) -> bool:
    """Return True when a result row should not be ranked as a valid success."""
    if not row:
        return False
    failure = failure_type(row)
    if failure and failure not in NO_FAILURE_VALUES:
        return True
    notes = _norm(row.get("notes")).upper()
    cof_raw = _norm(row.get("cof_steady_mean")).upper()
    return any(code in notes or cof_raw == code for code in CRITICAL_NOTE_CODES)


def outcome_status(row: Mapping[str, Any] | None) -> str:
    """Classify one wet-lab row without mixing it with audit status."""
    if not row:
        return "not_measured"
    if has_failure(row):
        return "experimental_failed"
    cof, _std = aggregate_cof_from_row(dict(row))
    if cof is None:
        return "measured_without_cof"
    return "measured_valid"


def rank_key(row: Mapping[str, Any] | None) -> tuple[int, float, float]:
    """Lower is better; failed/missing rows are penalized before COF ranking."""
    if not row:
        return (3, 999.0, 999.0)
    cof, std = aggregate_cof_from_row(dict(row))
    cof_val = 999.0 if cof is None else float(cof)
    std_val = 999.0 if std is None else float(std)
    if has_failure(row):
        return (2, cof_val, std_val)
    if cof is None:
        return (1, 999.0, std_val)
    return (0, cof_val, std_val)


def performance_rank(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "unknown"
    if has_failure(row):
        return "failed"
    cof, _std = aggregate_cof_from_row(dict(row))
    if cof is None:
        return "unknown"
    if cof < 0.01:
        return "excellent"
    if cof < 0.02:
        return "good"
    if cof < 0.03:
        return "moderate"
    return "poor"


# ---------------------------------------------------------------------------
# Composite Viability Score (CVS)
# ---------------------------------------------------------------------------
# CVS = I × P × S
#
#   I = Integrity Gate  — multiplicative penalty for mechanical failure
#   P = Performance     — COF + wear + modulus + COF_std, each normalized to [0,1]
#   S = Stability       — stable_proportion + stick_slip + plateau_ratio
#
# Design rationale:
#   - I is multiplicative, NOT additive: rupture halves the score regardless of COF.
#   - P uses tanh for COF → smooth saturation, no hard thresholds.
#   - f_modulus is Gaussian around 2.0 MPa (cartilage-mimetic target).
#   - S rewards stable, repeatable friction cycles.
# ---------------------------------------------------------------------------

import math


# ---- Integrity multipliers per error code ----
INTEGRITY_BY_ERROR: dict[str, float] = {
    # critical — COF data unreliable or absent
    "ERROR1": 0.25,   # rupture during test: COF from pre-rupture window, directional info only
    "ERROR2": 0.00,   # no gelation: no data at all
    "ERROR3": 0.15,   # too soft to clamp: no tribology data
    # high — data partially compromised
    "ERROR4": 0.70,   # visible surface damage: COF data usable but wear is poor
    "ERROR6": 0.60,   # excessive swelling: dimensions unstable during test
    "ERROR8": 0.50,   # precipitate/phase separation: bulk inhomogeneity
    # medium — data mostly usable
    "ERROR5": 0.80,   # unusual noise: possible stick-slip artifact
    "ERROR7": 0.60,   # COF rising continuously: steady-state not reached
    "ERROR9": 0.85,   # poor reproducibility: batch variation
    # info — negligible
    "ERROR10": 0.95,
}

# Default when error_codes are unknown but failure_type column indicates failure
DEFAULT_FAILURE_INTEGRITY = 0.30

# Performance sub-score weights
W_COF = 0.40
W_WEAR = 0.25
W_MODULUS = 0.20
W_COF_STD = 0.15

# Stability sub-score weights
W_STABLE_PROP = 0.40
W_STICK_SLIP = 0.30
W_PLATEAU = 0.30

# Reference values for normalisation
COF_SCALE = 0.03          # tanh inflection point: COF=0.03 → f_COF=0.5
WEAR_CAP = 50.0           # wear_proxy above this → f_wear → 0
MODULUS_TARGET = 2.0      # ideal compression modulus (MPa), Gaussian centre
MODULUS_SIGMA = 1.5       # Gaussian width
COF_STD_CAP = 0.05        # COF_std above this → f_cof_std → 0


def _safe_float(row: Mapping[str, Any], key: str) -> float | None:
    raw = row.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _f_cof(cof: float) -> float:
    """Smooth COF score: 0→1, 0.03→0.5, 0.10→~0."""
    return 1.0 - math.tanh(cof / COF_SCALE)


def _f_wear(wear: float) -> float:
    """Linear wear score: 0→1, 50→0."""
    return max(0.0, 1.0 - wear / WEAR_CAP)


def _f_modulus(modulus: float) -> float:
    """Gaussian modulus score centred at MODULUS_TARGET."""
    return math.exp(-((modulus - MODULUS_TARGET) ** 2) / (2 * MODULUS_SIGMA ** 2))


def _f_cof_std(cof_std: float) -> float:
    """Linear COF stability score."""
    return max(0.0, 1.0 - cof_std / COF_STD_CAP)


def integrity_multiplier(
    error_codes: list[str] | None = None,
    failure_type_col: str = "",
) -> float:
    """Compute I ∈ [0, 1] from error codes or failure_type column.

    Returns the *minimum* multiplier across all error codes (most severe dominates).
    If no structured error codes are available, falls back to failure_type column.
    """
    if error_codes:
        multipliers = [
            INTEGRITY_BY_ERROR.get(code.strip().upper(), 1.0)
            for code in error_codes
        ]
        if multipliers:
            return min(multipliers)

    # Fallback: use the failure_type column from results CSV
    ft = _norm(failure_type_col).lower()
    if ft and ft not in NO_FAILURE_VALUES:
        # Don't know exact error code → apply conservative default
        return DEFAULT_FAILURE_INTEGRITY

    return 1.0


def compute_cvs(
    row: Mapping[str, Any] | None,
    error_codes: list[str] | None = None,
) -> dict:
    """Compute the Composite Viability Score for one candidate result row.

    Parameters
    ----------
    row : dict
        A row from R*_results_filled.csv (or equivalent dict).
    error_codes : list[str] | None
        Pre-resolved error codes (e.g. ["ERROR1"]). If None, inferred from row.

    Returns
    -------
    dict with keys:
        cvs           : float  — Composite Viability Score  [0, 100]
        i_multiplier  : float  — Integrity multiplier       [0, 1]
        p_score       : float  — Performance sub-score      [0, 100]
        s_score       : float  — Stability sub-score        [0, 1]
        f_cof         : float  — COF component              [0, 1]
        f_wear        : float  — Wear component             [0, 1]
        f_modulus     : float  — Modulus component          [0, 1]
        f_cof_std     : float  — COF_std component          [0, 1]
        cof_raw       : float | None
        grade         : str    — A+/A/B+/B/C/D/F
        warnings      : list[str]
    """
    result: dict = {
        "cvs": 0.0,
        "i_multiplier": 0.0,
        "p_score": 0.0,
        "s_score": 0.0,
        "f_cof": 0.0,
        "f_wear": 0.0,
        "f_modulus": 0.0,
        "f_cof_std": 0.0,
        "cof_raw": None,
        "grade": "F",
        "warnings": [],
    }

    if not row:
        result["warnings"].append("No data row provided")
        return result

    # ---- Resolve error codes ----
    resolved_codes: list[str] = list(error_codes) if error_codes else []

    # If no explicit codes, try to infer from notes / failure_type
    if not resolved_codes:
        ft_raw = _norm(row.get("failure_type")).lower()
        if ft_raw and ft_raw not in NO_FAILURE_VALUES:
            resolved_codes.append(ft_raw.upper())

        # Scan notes column for ERROR* markers
        notes_raw = _norm(row.get("notes")).upper()
        for code in INTEGRITY_BY_ERROR:
            if code in notes_raw:
                if code not in resolved_codes:
                    resolved_codes.append(code)

    # ---- I: Integrity ----
    ft_col = _norm(row.get("failure_type"))
    i = integrity_multiplier(resolved_codes, ft_col)
    result["i_multiplier"] = i

    if i < 1.0:
        result["warnings"].append(
            f"Mechanical integrity penalty applied: I={i:.2f} "
            f"(codes={resolved_codes or ['unknown_failure']})"
        )

    # ---- P: Performance ----
    cof = _safe_float(row, "cof_steady_mean")
    wear = _safe_float(row, "wear_proxy")
    modulus = _safe_float(row, "compression_modulus_MPa")
    cof_std = _safe_float(row, "cof_std")

    result["cof_raw"] = cof

    f_cof_val = _f_cof(cof) if cof is not None else 0.0
    f_wear_val = _f_wear(wear) if wear is not None else 0.5   # neutral if missing
    f_modulus_val = _f_modulus(modulus) if modulus is not None else 0.5
    f_cof_std_val = _f_cof_std(cof_std) if cof_std is not None else 0.5

    result["f_cof"] = round(f_cof_val, 4)
    result["f_wear"] = round(f_wear_val, 4)
    result["f_modulus"] = round(f_modulus_val, 4)
    result["f_cof_std"] = round(f_cof_std_val, 4)

    p = 100.0 * (
        W_COF * f_cof_val
        + W_WEAR * f_wear_val
        + W_MODULUS * f_modulus_val
        + W_COF_STD * f_cof_std_val
    )
    result["p_score"] = round(p, 2)

    # ---- S: Stability ----
    stable_prop = _safe_float(row, "stable_proportion")
    stick_slip = _safe_float(row, "stick_slip_score")

    # plateau_ratio: use pos_plateau_ratio and neg_plateau_ratio if available,
    # otherwise fall back to the generic plateau_ratio column
    pos_plat = _safe_float(row, "pos_plateau_ratio")
    neg_plat = _safe_float(row, "neg_plateau_ratio")
    if pos_plat is not None and neg_plat is not None:
        # For asymmetric patterns, the limiting direction matters more
        plateau = min(pos_plat, neg_plat)
    else:
        plateau = _safe_float(row, "plateau_ratio")

    s_stable = stable_prop if stable_prop is not None else 0.5
    s_ss = max(0.0, 1.0 - stick_slip) if stick_slip is not None else 0.5
    s_plat = plateau if plateau is not None else 0.5

    s = (W_STABLE_PROP * s_stable + W_STICK_SLIP * s_ss + W_PLATEAU * s_plat)
    # If all three components are at their neutral 0.5 defaults, cap S at 0.5
    if stable_prop is None and stick_slip is None and plateau is None:
        s = 0.5
    result["s_score"] = round(min(s, 1.0), 4)

    # ---- CVS ----
    cvs = i * (p / 100.0) * s * 100.0
    result["cvs"] = round(cvs, 2)

    # ---- Grade ----
    if cvs >= 85:
        result["grade"] = "A+"
    elif cvs >= 75:
        result["grade"] = "A"
    elif cvs >= 65:
        result["grade"] = "B+"
    elif cvs >= 55:
        result["grade"] = "B"
    elif cvs >= 40:
        result["grade"] = "C"
    elif cvs >= 20:
        result["grade"] = "D"
    else:
        result["grade"] = "F"

    return result


def cvs_rank_key(
    row: Mapping[str, Any] | None,
    error_codes: list[str] | None = None,
) -> float:
    """Convenience: return -CVS so that higher CVS sorts first with ascending sort."""
    return -compute_cvs(row, error_codes)["cvs"]
