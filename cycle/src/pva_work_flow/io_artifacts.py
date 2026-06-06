import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import ACCEPTANCE

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


from .utils import _to_float_or_none


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


def compute_kpis(results_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    best = None
    ok_count = 0
    n = 0

    for r in results_rows:
        notes = (r.get("notes") or "").strip().upper()
        failure = (r.get("failure_type") or "none").strip().lower()

        cof_raw = (r.get("cof_steady_mean") or "").strip()
        is_error1 = ("ERROR1" in notes) or (cof_raw.upper() == "ERROR1")
        is_error2 = ("ERROR2" in notes) or (cof_raw.upper() == "ERROR2")

        if is_error1 or is_error2:
            n += 1
            continue

        cof, std = aggregate_cof_from_row(r)
        if cof is None:
            continue
        if std is None:
            std = 0.0

        n += 1
        best = cof if best is None else min(best, cof)

        pass_one = (cof <= ACCEPTANCE["cof_steady_max"]) and (std <= ACCEPTANCE["cof_std_max"])
        if ACCEPTANCE["no_failure"] and failure not in ("none", "", "na"):
            pass_one = False
        if pass_one:
            ok_count += 1

    return {
        "n": n,
        "best_cof": best,
        "pass_rate": (ok_count / n) if n else 0.0,
    }
