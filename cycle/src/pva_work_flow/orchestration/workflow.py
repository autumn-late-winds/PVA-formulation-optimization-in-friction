import yaml
from pathlib import Path

_prompts_path = Path(__file__).resolve().parents[1] / "prompts" / "prompts_en.yaml"
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
from pva_work_flow.core.llm_engines import LLM
from pva_work_flow.core.utils import read_json, write_json, _to_float_or_none, safe_json_loads
from pva_work_flow.artifacts.io_artifacts import export_doe_csv, export_results_template, read_results_filled, compute_kpis, aggregate_cof_from_row
from pva_work_flow.core.config import CONSTRAINTS, GenerationMode, CandidateDict, CONVERGENCE as _DEFAULT_CONVERGENCE
from pva_work_flow.artifacts.experiment_notes import build_notes_context_for_diagnosis
from pva_work_flow.wetlab.wetlab_outcomes import compute_cvs, has_failure
from pva_work_flow.planning.formulation_checks import (
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
                "lever": "post_soak_hours",
                "direction": "decrease",
                "target_range": "0.083-0.25 h",
                "target_material": None,
                "justification": "Fracture/rupture in thin PVA films can come from over-soaking or post-gel shrinkage treatment; first test a 5 min soak."
            })
            suggestions.append({
                "lever": "pva_wt_percent",
                "direction": "increase",
                "target_range": "18-20 wt%",
                "target_material": None,
                "justification": "Repeated rupture suggests insufficient PVA matrix strength; raise PVA before tuning GA/HCl loading."
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
                "lever": "pva_wt_percent",
                "direction": "increase",
                "target_material": None,
                "target_range": "18-20 wt%",
                "justification": "Catastrophic early rupture suggests increasing PVA matrix strength before changing network type."
            })
            suggestions.append({
                "lever": "post_soak_hours",
                "direction": "decrease",
                "target_material": None,
                "target_range": "0.083-0.25 h",
                "justification": "Use a short 5 min post-gel treatment to separate over-soak rupture from intrinsic formulation weakness."
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
        "name": "pva_wt_percent", "levels": ["12.0", "18.0", "20.0"],
        "operational_definition": "PVA weight percent in 20 g batch; after repeated rupture prioritize 18-20 wt%",
    },
    "freeze_thaw_cycles": {
        "name": "freeze_thaw_cycles", "levels": ["0", "1", "2"],
        "operational_definition": "Number of freeze-thaw cycles",
    },
    "post_soak_hours": {
        "name": "post_soak_hours", "levels": ["0.083", "0.25", "0.5", "1"],
        "operational_definition": "Hours of post-gel DI water or GA/HCl shrinkage treatment before testing; 0.083 h is 5 min",
    },
    "additive": {
        "name": "additive_type", "levels": [],
        "operational_definition": "Primary additive in formulation",
        "_dynamic": True,  # levels populated from parent materials
    },
    "crosslink": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "True crosslinker concentration level; GA/HCl in this PVA path may instead be optional post-gel shrinkage treatment",
    },
    "cross_link": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "True crosslinker concentration level; GA/HCl in this PVA path may instead be optional post-gel shrinkage treatment",
    },
    "cross_linking": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "True crosslinker concentration level; GA/HCl in this PVA path may instead be optional post-gel shrinkage treatment",
    },
    "cross-linking": {
        "name": "crosslinker_concentration", "levels": ["low", "medium", "high"],
        "operational_definition": "True crosslinker concentration level; GA/HCl in this PVA path may instead be optional post-gel shrinkage treatment",
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
    """Evaluate stop/continue criteria for PVA hydrogel optimization.

    Termination is candidate-level: one sample must simultaneously pass COF,
    modulus, friction stability, repeat count, and integrity checks.
    """
    cfg = convergence or _DEFAULT_CONVERGENCE

    def _num(row: dict, key: str) -> float | None:
        raw = row.get(key)
        if raw is None or str(raw).strip().lower() in ("", "na", "nan", "none"):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _cof_repeat_count(row: dict) -> int:
        n = sum(1 for k in ("COF_mean_1", "COF_mean_2", "COF_mean_3") if _num(row, k) is not None)
        return n if n else (1 if _num(row, "cof_steady_mean") is not None else 0)

    # Include explicit notes in failure accounting. This catches samples that
    # ruptured before a full result row could be written.
    from pva_work_flow.artifacts.experiment_notes import load_notes as _load_notes

    notes_obj = _load_notes(out_dir, round_idx)
    note_error_codes: dict[str, list[str]] = {}
    for cid, entry in notes_obj.items() if isinstance(notes_obj, dict) else []:
        if str(cid).startswith("_") or not isinstance(entry, dict):
            continue
        codes = [str(code).upper() for code in (entry.get("error_codes") or [])]
        if codes:
            note_error_codes[str(cid)] = codes

    rows_by_id = {(r.get("candidate_id") or "").strip(): r for r in results_rows if r.get("candidate_id")}

    # ---- Current-round scalar summaries ----
    best_cof = None
    best_cvs = None
    best_cvs_candidate = None
    target_hits: list[dict] = []
    evaluated_count = 0
    failed_count = 0

    for r in results_rows:
        cid = (r.get("candidate_id") or "").strip()
        codes = note_error_codes.get(cid, [])
        cof, _std = aggregate_cof_from_row(r)
        mod = _num(r, "compression_modulus_MPa")
        stable = _num(r, "stable_proportion")
        stick = _num(r, "stick_slip_score")
        comp_n = int(_num(r, "compression_modulus_n") or 0)
        cof_n = _cof_repeat_count(r)
        failed = bool(codes) or has_failure(r)
        if cof is not None or failed:
            evaluated_count += 1
        if failed:
            failed_count += 1

        if cof is not None and not failed and (best_cof is None or float(cof) < best_cof):
            best_cof = float(cof)

        cvs_result = compute_cvs(r, error_codes=codes or None)
        cvs = float(cvs_result.get("cvs") or 0.0)
        if best_cvs is None or cvs > best_cvs:
            best_cvs = cvs
            best_cvs_candidate = cid or None

        candidate_pass = (
            not failed
            and cof is not None
            and float(cof) <= cfg["cof_max"]
            and mod is not None
            and cfg["modulus_min_mpa"] <= mod <= cfg["modulus_max_mpa"]
            and stable is not None
            and stable >= cfg["stable_proportion_min"]
            and stick is not None
            and stick <= cfg["stick_slip_max"]
            and cof_n >= int(cfg.get("min_replicates_for_success", 1))
            and comp_n >= int(cfg.get("min_replicates_for_success", 1))
        )
        if candidate_pass:
            target_hits.append({
                "candidate_id": cid,
                "cof": round(float(cof), 6),
                "compression_modulus_MPa": mod,
                "stable_proportion": stable,
                "stick_slip_score": stick,
                "cof_repeats": cof_n,
                "compression_repeats": comp_n,
                "cvs": cvs,
            })

    # Count note-only failures that have no result row.
    for cid, codes in note_error_codes.items():
        if cid not in rows_by_id and {"ERROR1", "ERROR2", "ERROR3"} & set(codes):
            evaluated_count += 1
            failed_count += 1

    failure_rate = (failed_count / evaluated_count) if evaluated_count else 0.0

    # ---- Evaluate stop criteria ----
    criteria: dict[str, dict] = {}

    target_success = bool(target_hits)
    criteria["target_success"] = {
        "passed": target_success,
        "value": target_hits,
        "threshold": {
            "cof_max": cfg["cof_max"],
            "modulus_range": [cfg["modulus_min_mpa"], cfg["modulus_max_mpa"]],
            "stable_proportion_min": cfg["stable_proportion_min"],
            "stick_slip_max": cfg["stick_slip_max"],
            "min_replicates_for_success": cfg.get("min_replicates_for_success", 1),
        },
        "label": "one candidate simultaneously passes COF, modulus, stability, repeats, and no-failure checks",
    }

    # Plateau stop: two consecutive rounds where both best COF and best CVS do
    # not improve meaningfully.
    flat_needed = int(cfg.get("flat_trend_consecutive") or cfg.get("cof_trend_consecutive") or 2)
    kpi_history: list[dict] = []
    kpi_path = out_dir / "kpi_log.json"
    if kpi_path.exists():
        kpi_history = [e for e in read_json(kpi_path) if e.get("round") != round_idx]
    current_kpi = {
        "round": round_idx,
        "best_valid_cof": best_cof,
        "best_cvs": best_cvs,
        "failure_rate": failure_rate,
    }
    trend_series = sorted(kpi_history + [current_kpi], key=lambda e: e.get("round", 0))

    flat_consecutive = 0
    for i in range(len(trend_series) - 1, 0, -1):
        cur = trend_series[i]
        prev = trend_series[i - 1]
        cur_cof = _to_float_or_none(cur.get("best_valid_cof", cur.get("best_cof")))
        prev_cof = _to_float_or_none(prev.get("best_valid_cof", prev.get("best_cof")))
        cur_cvs = _to_float_or_none(cur.get("best_cvs"))
        prev_cvs = _to_float_or_none(prev.get("best_cvs"))
        if cur_cof is None or prev_cof is None or cur_cvs is None or prev_cvs is None:
            break
        cof_improvement = prev_cof - cur_cof
        cvs_improvement = cur_cvs - prev_cvs
        if cof_improvement < cfg["cof_trend_delta"] and cvs_improvement < cfg.get("cvs_trend_delta", 5.0):
            flat_consecutive += 1
        else:
            break

    plateau_stop = flat_consecutive >= flat_needed
    criteria["plateau_stop"] = {
        "passed": plateau_stop,
        "value": flat_consecutive,
        "threshold": flat_needed,
        "label": f"stop branch if COF improves < {cfg['cof_trend_delta']} and CVS improves < {cfg.get('cvs_trend_delta', 5.0)} for {flat_needed} consecutive rounds",
    }

    fail_needed = int(cfg.get("failure_rate_consecutive", 2))
    fail_consecutive = 0
    for entry in reversed(trend_series):
        n_val = _to_float_or_none(entry.get("n"))
        failed_val = _to_float_or_none(entry.get("failed_count"))
        rate = _to_float_or_none(entry.get("failure_rate"))
        if rate is None and n_val and failed_val is not None:
            rate = failed_val / n_val
        if rate is not None and rate >= cfg.get("failure_rate_stop", 0.5):
            fail_consecutive += 1
        else:
            break

    failure_stop = fail_consecutive >= fail_needed
    criteria["failure_stop"] = {
        "passed": failure_stop,
        "value": {"current_failure_rate": round(failure_rate, 4), "consecutive_rounds": fail_consecutive},
        "threshold": {"failure_rate": cfg.get("failure_rate_stop", 0.5), "consecutive_rounds": fail_needed},
        "label": "stop branch after repeated high rupture/no-gel/too-soft failure rate",
    }

    budget_stop = round_idx >= int(cfg.get("max_round", 8)) and not target_success
    criteria["budget_stop"] = {
        "passed": budget_stop,
        "value": round_idx,
        "threshold": cfg.get("max_round", 8),
        "label": "stop local tweaking at max_round if no target hit",
    }

    # ---- Aggregate verdict ----
    converged = target_success
    should_stop = target_success or plateau_stop or failure_stop or budget_stop
    stop_reasons: list[str] = []
    if target_success:
        stop_reasons.append("target_success")
    if plateau_stop:
        stop_reasons.append("plateau_stop")
    if failure_stop:
        stop_reasons.append("failure_stop")
    if budget_stop:
        stop_reasons.append("budget_stop")

    if target_success:
        best_hit = sorted(target_hits, key=lambda x: (-x["cvs"], x["cof"]))[0]
        recommendation = (
            f"STOP main optimization at R{round_idx}: {best_hit['candidate_id']} meets "
            "COF/modulus/stability/no-failure/repeat criteria. "
            "Enter robustness validation with 3-5 independent repeats."
        )
    elif plateau_stop:
        recommendation = (
            f"STOP this branch at R{round_idx}: COF and CVS have been flat for "
            f"{flat_consecutive} consecutive rounds. Switch root tree or introduce a new lubrication strategy."
        )
    elif failure_stop:
        recommendation = (
            f"STOP this branch at R{round_idx}: failure rate has stayed >= "
            f"{cfg.get('failure_rate_stop', 0.5):.0%} for {fail_consecutive} consecutive rounds."
        )
    elif budget_stop:
        recommendation = (
            f"STOP local tweaking at R{round_idx}: max_round={cfg.get('max_round', 8)} reached "
            "without a target hit. Change the material strategy rather than continuing small mutations."
        )
    else:
        recommendation = (
            f"CONTINUE after R{round_idx}: no candidate has met all target criteria, "
            "and plateau/failure/budget stop conditions are not yet triggered."
        )

    return {
        "converged": converged,
        "should_stop": should_stop,
        "stop_reasons": stop_reasons,
        "round": round_idx,
        "criteria": criteria,
        "current_best": {
            "best_valid_cof": best_cof,
            "best_cvs": best_cvs,
            "best_cvs_candidate": best_cvs_candidate,
            "failure_rate": failure_rate,
            "evaluated_count": evaluated_count,
            "failed_count": failed_count,
        },
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
        from pva_work_flow.memory.formulation_rag import build_formulation_rag_context

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
        
    # ---- Per-branch dCOF evaluation (tree mode) + CVS ranking ----
    # Load all prior-round COF values so each child compares against its true
    # parent, even if that parent is older than R(n-1).
    _parent_cof: dict[str, float] = load_prior_cof_by_candidate(out_dir, round_idx)

    # ---- Resolve error codes from experiment_notes for CVS computation ----
    from pva_work_flow.artifacts.experiment_notes import load_notes as _load_notes
    _notes = _load_notes(out_dir, round_idx)
    _error_codes_by_id: dict[str, list[str]] = {}
    if _notes:
        for _cid, _entry in _notes.items():
            if _cid.startswith("_") or not isinstance(_entry, dict):
                continue
            _errs = _entry.get("error_codes") or []
            if _errs:
                _error_codes_by_id[_cid] = [str(e) for e in _errs]

    # ---- Compute CVS for every result row ----
    _cvs_cache: dict[str, dict] = {}
    for r in results_rows:
        cid = r.get("candidate_id")
        if not cid:
            continue
        ec = _error_codes_by_id.get(cid)
        _cvs_cache[cid] = compute_cvs(r, error_codes=ec if ec else None)

    scored_rows: list[dict] = []
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
        cvs_result = _cvs_cache.get(cid, {})
        scored_rows.append({
            "cid": cid,
            "cof": float(cof),
            "std": std_val,
            "failure_type": failure_type,
            "dcof": dcof,
            "cvs": cvs_result.get("cvs", 0.0),
            "cvs_grade": cvs_result.get("grade", "F"),
            "cvs_i": cvs_result.get("i_multiplier", 1.0),
            "cvs_p": cvs_result.get("p_score", 0.0),
            "cvs_s": cvs_result.get("s_score", 0.0),
        })

    # ---- Integrity-gated CVS ranking ----
    # Manual notes capture rupture, spikes, indentation, and surface issues that
    # can make a high-looking friction trace unreliable. Prefer clean samples
    # first, then rank by CVS within the same integrity class.
    scored_rows.sort(key=lambda x: (x["cvs_i"] < 1.0, -x["cvs"]))

    # Best candidates: top 3 by integrity-gated CVS ranking.
    best_candidates = [x["cid"] for x in scored_rows[:3]]

    # ---- Per-branch evaluation summaries (dCOF-based, for tree-mode diagnosis) ----
    branch_evaluations: list[dict] = []
    for x in scored_rows:
        cid = x["cid"]
        parent_cid = (by_id.get(cid, {}).get("parent_candidate_id") or "").strip()
        dcof = x["dcof"]
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
            "cof": round(x["cof"], 6),
            "dcof": round(dcof, 6) if dcof is not None else None,
            "verdict": verdict,
            "cvs": x["cvs"],
            "cvs_grade": x["cvs_grade"],
        })

    # ---- Re-mark candidate_repairs for mechanically-failed candidates ----
    _failed_ids = {cid for cid, cv in _cvs_cache.items() if cv.get("i_multiplier", 1.0) < 1.0}
    for _repair in candidate_repairs:
        _rcid = _repair.get("candidate_id", "")
        if _rcid in _failed_ids:
            _cv = _cvs_cache.get(_rcid, {})
            _repair["performance_class"] = "mechanically_failed"
            _repair["cvs"] = _cv.get("cvs")
            _repair["cvs_grade"] = _cv.get("grade")
            _repair.setdefault("_experiment_note_warning",
                "COF data unreliable due to mechanical failure (rupture/no-gel). "
                f"CVS I-multiplier={_cv.get('i_multiplier', '?')}.")
        elif _rcid in _cvs_cache:
            _cv = _cvs_cache[_rcid]
            _repair["cvs"] = _cv.get("cvs")
            _repair["cvs_grade"] = _cv.get("grade")

    # ---- CVS summary for diagnosis output ----
    _cvs_summary = {
        "best_cvs": scored_rows[0]["cvs"] if scored_rows else 0.0,
        "best_grade": scored_rows[0]["cvs_grade"] if scored_rows else "F",
        "cvs_ranking": [
            {"candidate_id": x["cid"], "cvs": x["cvs"], "grade": x["cvs_grade"],
             "i_multiplier": x["cvs_i"], "p_score": x["cvs_p"], "s_score": x["cvs_s"]}
            for x in scored_rows[:8]
        ],
        "mechanically_failed_count": len(_failed_ids),
        "mechanically_failed_ids": sorted(_failed_ids) if _failed_ids else [],
        "cvs_explanation": (
            "CVS = I x P x S x 100. "
            "I = integrity gate (1.0=no failure, 0.25=rupture, 0.00=no gel). "
            "P = 40%COF + 25%wear + 20%modulus + 15%COF_std. "
            "S = 40%stable_prop + 30%(1-stick_slip) + 30%plateau_ratio."
        ),
    }

    # ---- Convergence check ----
    conv_result = check_convergence(results_rows, out_dir, round_idx, convergence)
    print(f"[CONVERGENCE] R{round_idx}: converged={conv_result['converged']}, "
          f"criteria={ {k: v['passed'] for k, v in conv_result['criteria'].items()} }")
    print(f"[CONVERGENCE] R{round_idx}: {conv_result['recommendation']}")

    diag["candidate_repairs"] = candidate_repairs
    diag["best_candidates"] = best_candidates
    diag["branch_evaluations"] = branch_evaluations
    diag["cvs_summary"] = _cvs_summary
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

    kpi = compute_kpis(results_rows, error_codes_by_id=_error_codes_by_id)
    kpi["round"] = round_idx
    return diag_path, kpi
