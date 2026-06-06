import yaml
from pathlib import Path

_prompts_path = Path(__file__).parent / "prompts" / "prompts_en.yaml"
try:
    _prompts = yaml.safe_load(_prompts_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    raise FileNotFoundError(
        f"Prompts file not found: {_prompts_path}. "
        f"Ensure prompts_en.yaml is present alongside workflow.py."
    )
except yaml.YAMLError as e:
    raise ValueError(f"Failed to parse prompts YAML: {_prompts_path}") from e

SYSTEM_PROMPT = _prompts["SYSTEM_PROMPT"]
GEN_PROMPT_R1 = _prompts["GEN_PROMPT_R1"]
GEN_PROMPT_RN = _prompts["GEN_PROMPT_RN"]
AUD_PROMPT = _prompts["AUD_PROMPT"]
DIA_PROMPT = _prompts["DIA_PROMPT"]

from typing import Any, Dict, List, Tuple
import json
from .llm_engines import LLM
from .utils import read_json, write_json, _to_float_or_none, safe_json_loads
from .io_artifacts import export_doe_csv, export_results_template, read_results_filled, compute_kpis, aggregate_cof_from_row
from .config import CONSTRAINTS, GenerationMode, CandidateDict, CONVERGENCE as _DEFAULT_CONVERGENCE
from .experiment_notes import build_notes_context_for_diagnosis
from .formulation_checks import (
    _text_blob_for_mechanism,
    compute_prep_time_hours,
    check_material_completeness,
    normalize_materials_and_formulation,
    candidate_material_names as _candidate_material_names_raw,
)


# _text_blob_for_mechanism — imported from formulation_checks, re-exported for backward compatibility


# compute_prep_time_hours — imported from formulation_checks, re-exported for backward compatibility

# check_material_completeness — imported from formulation_checks, re-exported for backward compatibility

# normalize_materials_and_formulation — imported from formulation_checks, re-exported for backward compatibility
# normalize_materials_and_formulation — imported from formulation_checks, re-exported for backward compatibility

# -------------------- Role runners --------------------


# _candidate_material_names — re-exported from formulation_checks for backward compatibility
_candidate_material_names = _candidate_material_names_raw


def run_prepare_wetlab(out_dir: Path, round_idx: int, candidates_path: Path, selected_ids: List[str]) -> Tuple[Path, Path]:
    obj = read_json(candidates_path)
    cands = obj["candidates"]
    doe_path = out_dir / f"R{round_idx}_doe.csv"
    tmpl_path = out_dir / f"R{round_idx}_results_template.csv"
    export_doe_csv(doe_path, selected_ids, cands)
    export_results_template(tmpl_path, selected_ids)
    return doe_path, tmpl_path

def run_text_only_diagnose(out_dir: Path, round_idx: int, candidates_path: Path, audits_path: Path | None = None) -> Path:
    """
    在没有实验结果的情况下，仅基于当轮 candidates（和可选的 audits），
    生成一个带 summary_for_generator 的诊断 JSON。
    用于：本轮没有任何候选通过严格审计时，仍然为下一轮提供"探索方向总结"。
    """
    cand_obj = read_json(candidates_path)
    cands = cand_obj.get("candidates", [])
    audits = []
    if audits_path is not None and audits_path.exists():
        audits = read_json(audits_path).get("audits", [])

    # 查找历史上最近一个"有真实实验结果"的轮次（例如 R1）
    last_valid_experimental_round: int | None = None
    for rr in range(round_idx - 1, 0, -1):
        rf = out_dir / f"R{rr}_results_filled.csv"
        if rf.exists():
            last_valid_experimental_round = rr
            break

    # 简单统计：PVA wt% 范围、network_type 分布、主要添加剂类别等
    pva_vals = [
        c.get("formulation", {}).get("pva_wt_percent")
        for c in cands
        if c.get("formulation", {}).get("pva_wt_percent") is not None
    ]
    network_counts: Dict[str, int] = {}
    additive_roles: Dict[str, int] = {}
    for c in cands:
        f = c.get("formulation", {}) or {}
        nt = (f.get("network_type") or "unknown").lower()
        network_counts[nt] = network_counts.get(nt, 0) + 1
        for a in f.get("additives") or []:
            role = ""
            if isinstance(a, dict):
                role = (a.get("role") or "").lower()
            else:
                role = str(a).lower()
            if role:
                additive_roles[role] = additive_roles.get(role, 0) + 1

    # 把失败原因也纳入 summary，让下一轮尽量修正这些薄弱点
    failed_reasons: Dict[str, int] = {}
    for a in audits:
        if a.get("decision") == "FAIL":
            for fr in a.get("failed_rules", []):
                failed_reasons[fr] = failed_reasons.get(fr, 0) + 1

    lines = []
    lines.append(f"Round R{round_idx} had no candidates passing the strict audit, so no DOE was run.")
    lines.append(f"A total of {len(cands)} candidates were proposed.")
    if pva_vals:
        lines.append(
            f"PVA wt% range (among candidates where defined): {min(pva_vals)}–{max(pva_vals)}."
        )
    if network_counts:
        lines.append("Network types explored (count): " + ", ".join(
            f"{k}: {v}" for k, v in sorted(network_counts.items(), key=lambda x: -x[1])
        ))
    if additive_roles:
        lines.append("Additive roles explored (count): " + ", ".join(
            f"{k}: {v}" for k, v in sorted(additive_roles.items(), key=lambda x: -x[1])
        ))
    if failed_reasons:
        lines.append(
            "Most frequent audit failures (to be fixed in the next round): "
            + "; ".join(f"{k} (n={v})" for k, v in sorted(failed_reasons.items(), key=lambda x: -x[1])[:8])
        )
    lines.append(
        "For the next round, generate only candidates with fully specified materials "
        "(including main polymer, additives, crosslinkers/initiators, nanofillers if used, "
        "with explicit wt% or mass ratios) and keep the preparation schedule explicit and intentional."
    )
    if last_valid_experimental_round is not None:
        lines.append(
            f"Use the quantitative outcomes from experimental round R{last_valid_experimental_round} "
            "together with the current round's audit failures as hard constraints for the next generation."
        )

    summary = "\n".join(lines)

    if last_valid_experimental_round is not None:
        goal_str = (
            "result_driven_with_history: refine formulations and protocols using experimental outcomes "
            f"from round R{last_valid_experimental_round} together with the current round's audit failures "
            "(no new experiments in this round)"
        )
    else:
        goal_str = (
            "fallback: refine formulations and protocols based on text-only summary "
            "(no experimental results in this round)"
        )

    inherited_failure_modes = []
    inherited_mechanisms = []
    inherited_levers = []
    if last_valid_experimental_round is not None:
        prev_diag_path = out_dir / f"R{last_valid_experimental_round}_diagnosis.json"
        if prev_diag_path.exists():
            prev_diag = read_json(prev_diag_path)
            inherited_failure_modes = prev_diag.get("dominant_failure_modes") or []
            inherited_mechanisms = prev_diag.get("inferred_mechanisms") or []
            inherited_levers = prev_diag.get("actionable_levers") or []

    diag = {
        "dominant_failure_modes": inherited_failure_modes,
        "inferred_mechanisms": inherited_mechanisms,
        "actionable_levers": inherited_levers,
        "next_round_doe": {
            "goal": goal_str,
            "factors": [
                {
                    "name": "network_type",
                    "levels": ["fast_chemical", "photocured", "room_temp_gel"],
                    "operational_definition": (
                        "choose between fast chemical crosslinking ('fast_chemical'), "
                        "photocuring ('photocured'), and room-temperature fast gelation ('room_temp_gel'), "
                        "all realizable within one-day total preparation time"
                    ),
                },
                {
                    "name": "polymer_wt_percent",
                    "levels": ["lower", "higher"],
                    "operational_definition": f"span the plausible polymer concentration window suggested by R{round_idx}"
                }
            ],
            "suggested_sample_count": 8
        },
        "summary_for_generator": summary,
        "missing_info": ["no_experimental_results_this_round"]
    }

    diag_path = out_dir / f"R{round_idx}_diagnosis.json"
    write_json(diag_path, diag)
    return diag_path

def build_candidate_repairs(results_struct: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    为每一个有实验结果的 candidate 生成一条"修复计划"节点，用于构建迭代树。

    返回列表中的每个元素结构大致为：
    {
        "candidate_id": ...,
        "performance_class": "too_brittle" | "too_soft" | "high_friction" | "good_balance" | "early_failure",
        "key_metrics": { ... },
        "repair_suggestions": [
            {
                "lever": "...",
                "direction": "increase" | "decrease" | "add" | "remove",
                "target_material": "...",          # 若适用
                "target_range": [low, high],      # 或者 None
                "justification": "..."
            },
            ...
        ]
    }
    """
    repairs: List[Dict[str, Any]] = []

    for rec in results_struct:
        cid = rec.get("candidate_id")
        metrics = rec.get("metrics") or {}
        key_form = rec.get("key_formulation") or {}

        cof_raw = metrics.get("cof_steady_mean")
        modulus_raw = metrics.get("compression_modulus_MPa")
        ft_cycles = key_form.get("freeze_thaw_cycles")
        post_soak_h = key_form.get("post_soak_hours")

        failure = (metrics.get("failure") or {}).get("type") or ""
        fail_time_raw = (metrics.get("failure") or {}).get("time_min")

        cof = _to_float_or_none(cof_raw)
        modulus = _to_float_or_none(modulus_raw)
        fail_time_val = _to_float_or_none(fail_time_raw)

        # 1) 粗略性能分类（可根据实际数据再微调阈值）
        perf_class = "good_balance"
        if failure in {"fracture", "delamination"} or (modulus is not None and modulus > 2.5):
            perf_class = "too_brittle"
        elif modulus is not None and modulus < 0.5:
            perf_class = "too_soft"
        elif cof is not None and cof > 0.03:
            perf_class = "high_friction"
        elif failure and fail_time_val is not None and fail_time_val < 10:
            perf_class = "early_failure"

        # 2) 针对不同性能类别给出简单的"修复建议模板"
        suggestions: List[Dict[str, Any]] = []

        if perf_class == "too_brittle":
            suggestions.append({
                "lever": "crosslinker_wt_percent",
                "direction": "decrease",
                "target_range": None,
                "target_material": None,
                "justification": "High modulus / fracture suggests lowering chemical crosslinker loading."
            })
            suggestions.append({
                "lever": "plasticizer_level_or_type",
                "direction": "increase_or_add",
                "target_material": "PEG 400",
                "target_range": None,
                "justification": "Plasticizer such as PEG 400 can reduce brittleness while keeping low COF."
            })
            if ft_cycles is not None and ft_cycles >= 2:
                suggestions.append({
                    "lever": "freeze_thaw_cycles",
                    "direction": "decrease",
                    "target_range": None,
                    "target_material": None,
                    "justification": "Multiple freeze–thaw cycles increase network stiffness and brittleness."
                })

        elif perf_class == "too_soft":
            suggestions.append({
                "lever": "polymer_wt_percent",
                "direction": "increase",
                "target_range": None,
                "target_material": None,
                "justification": "Low modulus suggests increasing PVA or adding a reinforcing additive."
            })
            suggestions.append({
                "lever": "crosslinker_wt_percent",
                "direction": "increase",
                "target_range": None,
                "target_material": None,
                "justification": "Moderately higher crosslink density can raise modulus if fracture is avoided."
            })

        elif perf_class == "high_friction":
            suggestions.append({
                "lever": "lubricating_additive",
                "direction": "add_or_increase",
                "target_material": "hyaluronic acid (HA)",
                "target_range": None,
                "justification": "Adding a hydrophilic lubricant such as HA can lower COF."
            })
            suggestions.append({
                "lever": "pva_wt_percent",
                "direction": "decrease",
                "target_range": None,
                "target_material": None,
                "justification": "Lower polymer wt% can reduce contact stiffness and COF if stability is maintained."
            })

        elif perf_class == "early_failure":
            suggestions.append({
                "lever": "network_type_or_crosslinking_method",
                "direction": "change",
                "target_material": None,
                "target_range": None,
                "justification": "Catastrophic early failure suggests changing network type or crosslinking strategy."
            })

        repairs.append({
            "candidate_id": cid,
            "performance_class": perf_class,
            "key_metrics": {
                "cof_steady_mean": cof,  # 或 cof_raw，看你更想保存哪种
                "compression_modulus_MPa": modulus,
                "failure_type": failure,
                "failure_time_min": fail_time_val,
            },
            "repair_suggestions": suggestions,
        })

    return repairs


# ---- Code-layer DOE factor definitions ----
# Map actionable lever keywords to stable factor definitions.
# The diagnosis LLM identifies WHAT to tune; this table defines HOW to represent it.
_LEVER_TO_DOE: dict[str, dict] = {
    "pva_wt_percent": {
        "name": "pva_wt_percent", "levels": ["8.0", "10.0", "12.0"],
        "operational_definition": "PVA weight percent in 20 g batch",
    },
    "freeze_thaw_cycles": {
        "name": "freeze_thaw_cycles", "levels": ["0", "1", "2"],
        "operational_definition": "Number of freeze-thaw cycles",
    },
    "post_soak_hours": {
        "name": "post_soak_hours", "levels": ["1", "2", "4"],
        "operational_definition": "Hours of DI water soak before testing",
    },
    "additive": {
        "name": "additive_type", "levels": [],
        "operational_definition": "Primary additive in formulation",
        "_dynamic": True,  # levels populated from parent materials
    },
    "crosslink": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "Cross-linker concentration level (low/medium/high within safe chemical range)",
    },
    "cross_link": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "Cross-linker concentration level (low/medium/high within safe chemical range)",
    },
    "cross_linking": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "Cross-linker concentration level (low/medium/high within safe chemical range)",
    },
    "cross-linking": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "Cross-linker concentration level (low/medium/high within safe chemical range)",
    },
    "plasticizer": {
        "name": "plasticizer_type", "levels": [],
        "operational_definition": "Plasticizer type in formulation",
        "_dynamic": True,
    },
}


def _build_structured_doe(
    llm_doe: dict,
    actionable_levers: list[dict],
    out_dir: Path,
    round_idx: int,
) -> dict:
    """Construct stable DOE factor definitions from diagnosis levers + parent data.

    The LLM's diagnosis identifies which levers matter.  This function translates
    those levers into stable factor names/levels that the generator and audit can
    rely on, eliminating the naming drift between rounds.
    """
    lever_texts: list[str] = []
    for al in actionable_levers:
        lever = str(al.get("lever") or "").strip().lower()
        direction = str(al.get("direction") or "").strip().lower()
        if lever:
            lever_texts.append(f"{lever}:{direction}" if direction else lever)

    factors: list[dict] = []
    seen_names: set[str] = set()

    # Collect parent-round additive/plasticizer names for dynamic factors.
    # Filter out crosslinkers, catalysts, solvents, and main polymers.
    _NON_ADDITIVE_KEYWORDS = (
        "crosslink", "catalyst", "initiator", "solvent", "main_polymer",
        "polymer", "water", "di water", "deionized",
        "glutaraldehyde", "戊二醛", "borax", "硼砂", "hcl", "盐酸",
        "mbaa", "pegda", "irgacure", "photo",
    )
    parent_additives: set[str] = set()
    parent_plasticizers: set[str] = set()
    parent_cand_path = out_dir / f"R{round_idx}_candidates.json"
    if parent_cand_path.exists():
        obj = read_json(parent_cand_path)
        for pc in obj.get("candidates", []):
            f = pc.get("formulation", {}) or {}
            for a in f.get("additives", []) or []:
                nm = str(a.get("name") or "").strip()
                role = str(a.get("role") or "").strip().lower()
                nm_lower = nm.lower()
                if not nm or nm_lower == "none":
                    continue
                # Skip non-additive materials
                if any(kw in role for kw in _NON_ADDITIVE_KEYWORDS):
                    continue
                if any(kw in nm_lower for kw in _NON_ADDITIVE_KEYWORDS):
                    continue
                if "plasticiz" in role:
                    parent_plasticizers.add(nm)
                else:
                    parent_additives.add(nm)

    # Match levers to factor definitions
    for lever_text in lever_texts:
        lever_base = lever_text.split(":")[0]
        matched = None
        for keyword, factor_def in _LEVER_TO_DOE.items():
            if keyword in lever_base:
                matched = factor_def
                break
        if matched is None:
            continue
        name = matched["name"]
        if name in seen_names:
            continue
        seen_names.add(name)
        levels = list(matched["levels"])
        # Fill dynamic levels from parent data
        if matched.get("_dynamic"):
            if "plasticiz" in name:
                dynamic_set = parent_plasticizers
            else:
                dynamic_set = parent_additives
            if dynamic_set:
                levels = sorted(dynamic_set)
            if not levels:
                levels = ["none"]
        factors.append({
            "name": name,
            "levels": levels,
            "operational_definition": matched["operational_definition"],
        })

    # If the LLM proposed factors we didn't cover, keep them (with normalized names).
    # Block LLM factors that overlap with code-generated ones (e.g. "additive_combination" vs "additive_type").
    _REDUNDANT_LLM_NAMES = {"additive_combination", "additive type", "crosslinker_concentration", "cross_linking_density", "crosslinking_density"}
    for llm_factor in (llm_doe.get("factors") or []):
        llm_name = str(llm_factor.get("name") or "").strip()
        norm_name = llm_name.lower().replace(" ", "_").replace("-", "_")
        if norm_name in seen_names or not llm_name:
            continue
        if norm_name in _REDUNDANT_LLM_NAMES and "additive_type" in seen_names:
            continue
        seen_names.add(norm_name)
        factors.append({
            "name": norm_name,
            "levels": [str(l) for l in (llm_factor.get("levels") or [])],
            "operational_definition": llm_factor.get("operational_definition", ""),
        })

    # Ensure pva_wt_percent is always present
    if "pva_wt_percent" not in seen_names and "pva_wt_percent" not in {f["name"] for f in factors}:
        factors.insert(0, {
            "name": "pva_wt_percent",
            "levels": ["8.0", "10.0", "12.0"],
            "operational_definition": "PVA weight percent in 20 g batch",
        })

    return {
        "goal": llm_doe.get("goal", "Optimize formulation based on previous round results"),
        "phase": llm_doe.get("phase", "main_plus_extension"),
        "factors": factors,
        "suggested_sample_count": llm_doe.get("suggested_sample_count", 8),
        "allow_extension": bool(llm_doe.get("allow_extension", True)),
    }


def check_convergence(
    results_rows: list[dict],
    out_dir: Path,
    round_idx: int,
    convergence: dict | None = None,
) -> dict:
    """Evaluate multi-round convergence criteria for PVA hydrogel optimization.

    Checks the current round's best candidate against configurable thresholds
    for COF, modulus, friction stability, and round-to-round trend flatness.

    Returns a dict with keys:
        converged: bool
        criteria: dict of {criterion_name: {passed: bool, value: ..., threshold: ...}}
        recommendation: str
    """
    cfg = convergence or _DEFAULT_CONVERGENCE

    # ---- Extract current-round best values ----
    best_cof = None
    best_modulus = None
    best_stable = None
    best_stick_slip = None
    for r in results_rows:
        for field, collector in (
            ("cof_steady_mean", "cof"),
            ("compression_modulus_MPa", "mod"),
            ("stable_proportion", "stab"),
            ("stick_slip_score", "ss"),
        ):
            raw = r.get(field)
            if raw is not None and str(raw).strip() not in ("", "na", "nan", "none"):
                try:
                    val = float(raw)
                except (ValueError, TypeError):
                    continue
                if collector == "cof" and (best_cof is None or val < best_cof):
                    best_cof = val
                elif collector == "mod" and (best_modulus is None or val > best_modulus):
                    best_modulus = val
                elif collector == "stab" and (best_stable is None or val > best_stable):
                    best_stable = val
                elif collector == "ss" and (best_stick_slip is None or val < best_stick_slip):
                    best_stick_slip = val

    # ---- Evaluate each criterion ----
    criteria: dict[str, dict] = {}

    # 1) COF
    cof_passed = best_cof is not None and best_cof <= cfg["cof_max"]
    criteria["cof_max"] = {
        "passed": cof_passed,
        "value": best_cof,
        "threshold": cfg["cof_max"],
        "label": f"COF <= {cfg['cof_max']}",
    }

    # 2) Modulus range
    mod_passed = (
        best_modulus is not None
        and cfg["modulus_min_mpa"] <= best_modulus <= cfg["modulus_max_mpa"]
    )
    criteria["modulus_range"] = {
        "passed": mod_passed,
        "value": best_modulus,
        "threshold": [cfg["modulus_min_mpa"], cfg["modulus_max_mpa"]],
        "label": f"modulus {cfg['modulus_min_mpa']}-{cfg['modulus_max_mpa']} MPa",
    }

    # 3) Stable proportion
    stab_passed = best_stable is not None and best_stable > cfg["stable_proportion_min"]
    criteria["stable_proportion"] = {
        "passed": stab_passed,
        "value": best_stable,
        "threshold": cfg["stable_proportion_min"],
        "label": f"stable_proportion > {cfg['stable_proportion_min']}",
    }

    # 4) Stick-slip
    ss_passed = best_stick_slip is not None and best_stick_slip < cfg["stick_slip_max"]
    criteria["stick_slip"] = {
        "passed": ss_passed,
        "value": best_stick_slip,
        "threshold": cfg["stick_slip_max"],
        "label": f"stick_slip < {cfg['stick_slip_max']}",
    }

    # 5) COF trend flatness (requires kpi_log.json)
    trend_passed = None
    trend_value = None
    kpi_path = out_dir / "kpi_log.json"
    if kpi_path.exists():
        kpi_log = read_json(kpi_path)
        cofs = []
        for entry in sorted(kpi_log, key=lambda e: e.get("round", 0)):
            rn = entry.get("round", 0)
            bc = entry.get("best_cof")
            if rn <= round_idx and bc is not None:
                cofs.append(bc)
        if len(cofs) >= 2 and cofs[-1] is not None and cofs[-2] is not None:
            trend_value = abs(cofs[-1] - cofs[-2])
            trend_passed = trend_value <= cfg["cof_trend_delta"]

            # Check consecutive flat rounds
            consecutive = 0
            for i in range(len(cofs) - 1, 0, -1):
                if cofs[i] is not None and cofs[i-1] is not None:
                    if abs(cofs[i] - cofs[i-1]) <= cfg["cof_trend_delta"]:
                        consecutive += 1
                    else:
                        break
            trend_passed = consecutive >= cfg["cof_trend_consecutive"]
            trend_value = consecutive
    criteria["cof_trend"] = {
        "passed": trend_passed,
        "value": trend_value,
        "threshold": cfg["cof_trend_consecutive"],
        "label": f"COF trend flat for >= {cfg['cof_trend_consecutive']} consecutive rounds (delta <= {cfg['cof_trend_delta']})",
    }

    # ---- Aggregate verdict ----
    all_passed = all(
        c["passed"] is not False  # None (no data) is treated as "not yet failed"
        for c in criteria.values()
    )
    # But trend_passed=None means "not enough data to judge trend" → don't block
    hard_failures = [
        name for name, c in criteria.items()
        if c["passed"] is False and name != "cof_trend"
    ]
    converged = len(hard_failures) == 0 and trend_passed is not False

    if converged and trend_passed is True:
        recommendation = (
            f"CONVERGED at R{round_idx}: all criteria met. "
            "Recommend entering robustness/repeatability validation (3-5 repeats of best formula)."
        )
    elif converged and trend_passed is None:
        recommendation = (
            f"Near-convergence at R{round_idx}: metric criteria passed but "
            "insufficient trend data. Continue one more round to confirm flat trend."
        )
    else:
        failed_labels = [criteria[n]["label"] for n in hard_failures] if hard_failures else []
        if trend_passed is False and "cof_trend" not in hard_failures:
            failed_labels.append(criteria["cof_trend"]["label"])
        recommendation = (
            f"NOT converged at R{round_idx}. "
            f"Failed criteria: {failed_labels if failed_labels else 'trend data insufficient'}. "
            "Continue iterative optimization."
        )
        if "cof_trend" in criteria and criteria["cof_trend"]["passed"] is False:
            recommendation += (
                " COF trend has flattened — may be near a local optimum. "
                "Consider switching root tree or introducing a new additive role."
            )

    return {
        "converged": converged,
        "round": round_idx,
        "criteria": criteria,
        "recommendation": recommendation,
    }


def _round_index_from_results_path(path: Path) -> int | None:
    stem = path.stem
    if not stem.startswith("R") or "_results_filled" not in stem:
        return None
    try:
        return int(stem.split("_", 1)[0][1:])
    except (IndexError, ValueError):
        return None


def load_prior_cof_by_candidate(out_dir: Path, round_idx: int) -> Dict[str, float]:
    """Load measured COF values from R1..R(round_idx-1)."""
    parent_cof: Dict[str, float] = {}
    for results_path in sorted(out_dir.glob("R*_results_filled.csv")):
        source_round = _round_index_from_results_path(results_path)
        if source_round is None or source_round >= round_idx:
            continue
        for row in read_results_filled(results_path):
            cid = (row.get("candidate_id") or "").strip()
            cof, _ = aggregate_cof_from_row(row)
            if cid and cof is not None:
                parent_cof[cid] = float(cof)
    return parent_cof


def run_diagnose(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    candidates_path: Path,
    results_filled_path: Path,
    convergence: dict | None = None,
) -> Tuple[Path, Dict[str, Any]]:
    cand_obj = read_json(candidates_path)
    results_rows = read_results_filled(results_filled_path)

    by_id = {c["candidate_id"]: c for c in cand_obj["candidates"]}
    results_struct = []
    for r in results_rows:
        cid = r.get("candidate_id")
        if not cid:
            continue
        c = by_id.get(cid)
        if not c:
            continue
        formulation = c.get("formulation") or {}
        processing = c.get("processing") or {}

        results_struct.append({
            "candidate_id": cid,
            "key_formulation": {
                "pva_wt_percent": formulation.get("pva_wt_percent"),
                "additives": formulation.get("additives", []),
                "freeze_thaw_cycles": processing.get("freeze_thaw_cycles"),
                "post_soak_hours": processing.get("post_soak_hours"),
            },
            "metrics": {
                "cof_steady_mean": r.get("cof_steady_mean"),
                "cof_std": r.get("cof_std"),
                "wear_proxy": r.get("wear_proxy"),
                "compression_modulus_MPa": r.get("compression_modulus_MPa"),
                "failure": {
                    "type": r.get("failure_type"),
                    "time_min": r.get("failure_time_min"),
                },
                "cof_repeats": {
                    "mean_1": r.get("COF_mean_1"),
                    "std_1": r.get("COF_std_1"),
                    "mean_2": r.get("COF_mean_2"),
                    "std_2": r.get("COF_std_2"),
                    "mean_3": r.get("COF_mean_3"),
                    "std_3": r.get("COF_std_3"),
                },
            },
            "friction_analysis": {
                "pattern": r.get("friction_pattern", ""),
                "plateau_ratio": r.get("plateau_ratio", ""),
                "pos_plateau": r.get("pos_plateau_ratio", ""),
                "neg_plateau": r.get("neg_plateau_ratio", ""),
                "asymmetry": r.get("asymmetry", ""),
                "cv_amplitude": r.get("cv_amplitude", ""),
                "stick_slip_score": r.get("stick_slip_score", ""),
                "stable_proportion": r.get("stable_proportion", ""),
            },
            "notes": r.get("notes", ""),
        })

    results_json = json.dumps(
        {"round_id": f"R{round_idx}", "constraints": CONSTRAINTS, "results": results_struct},
        ensure_ascii=False
    )
    
    # 为本轮所有有结果的候选生成"修复节点"，用于构建迭代树
    candidate_repairs = build_candidate_repairs(results_struct)

    # Inject experiment notes if present
    notes_ctx = build_notes_context_for_diagnosis(out_dir, round_idx)

    prompt = DIA_PROMPT.replace("{speed}", str(CONSTRAINTS["speed_mm_s"]))
    if notes_ctx:
        prompt += "\n\n" + notes_ctx
    try:
        from .formulation_rag import build_formulation_rag_context

        formulation_rag_context = build_formulation_rag_context(
            out_dir=out_dir,
            round_idx=round_idx + 1,
            phase="Diagnosis: explain failure modes and propose next-round optimization levers",
        )
        if formulation_rag_context:
            prompt += "\n\n" + formulation_rag_context
            print(f"[FORMULATION_RAG] Injected diagnosis context ({len(formulation_rag_context)} chars)")
    except Exception as e:
        print(f"[FORMULATION_RAG] diagnosis context unavailable: {e}")
    prompt += "\n\nresults_json:\n" + results_json

    raw = llm.generate(SYSTEM_PROMPT, prompt)
    try:
        diag = safe_json_loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        debug_path = out_dir / f"R{round_idx}_diag_raw.txt"
        debug_path.write_text(raw, encoding="utf-8")
        print(f"[WARN] Failed to parse diagnosis JSON; raw saved to {debug_path}: {e}")

        # 构造一个简单的兜底诊断，避免整个 workflow 崩掉
        diag = {
            "dominant_failure_modes": [],
            "inferred_mechanisms": [],
            "actionable_levers": [],
            "next_round_doe": {
                "goal": "fallback: keep exploring around best COF candidates",
                "factors": [],
                "suggested_sample_count": 8,
            },
            "summary_for_generator": (
                "Diagnosis LLM output could not be parsed as JSON. "
                "Please inspect the raw file and adjust prompts or LLM config."
            ),
            "missing_info": ["diagnosis_llm_json_parse_failed"],
        }
        
    # ---- Per-branch dCOF evaluation (tree mode) ----
    # Load all prior-round COF values so each child compares against its true
    # parent, even if that parent is older than R(n-1).
    _parent_cof: dict[str, float] = load_prior_cof_by_candidate(out_dir, round_idx)

    scored_rows: List[Tuple[str, float, float, str, float | None]] = []
    for r in results_rows:
        cid = r.get("candidate_id")
        if not cid:
            continue
        cof, std = aggregate_cof_from_row(r)
        if cof is None:
            continue
        failure_type = (r.get("failure_type") or "none").strip().lower()
        std_val = 0.0 if std is None else float(std)
        # Find parent COF for dCOF computation
        parent_cid = (by_id.get(cid, {}).get("parent_candidate_id") or "").strip()
        parent_cof = _parent_cof.get(parent_cid) if parent_cid else None
        dcof = (float(cof) - parent_cof) if parent_cof is not None else None
        scored_rows.append((cid, float(cof), std_val, failure_type, dcof))

    # In tree mode (R2+), rank by dCOF (most improvement first).
    # In R1 (no parent COF), fall back to absolute COF ranking.
    if round_idx > 1 and any(d is not None for _, _, _, _, d in scored_rows):
        # Separate into improved (dCOF < 0) and worsened (dCOF >= 0 or unknown)
        improved = [x for x in scored_rows if x[4] is not None and x[4] < 0]
        worsened = [x for x in scored_rows if x not in improved]
        improved.sort(key=lambda x: (x[4] or 0, x[2]))   # most negative dCOF first
        worsened.sort(key=lambda x: (x[1], x[2]))          # lowest absolute COF first
        scored_rows = improved + worsened
    else:
        scored_rows.sort(key=lambda x: (x[1], x[2]))       # R1: raw COF ascending

    passing_rows = [x for x in scored_rows if x[3] in ("none", "", "na")]
    if passing_rows:
        best_candidates = [cid for cid, _cof, _std, _failure, _dcof in passing_rows[:3]]
    else:
        best_candidates = [cid for cid, _cof, _std, _failure, _dcof in scored_rows[:3]]

    # Per-branch evaluation summaries for tree-mode diagnosis
    branch_evaluations: list[dict] = []
    for cid, cof, std, failure, dcof in scored_rows:
        parent_cid = (by_id.get(cid, {}).get("parent_candidate_id") or "").strip()
        if dcof is not None:
            if dcof < -0.005:
                verdict = "improved"
            elif dcof > 0.005:
                verdict = "worsened"
            else:
                verdict = "flat"
        else:
            verdict = "no_parent_data"
        branch_evaluations.append({
            "candidate_id": cid,
            "parent_candidate_id": parent_cid or None,
            "cof": round(cof, 6),
            "dcof": round(dcof, 6) if dcof is not None else None,
            "verdict": verdict,
        })

    # ---- Post-process: cross-reference experiment_notes to exclude mechanically ----
    # ---- failed candidates from best_candidates and re-mark their perf_class.  ----
    from .experiment_notes import load_notes as _load_notes, error_severity as _error_severity
    _notes = _load_notes(out_dir, round_idx)
    _failed_ids: set = set()
    if _notes:
        for _cid, _entry in _notes.items():
            if _cid.startswith("_") or not isinstance(_entry, dict):
                continue
            _errs = _entry.get("error_codes") or []
            if any(_error_severity(e) == "critical" for e in _errs):
                _failed_ids.add(_cid)

    if _failed_ids:
        # Remove mechanically failed from best_candidates
        _before = len(best_candidates)
        best_candidates = [c for c in best_candidates if c not in _failed_ids]
        if len(best_candidates) < _before:
            print(f"[DIAG] Removed {_before - len(best_candidates)} mechanically-failed candidates from best_candidates: "
                  f"{[c for c in best_candidates if c not in best_candidates] if False else set(best_candidates)}")
            # Refill from scored_rows sorted by COF, skipping failed
            _remaining = [(cid, cof, std, ft, dcof) for cid, cof, std, ft, dcof in scored_rows
                          if cid not in _failed_ids and cid not in set(best_candidates)]
            _remaining.sort(key=lambda x: (x[1], x[2]))
            for _cid, _cof, _std, _ft, _dcof in _remaining:
                if len(best_candidates) >= 3:
                    break
                best_candidates.append(_cid)

        # Re-mark candidate_repairs for mechanically failed candidates
        for _repair in candidate_repairs:
            _rcid = _repair.get("candidate_id", "")
            if _rcid in _failed_ids:
                _repair["performance_class"] = "mechanically_failed"
                _repair.setdefault("_experiment_note_warning", "COF data unreliable due to mechanical failure (rupture/no-gel)")

    # ---- Convergence check ----
    conv_result = check_convergence(results_rows, out_dir, round_idx, convergence)
    print(f"[CONVERGENCE] R{round_idx}: converged={conv_result['converged']}, "
          f"criteria={ {k: v['passed'] for k, v in conv_result['criteria'].items()} }")
    print(f"[CONVERGENCE] R{round_idx}: {conv_result['recommendation']}")

    diag["candidate_repairs"] = candidate_repairs
    diag["best_candidates"] = best_candidates
    diag["branch_evaluations"] = branch_evaluations
    diag["convergence"] = conv_result

    # ---- Post-process: code-layer structured DOE factors ----
    # Replace LLM-generated next_round_doe.factors with stable, code-generated
    # factor definitions based on actionable levers and parent materials.
    diag["next_round_doe"] = _build_structured_doe(
        diag.get("next_round_doe") or {},
        diag.get("actionable_levers") or [],
        out_dir,
        round_idx,
    )

    diag_path = out_dir / f"R{round_idx}_diagnosis.json"
    write_json(diag_path, diag)

    kpi = compute_kpis(results_rows)
    kpi["round"] = round_idx
    return diag_path, kpi
