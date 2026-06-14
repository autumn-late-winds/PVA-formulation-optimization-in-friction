"""Bruker UMT CSV parsing, friction pattern analysis, and compression modulus computation."""

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

# -------------------- Bruker CSV analysis --------------------
def parse_bruker_csv(csv_path: Path) -> Dict[str, Any]:
    """Parse a Bruker UMT CSV export file.

    Returns dict with:
        - metadata: dict of file-level metadata
        - runs: list of runs, each containing steps list
          Each step: {step_no, params: dict, columns: [str], data: List[dict]}
    """
    text = csv_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    metadata: Dict[str, str] = {}
    runs: List[Dict[str, Any]] = []
    current_run: Dict[str, Any] | None = None
    current_step: Dict[str, Any] | None = None
    data_rows: List[Dict[str, float]] = []
    columns: List[str] = []
    mode = "metadata"  # metadata | run_header | step_params | data

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if mode == "data" and current_step is not None and data_rows:
                current_step["data"] = data_rows
                data_rows = []
                mode = "step_params"
            # If in data mode with columns but no data yet, stay in data mode
            continue

        # Detect Run header
        if stripped.startswith("Run #"):
            if current_run and current_step:
                if data_rows:
                    current_step["data"] = data_rows
                    data_rows = []
                current_run.setdefault("steps", []).append(current_step)
            if current_run:
                runs.append(current_run)
            current_run = {"run_id": stripped, "steps": []}
            current_step = None
            mode = "run_header"
            continue

        # Detect Step header
        if stripped.startswith("Step No."):
            if current_step is not None and current_run is not None:
                if data_rows:
                    current_step["data"] = data_rows
                    data_rows = []
                current_run.setdefault("steps", []).append(current_step)
            step_no = int(stripped.split(".")[-1].strip())
            current_step = {"step_no": step_no, "params": {}, "columns": [], "data": []}
            columns = []  # reset for new step
            mode = "step_params"
            continue

        # Detect data header row (T,Fx,Fz,Ff,COF)
        if mode in ("step_params", "data") and not columns:
            # Check if this looks like a column header
            if re.match(r'^[Tt]\s*[,;]', stripped) or stripped.lower().startswith("t,"):
                columns = [c.strip() for c in stripped.split(",") if c.strip()]
                if current_step is not None:
                    current_step["columns"] = columns
                mode = "data"
                continue
            # Unit row: sec,N,N,N, — skip
            if stripped.lower().startswith("sec"):
                mode = "data"
                continue

        # Parse data rows
        if mode == "data" and columns:
            parts = stripped.split(",")
            if len(parts) >= len(columns):
                try:
                    row = {columns[i]: float(parts[i]) for i in range(len(columns))}
                    data_rows.append(row)
                except (ValueError, IndexError):
                    # Parameter line appearing in data section — skip
                    pass
                continue
            else:
                # Might be back in params
                pass

        # Parse parameter lines (key = value)
        if mode in ("step_params", "run_header", "metadata") and current_step is not None:
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                current_step["params"][key.strip()] = val.strip()
        elif mode in ("metadata", "run_header") and current_run is not None:
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                current_run[key.strip()] = val.strip()
            else:
                # Free-form metadata line (like script file path, date)
                current_run.setdefault("info_lines", []).append(stripped)
        elif mode == "metadata":
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                metadata[key.strip()] = val.strip()
            elif not any(stripped.startswith(kw) for kw in ("Run #", "Step No.", "T,", "sec,")):
                metadata.setdefault("info_lines", []).append(stripped)

    # Flush remaining
    if current_step is not None:
        if data_rows:
            current_step["data"] = data_rows
        if current_run is not None:
            current_run.setdefault("steps", []).append(current_step)
    if current_run is not None:
        runs.append(current_run)

    return {"metadata": metadata, "runs": runs}


def get_step_data(parsed: Dict[str, Any], run_idx: int = 0, step_no: int = 2) -> Dict[str, np.ndarray]:
    """Extract Step 2 data as numpy arrays from parsed Bruker CSV.

    Returns dict with keys: T, Fx, Fz, Ff, COF — each a 1D numpy array.
    """
    runs = parsed.get("runs", [])
    if not runs:
        raise ValueError("No runs found in parsed CSV data")
    if run_idx >= len(runs):
        raise ValueError(f"Run index {run_idx} out of range ({len(runs)} runs)")

    steps = runs[run_idx].get("steps", [])
    target = None
    for s in steps:
        if s.get("step_no") == step_no:
            target = s
            break
    if target is None:
        available = [s.get("step_no") for s in steps]
        raise ValueError(f"Step {step_no} not found. Available steps: {available}")

    data = target.get("data", [])
    if not data:
        raise ValueError(f"Step {step_no} has no data rows")

    columns = target.get("columns") or []
    if not columns:
        # infer from first data row
        if data:
            columns = list(data[0].keys())
    if not columns:
        raise ValueError(f"Step {step_no} has no column metadata and no data rows")
    arrays = {}
    for col in columns:
        arrays[col] = np.array([row.get(col, np.nan) for row in data], dtype=np.float64)
    return arrays


