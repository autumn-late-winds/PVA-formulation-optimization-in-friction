import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pva_work_flow.core.config import ACCEPTANCE

# -------------------- I/O artifacts --------------------
def export_doe_csv(path: Path, selected_ids: List[str], candidates: List[Dict[str, Any]]):
    if not selected_ids:
        # 没有候选被选中，给出明确错误信息
        raise RuntimeError("No candidates selected in audit; DOE CSV cannot be generated.")
    
    by_id = {c["candidate_id"]: c for c in candidates}
    rows = []
    for cid in selected_ids:
        c = by_id[cid]
        f = c["formulation"]
        p = c["processing"]
        adds = f.get("additives", []) or []
        add_desc = ";".join([
            f"{a.get('name')}:{a.get('wt_percent', a.get('wt_percent_range'))}"
            for a in adds
        ])
        rows.append({
            "candidate_id": cid,
            "pva_wt_percent": f.get("pva_wt_percent"),
            "freeze_thaw_cycles": p.get("freeze_thaw_cycles"),
            "freeze_temp_C": p.get("freeze_temp_C"),
            "thaw_temp_C": p.get("thaw_temp_C"),
            "cycle_hours": p.get("cycle_hours"),
            "post_soak_hours": p.get("post_soak_hours"),
            "additives": add_desc,
            "design_role": c.get("design_role", ""),
            "recommended_repeats": c.get("recommended_repeats", ""),
            "optimization_phase": c.get("optimization_phase", ""),
            "ratio_source": c.get("ratio_source", ""),
            "ratio_rationale": " | ".join(c.get("ratio_rationale", [])[:3]),
        })
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_results_template(path: Path, selected_ids: List[str]):
    fields = [
        "candidate_id",
        "cof_steady_mean",
        "cof_std",
        "COF_mean_1",
        "COF_std_1",
        "COF_mean_2",
        "COF_std_2",
        "COF_mean_3",
        "COF_std_3",
        "wear_proxy",
        "compression_modulus_MPa",
        "failure_type",
        "failure_time_min",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for cid in selected_ids:
            writer.writerow({"candidate_id": cid})


def read_results_filled(path: Path) -> List[Dict[str, str]]:
    rows = []
    with path.open("r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("candidate_id"):
                rows.append(r)
    return rows


from pva_work_flow.core.utils import _to_float_or_none


def aggregate_cof_from_row(r: Dict[str, str]) -> Tuple[float | None, float | None]:
    """
    优先使用 COF_mean_1~3 / COF_std_1~3 聚合；
    如果这些列不存在或全空，则回退到 cof_steady_mean / cof_std。

    聚合规则：
    - 总 mean = 所有有效 COF_mean_i 的平均值
    - 总 std  = sqrt( mean(std_i^2 + (mean_i - overall_mean)^2) )
      即把每组内部波动和组间波动都考虑进去
    """
    mean_keys = ["COF_mean_1", "COF_mean_2", "COF_mean_3"]
    std_keys  = ["COF_std_1",  "COF_std_2",  "COF_std_3"]

    means = []
    stds = []

    for mk, sk in zip(mean_keys, std_keys):
        m = _to_float_or_none(r.get(mk))
        s = _to_float_or_none(r.get(sk))
        if m is not None:
            means.append(m)
            stds.append(0.0 if s is None else s)

    if means:
        overall_mean = sum(means) / len(means)
        overall_var = sum((s ** 2) + ((m - overall_mean) ** 2) for m, s in zip(means, stds)) / len(means)
        overall_std = math.sqrt(max(overall_var, 0.0))
        return overall_mean, overall_std

    # fallback：没有重复列时，退回原来的单列
    mean_single = _to_float_or_none(r.get("cof_steady_mean"))
    std_single = _to_float_or_none(r.get("cof_std"))
    return mean_single, std_single


def compute_kpis(
    results_rows: List[Dict[str, str]],
    error_codes_by_id: Dict[str, List[str]] | None = None,
) -> Dict[str, Any]:
    """Compute round-level KPI trends with explicit ranking semantics.

    ``best_valid_cof`` is the lowest COF among candidates without wet-lab
    failure signals. ``best_cvs`` is the highest composite viability score and
    should be used for parent/formula ranking because it includes mechanical
    integrity, COF, wear/modulus, and friction stability.

    ``best_cof`` is kept as a backwards-compatible alias for
    ``best_valid_cof``; new code should prefer the explicit field names.
    """
    from pva_work_flow.wetlab.wetlab_outcomes import compute_cvs, has_failure

    best_valid_cof = None
    best_valid_cof_candidate = None
    best_cvs = None
    best_cvs_candidate = None
    best_cvs_grade = "F"
    ok_count = 0
    n = 0
    measured_valid_count = 0
    failed_count = 0
    error_codes_by_id = error_codes_by_id or {}

    for r in results_rows:
        failure = (r.get("failure_type") or "none").strip().lower()
        cid = (r.get("candidate_id") or "").strip()
        explicit_error_codes = error_codes_by_id.get(cid, [])
        failed = bool(explicit_error_codes) or has_failure(r)
        if failed:
            failed_count += 1

        cof, std = aggregate_cof_from_row(r)
        if cof is None and not failed:
            continue
        if std is None:
            std = 0.0

        n += 1
        cvs_result = compute_cvs(r, error_codes=explicit_error_codes or None)
        cvs = float(cvs_result.get("cvs") or 0.0)
        if best_cvs is None or cvs > best_cvs:
            best_cvs = cvs
            best_cvs_candidate = cid or None
            best_cvs_grade = str(cvs_result.get("grade") or "F")

        if failed or cof is None:
            continue

        measured_valid_count += 1
        if best_valid_cof is None or cof < best_valid_cof:
            best_valid_cof = cof
            best_valid_cof_candidate = cid or None

        pass_one = (cof <= ACCEPTANCE["cof_steady_max"]) and (std <= ACCEPTANCE["cof_std_max"])
        if ACCEPTANCE["no_failure"] and failure not in ("none", "", "na"):
            pass_one = False
        if pass_one:
            ok_count += 1

    return {
        "n": n,
        "measured_valid_count": measured_valid_count,
        "failed_count": failed_count,
        "best_valid_cof": best_valid_cof,
        "best_valid_cof_candidate": best_valid_cof_candidate,
        "best_cvs": best_cvs,
        "best_cvs_candidate": best_cvs_candidate,
        "best_cvs_grade": best_cvs_grade,
        "ranking_metric": "cvs_with_integrity_gate",
        "best_cof": best_valid_cof,
        "best_candidate_id": best_cvs_candidate,
        "pass_rate": (ok_count / n) if n else 0.0,
        "failure_rate": (failed_count / n) if n else 0.0,
    }
