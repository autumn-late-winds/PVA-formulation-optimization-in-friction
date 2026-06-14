from pathlib import Path
from typing import Any, Dict, List, Tuple
import csv
import random

# -------------------- Optional simulation (to validate workflow) --------------------
def simulate_results(path: Path, doe_ids: List[str], candidates: List[Dict[str, Any]], seed: int = 7):
    rng = random.Random(seed)
    by_id = {c["candidate_id"]: c for c in candidates}
    rows = []

    for cid in doe_ids:
        c = by_id[cid]
        pva = float(c["formulation"]["pva_wt_percent"])
        cycles = int(c["processing"]["freeze_thaw_cycles"])

        # crude heuristic: more cycles and moderate PVA tend to reduce COF and wear
        base = 0.085
        cof = base - 0.004 * (cycles - 2) - 0.0015 * max(0, (14 - abs(pva - 14)))
        cof += rng.uniform(-0.004, 0.004)
        cof = max(0.02, min(0.12, cof))

        std = abs(rng.gauss(0.008, 0.004))
        wear = max(0.0, rng.gauss(1.0 - 0.12 * cycles, 0.2))

        failure_type = "none"
        if cof > 0.10 or wear > 1.3:
            failure_type = rng.choice(["debris", "delamination", "stick_slip"])
            
        # 简单示意：压缩模量随 cycles 增大而增大
        modulus = max(0.1, 0.5 + 0.3 * cycles + rng.uniform(-0.1, 0.1))

        rows.append({
            "candidate_id": cid,
            "cof_steady_mean": f"{cof:.4f}",
            "cof_std": f"{std:.4f}",
            "wear_proxy": f"{wear:.3f}",
            "compression_modulus_MPa": f"{modulus:.2f}",  # 新增
            "failure_type": failure_type,
            "failure_time_min": "",
            "notes": "simulated",
        })

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)