def detect_half_cycles(t: np.ndarray, fx: np.ndarray) -> List[Dict[str, Any]]:
    """Detect half-cycles from reciprocating friction data using Schmitt-trigger
    zero-crossing detection with hysteresis band.

    A crossing is only counted when the signal exits the hysteresis band around zero,
    suppressing noise-induced spurious crossings.
    """
    if len(fx) < 20:
        return []

    # Smooth Fx with a short moving average (~50 ms at 100 Hz)
    win = max(3, min(11, len(fx) // 500))
    kernel = np.ones(win) / win
    fx_smooth = np.convolve(fx, kernel, mode="same")

    # Hysteresis band: ±10% of the signal's RMS
    rms = np.sqrt(np.mean(fx_smooth ** 2))
    band = max(0.03, rms * 0.10)

    # State machine: -1 (below band), 0 (inside band), +1 (above band)
    cross_indices = []
    state = 1 if fx_smooth[0] > band else (-1 if fx_smooth[0] < -band else 0)
    min_gap = 8  # minimum samples between crossings (~80ms at 100Hz)

    for i in range(1, len(fx_smooth)):
        v = fx_smooth[i]
        if v > band:
            new_state = 1
        elif v < -band:
            new_state = -1
        else:
            new_state = state  # inside band, keep previous direction

        if new_state != state:
            if new_state != 0:  # crossing to an active state
                if not cross_indices or (i - cross_indices[-1]) >= min_gap:
                    cross_indices.append(i)
            state = new_state

    if len(cross_indices) < 3:
        return []

    # Build half-cycles between consecutive crossings using original (unsmoothed) data
    half_cycles = []
    for idx in range(len(cross_indices) - 1):
        si, ei = cross_indices[idx], cross_indices[idx + 1]
        seg_len = ei - si
        if seg_len < 8:  # skip very short segments
            continue
        seg = fx[si:ei]
        is_positive = np.mean(seg) > 0
        amplitude = float(np.max(np.abs(seg)))
        duration_s = float(t[ei] - t[si])
        plateau_ratio = _compute_plateau_ratio(t[si:ei], seg)
        half_cycles.append({
            "idx": len(half_cycles),
            "start_i": int(si),
            "end_i": int(ei),
            "is_positive": bool(is_positive),
            "duration_s": duration_s,
            "amplitude": amplitude,
            "plateau_ratio": plateau_ratio,
        })

    # Filter by duration and amplitude consistency
    if half_cycles:
        durations = np.array([hc["duration_s"] for hc in half_cycles])
        amps = np.array([hc["amplitude"] for hc in half_cycles])
        med_dur = np.median(durations)
        med_amp = np.median(amps)
        min_dur = max(0.12, med_dur * 0.40)
        min_amp = max(0.05, med_amp * 0.15)
        half_cycles = [
            hc for hc in half_cycles
            if hc["duration_s"] >= min_dur and hc["amplitude"] >= min_amp
        ]

    return half_cycles


def _compute_plateau_ratio(t_seg: np.ndarray, fx_seg: np.ndarray, deriv_threshold: float = 1.0) -> float:
    """Compute fraction of segment where |dFx/dt| is below threshold (steady-state plateau).

    deriv_threshold in N/s. Signal is smoothed to suppress noise before derivative.
    """
    if len(fx_seg) < 10:
        return 0.0

    # Smooth fx to suppress noise (~5 samples = 50ms at 100Hz)
    win = max(3, min(7, len(fx_seg) // 15))
    kernel = np.ones(win) / win
    fx_smooth = np.convolve(fx_seg, kernel, mode="same")

    dt = np.median(np.diff(t_seg))
    if dt <= 0:
        return 0.0
    dfx = np.diff(fx_smooth)
    deriv = np.abs(dfx / dt)
    deriv = deriv[np.isfinite(deriv)]

    if len(deriv) < 5:
        return 0.0

    plateau_fraction = float(np.mean(deriv < deriv_threshold))
    return plateau_fraction


def discriminate_pattern(parsed: Dict[str, Any], step_no: int = 2) -> Dict[str, Any]:
    """Analyze Step 2 friction data and classify the friction pattern.

    Returns dict:
        pattern: 'good' | 'triangular' | 'irregular' | 'stick_slip'
        confidence: 0-1
        details: dict with metrics
    """
    arr = get_step_data(parsed, step_no=step_no)
    t = arr["T"]
    fx = arr["Fx"]

    half_cycles = detect_half_cycles(t, fx)
    if len(half_cycles) < 4:
        return {
            "pattern": "irregular",
            "confidence": 0.5,
            "details": {"reason": f"Too few cycles detected: {len(half_cycles)}", "half_cycles": []},
        }

    # Separate positive and negative half-cycles for plateau analysis
    pos_hc = [hc for hc in half_cycles if hc["is_positive"]]
    neg_hc = [hc for hc in half_cycles if not hc["is_positive"]]

    pos_plateaus = [hc["plateau_ratio"] for hc in pos_hc] if pos_hc else [0]
    neg_plateaus = [hc["plateau_ratio"] for hc in neg_hc] if neg_hc else [0]
    mean_plateau = float(np.mean(pos_plateaus + neg_plateaus))

    # Amplitude analysis
    amplitudes = np.array([hc["amplitude"] for hc in half_cycles])
    cv_amplitude = float(np.std(amplitudes) / np.mean(amplitudes)) if np.mean(amplitudes) > 0 else 1.0

    # Trend in amplitude (linear fit slope / mean)
    x_idx = np.arange(len(amplitudes))
    slope, _ = np.polyfit(x_idx, amplitudes, 1)
    amplitude_trend = float(slope / np.mean(amplitudes)) if np.mean(amplitudes) > 0 else 0.0

    # Stick-slip detection: check for high-frequency oscillation in "plateau" regions
    stick_slip_score = _compute_stick_slip_score(t, fx, half_cycles)

    # Classification
    # Stable cycle proportion: fraction of half-cycles with plateau_ratio > 0.25
    all_plateaus = np.array(pos_plateaus + neg_plateaus)
    stable_threshold = 0.25
    n_stable = int(np.sum(all_plateaus > stable_threshold))
    stable_proportion = float(n_stable / len(all_plateaus)) if len(all_plateaus) > 0 else 0.0

    # Asymmetry: ratio of positive to negative mean amplitude
    pos_amps = [hc["amplitude"] for hc in pos_hc] if pos_hc else [0]
    neg_amps = [hc["amplitude"] for hc in neg_hc] if neg_hc else [0]
    pos_amp_mean = float(np.mean(pos_amps))
    neg_amp_mean = float(np.mean(neg_amps))
    asymmetry = float(abs(pos_amp_mean - neg_amp_mean) / max(pos_amp_mean + neg_amp_mean, 1e-6))

    details = {
        "n_half_cycles": len(half_cycles),
        "n_full_cycles": len(half_cycles) // 2,
        "n_stable_half_cycles": n_stable,
        "stable_proportion": round(stable_proportion, 4),
        "mean_plateau_ratio": round(mean_plateau, 4),
        "pos_plateau_ratio": round(float(np.mean(pos_plateaus)), 4) if pos_plateaus else 0,
        "neg_plateau_ratio": round(float(np.mean(neg_plateaus)), 4) if neg_plateaus else 0,
        "pos_amplitude": round(pos_amp_mean, 4),
        "neg_amplitude": round(neg_amp_mean, 4),
        "asymmetry": round(asymmetry, 4),
        "cv_amplitude": round(cv_amplitude, 4),
        "amplitude_trend": round(amplitude_trend, 4),
        "stick_slip_score": round(stick_slip_score, 4),
        "mean_amplitude": round(float(np.mean(amplitudes)), 4),
    }

    # Decision logic
    if asymmetry > 0.65:
        pattern = "asymmetric"
        confidence = min(1.0, asymmetry * 1.2)
    elif stable_proportion > 0.6 and cv_amplitude < 0.30 and stick_slip_score < 0.35:
        pattern = "good"
        confidence = min(1.0, stable_proportion + (1 - cv_amplitude) * 0.3)
    elif cv_amplitude > 0.50 or abs(amplitude_trend) > 0.10:
        pattern = "irregular"
        confidence = min(1.0, cv_amplitude * 0.8 + abs(amplitude_trend) * 3)
    elif stick_slip_score > 0.45:
        pattern = "stick_slip"
        confidence = min(1.0, stick_slip_score * 1.5)
    elif stable_proportion < 0.15:
        pattern = "triangular"
        confidence = min(1.0, (0.15 - stable_proportion) * 5 + (1 - cv_amplitude) * 0.3)
    else:
        pattern = "irregular"
        confidence = min(1.0, (1 - stable_proportion) * 0.8 + cv_amplitude * 0.5)

    return {
        "pattern": pattern,
        "confidence": round(confidence, 4),
        "details": details,
    }


def _compute_stick_slip_score(t: np.ndarray, fx: np.ndarray, half_cycles: List[Dict[str, Any]]) -> float:
    """Quantify stick-slip by measuring high-frequency energy in plateau-like regions."""
    if len(half_cycles) < 4:
        return 0.0
    # Focus on half-cycles with decent plateau ratio (candidate plateaus)
    plateau_candidates = [hc for hc in half_cycles if hc["plateau_ratio"] > 0.1]
    if not plateau_candidates:
        return 0.0

    scores = []
    dt = np.median(np.diff(t))
    sample_rate = 1.0 / dt if dt > 0 else 100.0

    for hc in plateau_candidates:
        seg = fx[hc["start_i"]:hc["end_i"]]
        if len(seg) < 20:
            continue
        # Detrend
        seg_detrend = seg - np.mean(seg)
        # Rough high-frequency energy: std of residual after smoothing
        win = max(5, int(sample_rate * 0.05))  # 50ms window
        if win >= len(seg) // 3:
            continue
        kernel = np.ones(win) / win
        smoothed = np.convolve(seg_detrend, kernel, mode="same")
        residual = seg_detrend - smoothed
        # Ratio of residual energy to total energy
        total_var = np.var(seg_detrend)
        if total_var > 1e-12:
            hf_ratio = float(np.var(residual) / total_var)
            scores.append(hf_ratio)

    return float(np.mean(scores)) if scores else 0.0


def parse_compression_csv(csv_path: Path) -> Dict[str, Any]:
    """Parse a compression-test CSV file.

    The file begins with metadata lines (key,value), then a header row, then data.
    Returns dict with metadata and data arrays.
    """
    text = csv_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    metadata: Dict[str, str] = {}
    header_line: str = ""
    data_lines: List[str] = []
    in_data = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(",", 1)
        if not in_data:
            # Detect data start: line with 7 comma-separated numeric-like values
            num_parts = stripped.split(",")
            if len(num_parts) >= 5:
                # Try to parse the first column as a float — if yes, this is data
                try:
                    float(num_parts[0])
                    in_data = True
                    data_lines.append(stripped)
                    continue
                except ValueError:
                    pass

            # If two parts and looks like key,value metadata
            if len(parts) == 2 and not any(c in parts[0] for c in ("mm/mm", "MPa", "mm)", "N)", "s)", "mm/s")):
                metadata[parts[0].strip()] = parts[1].strip()
            else:
                # This is the column header
                header_line = stripped
        else:
            data_lines.append(stripped)

    # Extract column names from header
    col_names = [c.strip() for c in header_line.split(",")]
    if not col_names:
        # Fallback: guess column order
        col_names = ["strain", "stress_MPa", "displacement_mm", "force_N", "force_Y_N", "time_s", "speed_mm_s"]

    # Parse data
    n_cols = len(col_names)
    records: List[Dict[str, float]] = []
    for dl in data_lines:
        parts = dl.split(",")
        if len(parts) < n_cols:
            continue
        try:
            row = {col_names[i]: float(parts[i]) for i in range(n_cols)}
            records.append(row)
        except (ValueError, IndexError):
            continue

    # Build numpy arrays for key columns
    strain_col = next((c for c in col_names if "mm/mm" in c), col_names[0])
    stress_col = next((c for c in col_names if "MPa" in c), col_names[1])
    displacement_col = next((c for c in col_names if "mm)" in c), col_names[2])
    force_col = next((c for c in col_names if "N)" in c or "N," in c), col_names[3])
    time_col = next((c for c in col_names if "s)" in c), col_names[5])

    strain = np.array([r.get(strain_col, np.nan) for r in records], dtype=np.float64)
    stress = np.array([r.get(stress_col, np.nan) for r in records], dtype=np.float64)
    displacement = np.array([r.get(displacement_col, np.nan) for r in records], dtype=np.float64)
    force = np.array([r.get(force_col, np.nan) for r in records], dtype=np.float64)
    time = np.array([r.get(time_col, np.nan) for r in records], dtype=np.float64)

    return {
        "metadata": metadata,
        "sample_name": metadata.get("SampleName", csv_path.stem),
        "thickness": float(metadata.get("Thickness", 0)),
        "width": float(metadata.get("Width", 0)),
        "strain": strain,
        "stress": stress,
        "displacement": displacement,
        "force": force,
        "time": time,
    }


def compute_compression_modulus(
    parsed: Dict[str, Any],
    strain_range: Tuple[float, float] | None = None,
) -> Dict[str, Any]:
    """Compute compression modulus (MPa) from the linear region of stress-strain data.

    Args:
        parsed: output of parse_compression_csv
        strain_range: optional (min_strain, max_strain) for the linear region.
                      If None, auto-detect using R² optimization.

    Returns dict with modulus_MPa, r_squared, strain_range_used, n_points.
    """
    strain = parsed["strain"]
    stress = parsed["stress"]

    # Remove NaN and filter to positive strain (loading phase)
    valid = np.isfinite(strain) & np.isfinite(stress) & (strain >= 0)
    strain_v = strain[valid]
    stress_v = stress[valid]

    if len(strain_v) < 10:
        return {"modulus_MPa": None, "error": "Insufficient valid data points"}

    if strain_range is not None:
        lo, hi = strain_range
        mask = (strain_v >= lo) & (strain_v <= hi)
        strain_seg = strain_v[mask]
        stress_seg = stress_v[mask]
        if len(strain_seg) < 5:
            return {"modulus_MPa": None, "error": f"Insufficient points in strain range [{lo}, {hi}]"}
        slope, intercept = np.polyfit(strain_seg, stress_seg, 1)
        predicted = slope * strain_seg + intercept
        ss_res = np.sum((stress_seg - predicted) ** 2)
        ss_tot = np.sum((stress_seg - np.mean(stress_seg)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return {
            "modulus_MPa": round(float(slope), 4),
            "r_squared": round(float(r2), 4),
            "strain_range_used": [lo, hi],
            "n_points": int(len(strain_seg)),
        }

    # Auto-detect: forward search anchored at the start of the loading curve.
    # The compression modulus must come from the *initial* linear-elastic
    # region. We start from near-zero strain and expand forward, stopping
    # when the running slope deviates or R² breaks. This replaces the old
    # full-curve sliding-window scan, which could pick regions far from the
    # elastic onset (e.g. post-densification) and misrepresent the modulus.

    min_strain_window = 0.015   # seed window must cover at least 1.5% strain
    min_pts = 30                # … and at least this many points
    max_strain_frac = 0.15      # never look beyond 15% strain for hydrogels
    r2_threshold = 0.90         # stop when R² falls below this
    slope_dev_threshold = 0.35  # stop when running slope deviates > ±35% from baseline
    smooth_halfwin = 5          # half-window for running-slope smoothing

    n_total = len(strain_v)
    max_strain_limit = float(strain_v[-1]) * max_strain_frac
    max_idx = int(np.searchsorted(strain_v, max_strain_limit))
    max_idx = max(max_idx, min_pts)
    max_idx = min(max_idx, n_total)

    # --- determine seed window size ---
    seed_end = max(min_pts, int(np.searchsorted(strain_v, min_strain_window)))
    seed_end = min(seed_end, max_idx)

    # --- compute smoothed running slope over the seed window ---
    # Use central differences smoothed by a short moving average to get a
    # robust baseline slope that isn't thrown off by point-to-point noise.
    def _running_slopes(x, y, halfwin):
        """Return array of local-slope estimates (same length as x)."""
        n = len(x)
        slopes = np.full(n, np.nan)
        for i in range(halfwin, n - halfwin):
            sx = x[i - halfwin:i + halfwin + 1]
            sy = y[i - halfwin:i + halfwin + 1]
            slopes[i] = np.polyfit(sx, sy, 1)[0]
        return slopes

    rslopes = _running_slopes(strain_v[:seed_end], stress_v[:seed_end], smooth_halfwin)
    valid_slopes = rslopes[np.isfinite(rslopes)]
    if len(valid_slopes) < 3:
        # fallback: direct fit on seed window
        baseline_slope = np.polyfit(strain_v[:seed_end], stress_v[:seed_end], 1)[0]
    else:
        baseline_slope = float(np.median(valid_slopes))

    if baseline_slope <= 0:
        return {"modulus_MPa": None, "error": "No positive-slope region found near origin"}

    # --- expand forward, refit cumulatively, stop when linearity breaks ---
    best_end = seed_end
    for i in range(seed_end + 1, max_idx + 1):
        seg_s = stress_v[:i]
        seg_e = strain_v[:i]
        slope, intercept = np.polyfit(seg_e, seg_s, 1)
        pred = slope * seg_e + intercept
        ss_res = np.sum((seg_s - pred) ** 2)
        ss_tot = np.sum((seg_s - np.mean(seg_s)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        slope_dev = abs(slope - baseline_slope) / abs(baseline_slope)
        if r2 < r2_threshold or slope_dev > slope_dev_threshold:
            break
        best_end = i

    # --- final fit on detected linear-elastic region ---
    seg_s = stress_v[:best_end]
    seg_e = strain_v[:best_end]
    slope, intercept = np.polyfit(seg_e, seg_s, 1)
    pred = slope * seg_e + intercept
    ss_res = np.sum((seg_s - pred) ** 2)
    ss_tot = np.sum((seg_s - np.mean(seg_s)) ** 2)
    final_r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    best_range = (float(strain_v[0]), float(strain_v[best_end - 1]))

    return {
        "modulus_MPa": round(float(slope), 4),
        "r_squared": round(float(final_r2), 4),
        "strain_range_used": list(best_range),
        "n_points": best_end,
    }


def extract_cof_stats_from_bruker(csv_path: Path, step_no: int = 2) -> Dict[str, Any]:
    """Extract COF statistics and wear proxy from a Bruker UMT CSV (Step 2 data).

    Returns dict with:
        cof_mean, cof_std, wear_proxy (frictional energy in mJ), n_points, ...
    """
    parsed = parse_bruker_csv(csv_path)
    arr = get_step_data(parsed, step_no=step_no)
    cof = arr.get("COF")
    fx = arr.get("Fx")
    if cof is None or fx is None:
        raise ValueError(f"Missing COF/Fx column in step {step_no} of {csv_path}")
    t = arr["T"]

    # Get velocity from step params
    velocity = None
    for run in parsed.get("runs", []):
        for step in run.get("steps", []):
            if step.get("step_no") == step_no:
                v_str = step.get("params", {}).get("Velocity", "")
                try:
                    velocity = float(v_str)
                except (ValueError, TypeError):
                    velocity = None
                break

    # Exclude first 5% and last 2% of data (settling / ramp-out)
    n = len(cof)
    start_i = int(n * 0.05)
    end_i = int(n * 0.98)
    cof_trim = cof[start_i:end_i]
    fx_trim = np.abs(fx[start_i:end_i])
    t_trim = t[start_i:end_i]

    cof_mean = float(np.mean(cof_trim))
    cof_std = float(np.std(cof_trim))

    # Wear proxy: total frictional energy dissipated (mJ = N·mm)
    # E = Σ|Fx| * v * Δt = v * Δt * Σ|Fx|
    dt = float(np.median(np.diff(t_trim)))
    if velocity is not None and velocity > 0:
        wear_proxy = float(np.sum(fx_trim) * velocity * dt * 1e-3)  # convert to J or keep as mJ
    else:
        # Fallback: cumulative absolute friction work (N·s), dimensionless-ish
        wear_proxy = float(np.sum(fx_trim) * dt)

    return {
        "cof_mean": round(cof_mean, 6),
        "cof_std": round(cof_std, 6),
        "wear_proxy": round(wear_proxy, 6),
        "velocity": velocity,
        "n_points": int(len(cof_trim)),
        "T_start": round(float(t_trim[0]), 3),
        "T_end": round(float(t_trim[-1]), 3),
        "duration_s": round(float(t_trim[-1] - t_trim[0]), 3),
    }


def build_results_from_bruker_csvs(
    out_dir: Path,
    round_idx: int,
    candidate_csv_map: Dict[str, List[Path]],
    compression_map: Dict[str, Path] | None = None,
) -> Path:
    """Build a results_filled.csv from multiple Bruker CSV files.

    Args:
        out_dir: output directory
        round_idx: round number (e.g., 2 for R2)
        candidate_csv_map: {candidate_id: [path_to_repeat1.csv, path_to_repeat2.csv, ...]}
        compression_map: optional {candidate_id: path_to_compression.csv} for modulus

    Returns:
        Path to the generated results_filled.csv
    """
    compression_map = compression_map or {}
    rows = []
    for cid, csv_paths in candidate_csv_map.items():
        row: Dict[str, str] = {"candidate_id": cid}
        cof_means = []

        for i, csv_path in enumerate(csv_paths, start=1):
            if not csv_path.exists():
                print(f"[WARN] {csv_path} not found, skipping repeat {i} for {cid}")
                continue
            stats = extract_cof_stats_from_bruker(csv_path)
            row[f"COF_mean_{i}"] = str(stats["cof_mean"])
            row[f"COF_std_{i}"] = str(stats["cof_std"])
            cof_means.append(stats["cof_mean"])
            # Use first repeat's wear proxy as the overall value
            if i == 1 and "wear_proxy" in stats:
                row["wear_proxy"] = str(stats["wear_proxy"])

        # Fill remaining repeat slots with empty if fewer than 3
        for j in range(i + 1, 4):
            row.setdefault(f"COF_mean_{j}", "")
            row.setdefault(f"COF_std_{j}", "")

        # Compute aggregated COF
        if cof_means:
            overall_mean = float(np.mean(cof_means))
            # Aggregate std: between-group + within-group
            overall_std = 0.0
            if len(cof_means) > 1:
                between_var = float(np.var(cof_means))
                within_vars = []
                for i2 in range(1, len(csv_paths) + 1):
                    s = row.get(f"COF_std_{i2}", "")
                    if s:
                        within_vars.append(float(s) ** 2)
                if within_vars:
                    overall_std = float(np.sqrt(between_var + np.mean(within_vars)))
                else:
                    overall_std = float(np.sqrt(between_var))
            else:
                s = row.get("COF_std_1", "")
                overall_std = float(s) if s else 0.0

            row["cof_steady_mean"] = str(round(overall_mean, 6))
            row["cof_std"] = str(round(overall_std, 6))

        # Compression modulus
        comp_path = compression_map.get(cid)
        if comp_path and comp_path.exists():
            comp = parse_compression_csv(comp_path)
            mod_result = compute_compression_modulus(comp)
            if mod_result.get("modulus_MPa") is not None:
                row["compression_modulus_MPa"] = str(mod_result["modulus_MPa"])
                print(f"[INFO] {cid}: modulus = {mod_result['modulus_MPa']} MPa (R2={mod_result['r_squared']}, strain {mod_result['strain_range_used'][0]:.3f}-{mod_result['strain_range_used'][1]:.3f})")
            else:
                print(f"[WARN] {cid}: could not compute modulus from {comp_path.name}: {mod_result.get('error')}")
        else:
            row.setdefault("compression_modulus_MPa", "")

        # Fields that still need manual input
        row.setdefault("wear_proxy", "")
        row.setdefault("failure_type", "")
        row.setdefault("failure_time_min", "")
        row.setdefault("notes", "auto-filled from Bruker CSV")

        # Infer failure_type from friction pattern analysis on first repeat
        if not row.get("failure_type") and csv_paths:
            try:
                parsed_friction = parse_bruker_csv(csv_paths[0])
                pat_result = discriminate_pattern(parsed_friction)
                pattern = pat_result["pattern"]
                hint_map = {
                    "good": "none",
                    "triangular": "stick_slip",
                    "stick_slip": "stick_slip",
                    "asymmetric": "misalignment_or_directional",
                    "irregular": "delamination_or_debris",
                }
                suggested = hint_map.get(pattern, "")
                row["notes"] = f"friction_pattern={pattern}(conf={pat_result['confidence']:.2f}); suggested_failure_type={suggested}"
                # Inject structured friction metrics into the row for diagnosis LLM
                d = pat_result.get("details", {})
                row["plateau_ratio"] = str(d.get("mean_plateau_ratio", ""))
                row["pos_plateau_ratio"] = str(d.get("pos_plateau_ratio", ""))
                row["neg_plateau_ratio"] = str(d.get("neg_plateau_ratio", ""))
                row["asymmetry"] = str(d.get("asymmetry", ""))
                row["cv_amplitude"] = str(d.get("cv_amplitude", ""))
                row["stick_slip_score"] = str(d.get("stick_slip_score", ""))
                row["stable_proportion"] = str(d.get("stable_proportion", ""))
                row["friction_pattern"] = pattern
                print(f"[INFO] {cid}: friction={pattern}, suggested failure_type={suggested} (needs manual confirmation)")

                # Generate friction pattern plot for the first repeat
                plot_path = out_dir / f"R{round_idx}_{cid}_friction.png"
                try:
                    saved = plot_fx_vs_t(
                        parsed_friction,
                        title=f"R{round_idx} {cid} — {csv_paths[0].name}",
                        save_path=plot_path,
                    )
                    print(f"[INFO] {cid}: plot saved -> {Path(saved).name}")
                except (OSError, ValueError, ImportError) as e:
                    print(f"[WARN] {cid}: plot failed: {e}")
            except (OSError, ValueError) as e:
                print(f"[WARN] {cid}: could not analyze friction pattern: {e}")

        # Also extract wear_proxy from the first repeat if not set by repeat loop
        if not row.get("wear_proxy") and csv_paths:
            first_stats = extract_cof_stats_from_bruker(csv_paths[0])
            if "wear_proxy" in first_stats:
                row["wear_proxy"] = str(first_stats["wear_proxy"])

        rows.append(row)

    # Write CSV
    fields = [
        "candidate_id",
        "cof_steady_mean", "cof_std",
        "COF_mean_1", "COF_std_1",
        "COF_mean_2", "COF_std_2",
        "COF_mean_3", "COF_std_3",
        "wear_proxy", "compression_modulus_MPa",
        "failure_type", "failure_time_min",
        "plateau_ratio", "pos_plateau_ratio", "neg_plateau_ratio",
        "asymmetry", "cv_amplitude", "stick_slip_score",
        "stable_proportion", "friction_pattern",
        "notes",
    ]
    out_path = out_dir / f"R{round_idx}_results_filled.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"[OK] Built results from Bruker CSVs: {out_path} ({len(rows)} candidates)")
    return out_path


def plot_fx_vs_t(
    parsed: Dict[str, Any],
    step_no: int = 2,
    title: str = "",
    save_path: Path | None = None,
    show: bool = False,
) -> str | None:
    """Plot Fx vs T for a given step. Saves to save_path if given, or returns base64 PNG.

    The plot includes:
    - Full Fx vs T trace
    - Highlighted cycle boundaries (zero-crossings)
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    arr = get_step_data(parsed, step_no=step_no)
    t = arr["T"]
    fx = arr["Fx"]
    fz = arr.get("Fz")
    cof = arr.get("COF")

    half_cycles = detect_half_cycles(t, fx)
    result = discriminate_pattern(parsed, step_no=step_no)

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.subplots_adjust(hspace=0.08)

    # --- Axes 1: Fx vs T with cycle detection ---
    ax1 = axes[0]
    ax1.plot(t, fx, linewidth=0.3, color="#1f77b4", alpha=0.8)
    ax1.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
    # Mark cycle boundaries
    for hc in half_cycles:
        if hc["idx"] % 2 == 0:
            ax1.axvline(x=t[hc["start_i"]], color="green", linewidth=0.4, alpha=0.3)
    ax1.set_ylabel("Fx (N)")
    ax1.grid(True, alpha=0.3)

    # Legend for pattern classification
    pattern_label = result["pattern"].upper()
    colors = {"good": "#2ca02c", "triangular": "#ff7f0e", "irregular": "#d62728", "stick_slip": "#9467bd", "asymmetric": "#8b008b"}
    color = colors.get(result["pattern"], "#333333")
    ax1.text(
        0.02, 0.95, f"Pattern: {pattern_label} (conf={result['confidence']:.2f})",
        transform=ax1.transAxes, fontsize=11, fontweight="bold", color=color,
        verticalalignment="top", bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85),
    )

    if not title:
        title = "Friction Force vs Time (Step 2)"

    d = result["details"]
    if "reason" in d:
        # Early return case: too few cycles
        detail_lines = [f"Warning: {d.get('reason', '')}"]
    else:
        detail_lines = [
            f"Cycles: {d.get('n_full_cycles', 0)} | "
            f"Plateau ratio: {d.get('mean_plateau_ratio', 0):.2f} "
            f"(pos={d.get('pos_plateau_ratio', 0):.2f}, neg={d.get('neg_plateau_ratio', 0):.2f})",
            f"CV amplitude: {d.get('cv_amplitude', 0):.2f} | "
            f"Trend: {d.get('amplitude_trend', 0):.3f} | "
            f"Stick-slip: {d.get('stick_slip_score', 0):.2f}",
        ]
    ax1.set_title(title + "\n" + "\n".join(detail_lines), fontsize=9, loc="left")

    # --- Axes 2: Zoom view (~2 seconds) ---
    ax2 = axes[1]
    zoom_start = min(5.0, t[-1] * 0.05)  # start at 5s or 5% of total
    zoom_mask = (t >= zoom_start) & (t < zoom_start + 2.0)
    ax2.plot(t[zoom_mask], fx[zoom_mask], linewidth=0.8, color="#1f77b4")
    ax2.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
    # Mark zero-crossings in zoom region
    for hc in half_cycles:
        ti = t[hc["start_i"]]
        if zoom_start <= ti <= zoom_start + 2.0:
            ax2.axvline(x=ti, color="green", linewidth=0.5, alpha=0.5)
    ax2.set_ylabel("Fx (N)")
    ax2.set_title(f"Zoom: {zoom_start:.0f}s – {zoom_start + 2:.0f}s", fontsize=9, loc="right")
    ax2.grid(True, alpha=0.3)

    # --- Axes 3: Fz (normal force) and COF ---
    ax3 = axes[2]
    if fz is not None:
        ax3.plot(t, fz, linewidth=0.3, color="#2ca02c", alpha=0.6, label="Fz (N)")
    if cof is not None:
        ax3_rhs = ax3.twinx()
        ax3_rhs.plot(t, cof, linewidth=0.3, color="#d62728", alpha=0.6, label="COF")
        ax3_rhs.set_ylabel("COF", color="#d62728")
        ax3_rhs.tick_params(axis="y", labelcolor="#d62728")
    ax3.set_xlabel("Time (s)")
    ax3.set_ylabel("Fz (N)", color="#2ca02c")
    ax3.tick_params(axis="y", labelcolor="#2ca02c")
    ax3.grid(True, alpha=0.3)

    if save_path:
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(save_path), dpi=150, bbox_inches="tight")
        plt.close(fig)
        return str(save_path)
    else:
        import io
        import base64
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
