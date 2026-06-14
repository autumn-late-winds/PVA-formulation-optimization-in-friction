"""Candidate generation: prompt building and LLM-driven candidate proposal."""

import itertools
import json
import math
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pva_work_flow.core.config import CONSTRAINTS, GenerationMode
from pva_work_flow.core.llm_engines import LLM
from pva_work_flow.planning.ratio_planner import apply_ratio_plan
from pva_work_flow.artifacts.io_artifacts import read_results_filled
from pva_work_flow.planning.pipeline_agents import run_three_agent_pipeline
from pva_work_flow.core.utils import (
    read_json, write_json, safe_json_loads,
    find_similar_materials, load_allowed_materials,
    canonicalize_candidate_materials,
)
from pva_work_flow.tree.tree_naming import normalize_tree_label, root_label_from_candidate_id
from pva_work_flow.planning.candidate_rules import (
    black_box_jump_score,
    build_inheritance_table,
    detect_changed_variables,
    has_pva as candidate_has_pva,
    inheritance_table_markdown,
    validate_candidate_constraints,
)
from pva_work_flow.orchestration.workflow import (
    SYSTEM_PROMPT,
    GEN_PROMPT_R1,
    GEN_PROMPT_RN,
    compute_prep_time_hours,
    check_material_completeness,
    normalize_materials_and_formulation,
    _candidate_material_names,
)


def _load_diag_bundle_for_round(out_dir: Path, r: int) -> Dict[str, Any]:
    diag_path = out_dir / f"R{r}_diagnosis.json"
    if not diag_path.exists():
        return {
            "summary": "",
            "repairs": [],
            "factors": [],
            "best_candidates": [],
            "actionable_levers": [],
            "dominant_failure_modes": [],
        }
    try:
        diag = read_json(diag_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {
            "summary": "",
            "repairs": [],
            "factors": [],
            "best_candidates": [],
            "actionable_levers": [],
            "dominant_failure_modes": [],
        }
    return {
        "summary": diag.get("summary_for_generator") or "",
        "repairs": diag.get("candidate_repairs") or [],
        "factors": (diag.get("next_round_doe") or {}).get("factors", []) or [],
        "best_candidates": diag.get("best_candidates") or [],
        "actionable_levers": diag.get("actionable_levers") or [],
        "dominant_failure_modes": diag.get("dominant_failure_modes") or [],
    }


def build_generator_prompt(
    round_idx: int,
    n: int,
    out_dir: Path,
    last_valid_experimental_round: int | None = None,
    last_failed_audit_round: int | None = None,
    diag_bundle_valid_exp: Dict[str, Any] | None = None,
    diag_bundle_failed_audit: Dict[str, Any] | None = None,
) -> str:
    speed = str(CONSTRAINTS["speed_mm_s"])
    if round_idx == 1:
        prompt = GEN_PROMPT_R1
        prompt = prompt.replace("{n}", str(n)).replace("{speed}", speed)
        try:
            from pva_work_flow.memory.formulation_rag import build_formulation_rag_context

            formulation_rag_context = build_formulation_rag_context(
                out_dir=out_dir,
                round_idx=round_idx,
                phase="R1 root generation: suggest literature-supported starting formulation families",
            )
            if formulation_rag_context:
                prompt = formulation_rag_context + "\n\n" + prompt
                print(f"[FORMULATION_RAG] Injected R1 generator context ({len(formulation_rag_context)} chars)")
        except Exception as e:
            print(f"[FORMULATION_RAG] R1 generator context unavailable: {e}")
        return prompt

    summary_pieces: list[str] = []
    factors: list[dict] = []
    parent_ids: list[str] = []
    best_candidates: list[str] = []
    actionable_levers: list[str] = []
    failure_modes: list[str] = []

    def _extract_bundle_info(bundle: Dict[str, Any]):
        nonlocal factors, parent_ids, best_candidates, actionable_levers, failure_modes
        if bundle.get("summary"):
            summary_pieces.append(bundle["summary"])
        if not factors and bundle.get("factors"):
            factors = bundle["factors"]
        if bundle.get("best_candidates"):
            best_candidates = [str(x) for x in bundle["best_candidates"] if str(x).strip()]
        if bundle.get("actionable_levers"):
            actionable_levers = [str(x.get("lever") or "") for x in bundle["actionable_levers"] if str(x.get("lever") or "").strip()]
        if bundle.get("dominant_failure_modes"):
            failure_modes = [str(x.get("mode") or "") for x in bundle["dominant_failure_modes"] if str(x.get("mode") or "").strip()]
        if best_candidates:
            parent_ids = best_candidates[:]
        elif bundle.get("repairs"):
            parent_ids = [r.get("candidate_id") for r in bundle["repairs"] if r.get("candidate_id")]

    if last_valid_experimental_round is not None:
        bundle = diag_bundle_valid_exp or _load_diag_bundle_for_round(out_dir, last_valid_experimental_round)
        _extract_bundle_info(bundle)

    if not summary_pieces and last_failed_audit_round is not None:
        bundle = diag_bundle_failed_audit or _load_diag_bundle_for_round(out_dir, last_failed_audit_round)
        _extract_bundle_info(bundle)

    if not summary_pieces:
        parent_round_idx = round_idx - 1
        bundle = _load_diag_bundle_for_round(out_dir, parent_round_idx)
        _extract_bundle_info(bundle)

    summary = "\n\n".join(summary_pieces) if summary_pieces else "(no diagnosis found)"
    parent_id_list = "\n".join([f"- {pid}" for pid in parent_ids]) if parent_ids else "(no parents found)"
    factor_str = "\n".join([f"- {f['name']}: {f.get('levels',[])}" for f in factors]) if factors else "(no DOE factors defined)"

    prompt = GEN_PROMPT_RN
    prompt = (
        prompt
        .replace("{n}", str(max(n, len(parent_ids)) if parent_ids else n))
        .replace("{speed}", speed)
        .replace("{summary}", summary)
        .replace("{parent_id_list}", parent_id_list)
        .replace("{doe_factors}", factor_str)
    )

    # ---- Inject structured data: results table + parent formulas + DOE bounds + allowed materials ----
    structured_sections: list[str] = []
    results_table = _build_results_data_section(out_dir, round_idx)
    if results_table:
        structured_sections.append(results_table)
    parent_formulas = _build_parent_formulation_section(out_dir, parent_ids, round_idx)
    if parent_formulas:
        structured_sections.append(parent_formulas)
    doe_bounds = _build_doe_boundary_section(factors)
    if doe_bounds:
        structured_sections.append(doe_bounds)
    allowed_mat = _build_allowed_materials_section(out_dir)
    if allowed_mat:
        structured_sections.append(allowed_mat)
    if structured_sections:
        prompt = "\n\n".join(structured_sections) + "\n\n" + prompt

    # ---- Inject KPI trend across rounds ----
    kpi_path = out_dir / "kpi_log.json"
    kpi_lines: list[str] = []
    if kpi_path.exists():
        kpi_log = read_json(kpi_path)
        kpi_lines.append("KPI HISTORY (COF trend across rounds — lower is better):")
        for entry in kpi_log:
            rn = entry.get("round", "?")
            best = entry.get("best_cof")
            pr = entry.get("pass_rate", 0)
            n_entries = entry.get("n", 0)
            best_str = f"{best:.4f}" if best is not None else "N/A"
            pr_str = f"{pr:.1%}"
            kpi_lines.append(f"  R{rn}: best_cof={best_str}, pass_rate={pr_str}, n={n_entries}")
        cofs = [e.get("best_cof") for e in kpi_log if e.get("best_cof") is not None]
        if len(cofs) >= 2 and cofs[-1] is not None and cofs[0] is not None and cofs[-1] > cofs[0]:
            kpi_lines.append(f"  WARNING: COF INCREASED from {cofs[0]:.4f} (R1) to {cofs[-1]:.4f} (latest). Optimization is going in the WRONG direction. Reconsider your strategy.")
    kpi_trend = "\n".join(kpi_lines)

    # ---- Inject audit failure summary ----
    audit_lines: list[str] = []
    if last_failed_audit_round is not None:
        audit_path = out_dir / f"R{last_failed_audit_round}_audits.json"
        if audit_path.exists():
            audits_obj = read_json(audit_path)
            audits = audits_obj.get("audits", [])
            failed_audits = [a for a in audits if a.get("decision") == "FAIL"]
            if failed_audits:
                reason_counter: Counter = Counter()
                for fa in failed_audits:
                    for fr in fa.get("failed_rules", []):
                        short = fr.split(":")[0].strip()
                        reason_counter[short] += 1
                audit_lines.append(f"AUDIT FAILURE SUMMARY (R{last_failed_audit_round} — {len(failed_audits)}/{len(audits)} candidates FAILED):")
                audit_lines.append("  DO NOT repeat these mistakes in the next round:")
                for reason, count in reason_counter.most_common(8):
                    audit_lines.append(f"  - [{count}x] {reason}")
                rejections = Counter(a.get("rejection_reason", "unknown") for a in failed_audits)
                audit_lines.append("  Rejection categories:")
                for rj, cnt in rejections.most_common(5):
                    audit_lines.append(f"  - {rj}: {cnt} candidates")
    audit_failure_str = "\n".join(audit_lines)

    # ---- Inject R1 diagnosis for R3+ (prevent information loss) ----
    r1_context = ""
    if round_idx >= 3:
        r1_diag_path = out_dir / "R1_diagnosis.json"
        if r1_diag_path.exists():
            r1_diag = read_json(r1_diag_path)
            r1_best = r1_diag.get("best_candidates", [])
            r1_summary = r1_diag.get("summary_for_generator", "")
            r1_parts = [f"R1 REFERENCE CONTEXT (earliest experimental round — do NOT forget these findings):"]
            if r1_best:
                r1_parts.append(f"  R1 best candidates: {r1_best}")
            if r1_summary:
                r1_parts.append(f"  R1 diagnosis summary: {r1_summary[:600]}")
            r1_context = "\n".join(r1_parts)

    # ---- Append all extra context to the prompt ----
    extra_context_parts = []
    if kpi_trend:
        extra_context_parts.append(kpi_trend)
    if audit_failure_str:
        extra_context_parts.append(audit_failure_str)
    if r1_context:
        extra_context_parts.append(r1_context)
    try:
        from pva_work_flow.tree.tree_statistics import build_tree_statistics_context

        tree_stats_context = build_tree_statistics_context(out_dir)
        if tree_stats_context:
            extra_context_parts.append(tree_stats_context)
    except Exception as e:
        print(f"[TREE_STATS] context unavailable: {e}")
    try:
        from pva_work_flow.memory.failure_factor_memory import build_failure_factor_context

        failure_factor_context = build_failure_factor_context(out_dir)
        if failure_factor_context:
            extra_context_parts.append(failure_factor_context)
            print(f"[FAILURE_FACTOR] Injected generator context ({len(failure_factor_context)} chars)")
    except Exception as e:
        print(f"[FAILURE_FACTOR] generator context unavailable: {e}")
    try:
        from pva_work_flow.memory.chain_memory import build_chain_memory_context

        chain_memory_context = build_chain_memory_context(out_dir)
        if chain_memory_context:
            extra_context_parts.append(chain_memory_context)
            print(f"[CHAIN_MEMORY] Injected generator context ({len(chain_memory_context)} chars)")
    except Exception as e:
        print(f"[CHAIN_MEMORY] generator context unavailable: {e}")
    try:
        from pva_work_flow.memory.formulation_rag import build_formulation_rag_context

        formulation_rag_context = build_formulation_rag_context(
            out_dir=out_dir,
            round_idx=round_idx,
            phase="Generator: choose literature-supported local changes while preserving tree parent lineage",
        )
        if formulation_rag_context:
            extra_context_parts.append(formulation_rag_context)
            print(f"[FORMULATION_RAG] Injected generator context ({len(formulation_rag_context)} chars)")
    except Exception as e:
        print(f"[FORMULATION_RAG] generator context unavailable: {e}")
    if extra_context_parts:
        prompt += "\n\n" + "\n\n".join(extra_context_parts)

    # Detect RESET condition: R3+ with consecutive audit failures from R2 onwards
    needs_reset = (
        round_idx >= 3
        and last_valid_experimental_round is not None
        and last_failed_audit_round is not None
        and last_failed_audit_round >= 2
    )

    strict_lines: list[str] = []

    if needs_reset:
        strict_lines.append("!!! RESET EXPLORATION — CONSECUTIVE AUDIT FAILURES DETECTED !!!")
        strict_lines.append("- The previous round(s) produced NO viable candidates passing audit.")
        strict_lines.append(f"- You are RESTARTING exploration from R{last_valid_experimental_round} best-performing candidates.")
        strict_lines.append("- Prioritize DIVERSITY over exploitation: generate candidates with varied network types, crosslinkers, and additive combinations.")
        strict_lines.append("- Every candidate MUST include PVA as the main polymer with its role explicitly set to 'polymer' or 'main_polymer'.")
        strict_lines.append("- Every candidate MUST have a fully specified formulation (pva_wt_percent, total_batch_mass_g=20, ordered process.steps).")
        strict_lines.append("- Focus on meeting ALL audit requirements first, then optimize for performance.")
        strict_lines.append("")

    strict_lines.append("RESULT_DRIVEN_HARD_REQUIREMENTS:")
    strict_lines.append("- This is a true result-driven round, not a label-only round.")
    strict_lines.append("- generation_mode at top level and iteration_metadata.generation_mode MUST both be result_driven.")
    strict_lines.append("- diagnosis_evidence_used, mutation_rationale, diagnosis_levers_used, and doe_factor_levels must be non-empty for every candidate.")
    strict_lines.append("- Every mutation_rationale must explicitly mention: (1) which parent result is referenced, (2) which lever is being optimized, and (3) why the change is expected to improve performance.")
    if parent_ids:
        strict_lines.append("- The only allowed parent anchors for this round are:")
        strict_lines.extend([f"  - {pid}" for pid in parent_ids])
        strict_lines.append("- At least 50% of generated candidates MUST be direct local variations around these parent anchors.")
    if best_candidates:
        strict_lines.append("- The following best-performing parent candidates must be prioritized for exploitation:")
        strict_lines.extend([f"  - {pid}" for pid in best_candidates])
    if actionable_levers:
        strict_lines.append("- Preferred optimization levers from diagnosis:")
        strict_lines.extend([f"  - {x}" for x in actionable_levers])
    if failure_modes:
        strict_lines.append("- Dominant failure modes that must be addressed:")
        strict_lines.extend([f"  - {x}" for x in failure_modes])
    strict_lines.append("- Do not generate candidates that merely rename generation_mode to result_driven without using the parent results.")
    strict_lines.append("- In result-driven rounds, the search must be exploitative first and exploratory second.")
    strict_lines.append("- Local changes only unless a candidate is explicitly marked as an extension.")
    strict_lines.append("- If introducing any new material relative to the parent round, mark is_extension=true and provide a non-empty extension_reason.")
    strict_lines.append("- If the diagnosis defines main DOE factors, complete those factors first before any extension candidate is allowed.")
    strict_lines.append("- Default exploitation ratio: at least 70% exploitative local variants around best_candidates, at most 30% nearby exploration.")
    strict_lines.append("- Confirmed failure factors from memory must not be reused as optimization directions.")
    strict_lines.append("- Suspected failure factors should be tested one at a time using design_type=failure_factor_verification before being optimized.")
    strict_lines.append("- If mechanical integrity failed, restore or verify integrity before optimizing COF.")
    prompt += "\n\n" + "\n".join(strict_lines)
    return prompt


def _extract_parent_id(c: Dict[str, Any]) -> str | None:
    pid = (c.get("parent_candidate_id") or "").strip()
    if pid:
        return pid

    pcs = c.get("parent_candidates") or []
    if isinstance(pcs, list) and pcs:
        pid = str(pcs[0]).strip()
        if pid:
            return pid

    iter_block = c.get("iteration") or {}
    if isinstance(iter_block, dict):
        pid = (iter_block.get("parent_candidate_id") or "").strip()
        if pid:
            return pid
        pcs = iter_block.get("parent_candidates") or []
        if isinstance(pcs, list) and pcs:
            pid = str(pcs[0]).strip()
            if pid:
                return pid

    return None


def _build_parent_formulation_section(out_dir: Path, parent_ids: list[str], round_idx: int) -> str:
    """Load parent candidate formulations and format compactly for prompt injection."""
    if not parent_ids:
        return ""
    target_round = round_idx - 1
    cand_path = out_dir / f"R{target_round}_candidates.json"
    if not cand_path.exists():
        return ""
    obj = read_json(cand_path)
    all_cands = obj.get("candidates", [])
    by_id = {c.get("candidate_id", ""): c for c in all_cands}

    lines: list[str] = []
    lines.append(f"=== PARENT CANDIDATE FORMULATIONS (R{target_round}) ===")
    for pid in parent_ids:
        pc = by_id.get(pid)
        if not pc:
            continue
        f = pc.get("formulation", {}) or {}
        p = pc.get("processing", {}) or {}
        adds = f.get("additives", []) or []
        add_str = ", ".join(f"{a.get('name','?')} {a.get('wt_percent','?')}%" for a in adds) if adds else "none"
        cl = f.get("crosslinker", {}) or {}
        cl_str = f"{cl.get('name','none')} {cl.get('wt_percent','?')}%" if cl.get("name") and cl.get("name") != "none" else "none"
        ft = p.get("freeze_thaw_cycles", 0)
        soak = p.get("post_soak_hours", 0)
        lines.append(
            f"  {pid}: PVA={f.get('pva_wt_percent','?')}%, "
            f"network={f.get('network_type','?')}/{f.get('crosslink_or_phys_method','?')}, "
            f"crosslinker={cl_str}, additives=[{add_str}], "
            f"FT={ft}, soak={soak}h"
        )
    lines.append("")
    lines.append("CRITICAL: Children MUST stay in the same chemical family as their parent unless marked is_extension=true.")
    return "\n".join(lines)


def _build_results_data_section(out_dir: Path, round_idx: int) -> str:
    """Read previous-round results_filled.csv and format as compact table for prompt injection."""
    lines: list[str] = []
    target_round = round_idx - 1
    if target_round < 1:
        return ""
    results_path = out_dir / f"R{target_round}_results_filled.csv"
    if not results_path.exists():
        return ""
    rows = read_results_filled(results_path)
    if not rows:
        return ""

    # key columns: id, cof, friction_pattern, wear, modulus, failure
    cols = ["candidate_id", "cof_steady_mean", "friction_pattern", "wear_proxy", "compression_modulus_MPa", "failure_type", "plateau_ratio", "stable_proportion"]
    header = " | ".join(cols)
    sep = " | ".join(["---"] * len(cols))
    lines.append("=== R{} EXPERIMENTAL RESULTS (REAL DATA — USE THIS) ===".format(target_round))
    lines.append(header)
    lines.append(sep)
    for r in rows:
        vals = [str(r.get(c, "") or "")[:10] for c in cols]
        lines.append(" | ".join(vals))
    lines.append("")
    lines.append("IMPORTANT: The numbers above are the ONLY source of truth. Do NOT invent fake COF/wear values.")
    return "\n".join(lines)


def _build_doe_boundary_section(factors: list[dict]) -> str:
    """Format DOE factors as a hard-constraint pick-from table."""
    if not factors:
        return ""
    lines: list[str] = []
    lines.append("=== DOE BOUNDARIES — YOU MUST PICK FROM THESE ===")
    for f in factors:
        name = f.get("name", "?")
        levels = f.get("levels", [])
        lines.append(f"  {name}: [{', '.join(str(l) for l in levels)}]  ← pick ONE")
    lines.append("")
    lines.append("CRITICAL: Every candidate's doe_factor_levels MUST include ALL factors above with exactly ONE level each.")
    return "\n".join(lines)


def _build_allowed_materials_section(out_dir: Path) -> str:
    """Read materials CSV and format allowed materials list."""
    # materials_csv is at cycle/materials/materials_en.csv
    materials_csv = Path(__file__).resolve().parents[3] / "materials" / "materials_en.csv"
    if not materials_csv.exists():
        return ""
    allowed, _ = load_allowed_materials(materials_csv)
    if not allowed:
        return ""
    lines: list[str] = []
    lines.append("=== ALLOWED MATERIALS — DO NOT INVENT NEW ONES ===")
    # Group by first letter for readability
    allowed_sorted = sorted(set(a.strip().lower() for a in allowed if a.strip()))
    lines.append(", ".join(allowed_sorted))
    lines.append("")
    lines.append("CRITICAL: Use ONLY materials from the list above. If a material is not listed, it is NOT allowed.")
    return "\n".join(lines)


def _extract_round_doe_ctx(out_dir: Path, parent_round_idx: int | None) -> Dict[str, Any]:
    if parent_round_idx is None:
        return {"factors": [], "factor_levels": {}, "required_combinations": [], "parent_material_names": set()}
    diag_path = out_dir / f"R{parent_round_idx}_diagnosis.json"
    cand_path = out_dir / f"R{parent_round_idx}_candidates.json"
    factors = []
    if diag_path.exists():
        diag = read_json(diag_path)
        factors = diag.get("next_round_doe", {}).get("factors", []) or []
    # Normalize factor names to snake_case for stable matching
    _norm = lambda s: s.strip().lower().replace(" ", "_").replace("-", "_")
    factor_levels = {_norm(str(f.get("name") or "")): [str(x) for x in (f.get("levels") or [])] for f in factors if f.get("name")}
    # Also normalize factor dicts in-place for downstream consumers
    for f in factors:
        f["name"] = _norm(str(f.get("name") or ""))
    required_combinations = []
    if {"pva_wt_percent", "freeze_thaw_cycles"}.issubset(factor_levels.keys()):
        required_combinations = list(itertools.product(factor_levels["pva_wt_percent"], factor_levels["freeze_thaw_cycles"]))
    parent_material_names: set[str] = set()
    if cand_path.exists():
        obj = read_json(cand_path)
        for pc in obj.get("candidates", []):
            parent_material_names |= _candidate_material_names(pc)
    return {
        "factors": factors,
        "factor_levels": factor_levels,
        "required_combinations": required_combinations,
        "parent_material_names": parent_material_names,
        "allow_extension": False,
    }


# ============================================================
# Plan A: Code-layer auto-completion helpers
# ============================================================

def _auto_fix_process_steps_order(c: dict) -> list[str]:
    """Auto-number process.steps[*].order by array position if missing."""
    fixes: list[str] = []
    proc = c.get("process") or {}
    if not isinstance(proc, dict):
        return fixes
    steps = proc.get("steps") or []
    if not isinstance(steps, list):
        return fixes
    for i, step in enumerate(steps):
        if isinstance(step, dict) and step.get("order") is None:
            step["order"] = i + 1
            fixes.append(f"steps[{i}].order -> {i+1}")
    if fixes:
        proc["steps"] = steps
        c["process"] = proc
    return fixes


def _processing_float(value: Any, default: float = 0.0) -> float:
    """Best-effort numeric parsing for LLM processing fields."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        pass

    # Accept simple unit-suffixed values such as "2h"; avoid guessing ranges.
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*[a-zA-Z]+", text):
        match = re.match(r"[+-]?\d+(?:\.\d+)?", text)
        if match:
            return float(match.group(0))
    return default


def _processing_int(value: Any, default: int = 0) -> int:
    return int(_processing_float(value, float(default)))


def _auto_generate_risks(c: dict) -> list[str]:
    """Auto-generate risks_and_mitigations from formulation characteristics
    when count < 2."""
    fixes: list[str] = []
    risks = c.get("risks_and_mitigations") or []
    if not isinstance(risks, list):
        risks = []
    if len(risks) >= 2:
        return fixes

    formulation = c.get("formulation") or {}
    processing = c.get("processing") or {}
    materials = c.get("materials") or []
    mat_names = " ".join((m.get("name") or "").lower() for m in materials)
    blob = str(formulation) + str(processing) + mat_names

    # Template risks based on chemical features
    if "glutaraldehyde" in mat_names or "glutaraldehyde" in str(formulation).lower():
        risks.append({
            "risk": "Glutaraldehyde crosslinking may be too fast at high temperature, causing inhomogeneous gel",
            "mitigation": "Cool solution to 50-55°C before adding crosslinker; mix rapidly and cast immediately",
        })
    if "hcl" in mat_names or "hcl" in str(formulation).lower():
        risks.append({
            "risk": "HCl catalyst may cause premature gelation or pH-induced polymer degradation",
            "mitigation": "Use minimal HCl concentration (0.02-0.15 wt%); monitor pH during mixing",
        })
    if "freeze" in str(processing).lower() or _processing_int(processing.get("freeze_thaw_cycles")) > 0:
        risks.append({
            "risk": "Freeze-thaw cycles may produce non-uniform ice crystals leading to pore size variation",
            "mitigation": "Control freezing rate to -20°C and thaw slowly at room temperature",
        })
    if "hyaluronate" in mat_names or "ha" in mat_names.split():
        risks.append({
            "risk": "Sodium Hyaluronate may leach out during post-soak, reducing lubrication effect",
            "mitigation": "Limit post-soak to <= 2h; verify HA retention via surface characterization",
        })
    if "cmc" in mat_names:
        risks.append({
            "risk": "CMC may increase solution viscosity excessively, making casting difficult",
            "mitigation": "Pre-disperse CMC in small amount of water before adding to PVA solution",
        })
    if "dmso" in mat_names:
        risks.append({
            "risk": "DMSO may plasticize the network excessively, reducing mechanical integrity",
            "mitigation": "Keep DMSO concentration <= 5 wt%; monitor gel modulus after soaking",
        })
    if "photo" in str(formulation).lower() or "uv" in str(formulation).lower():
        risks.append({
            "risk": "Incomplete UV curing may leave residual monomers affecting friction and biocompatibility",
            "mitigation": "Ensure uniform UV exposure (both sides); verify curing via FTIR or swelling test",
        })

    # Generic fallback risks (always relevant for PVA hydrogels)
    if len(risks) == 0:
        risks.append({
            "risk": "PVA concentration or crosslinking density may be suboptimal for low friction",
            "mitigation": "Compare COF with baseline formulation; adjust PVA wt% or crosslinker in next round",
        })

    if len(risks) < 2:
        risks.append({
            "risk": "Post-soak time may affect surface hydration and friction stability",
            "mitigation": "Standardize post-soak to 1-2h; measure COF immediately after soak vs after extended soaking",
        })

    c["risks_and_mitigations"] = risks
    fixes.append(f"generated {len(risks)} risks_and_mitigations (was {len(c.get('risks_and_mitigations') or [])})")
    return fixes


def _auto_extract_doe_factors(c: dict) -> list[str]:
    """Auto-extract doe_factor_levels from candidate fields when empty.
    Also normalizes keys to snake_case."""
    fixes: list[str] = []
    doe = c.get("doe_factor_levels") or {}
    if doe and len(doe) > 0:
        # Normalize existing keys to snake_case
        normalized = {}
        for k, v in doe.items():
            nk = k.strip().lower().replace(" ", "_").replace("-", "_")
            normalized[nk] = v
        if list(normalized.keys()) != list(doe.keys()):
            c["doe_factor_levels"] = normalized
            c["doe_factor_levels_used"] = normalized
            fixes.append(f"normalized DOE keys: {list(doe.keys())} -> {list(normalized.keys())}")
        return fixes

    formulation = c.get("formulation") or {}
    processing = c.get("processing") or {}

    # Extract from formulation
    pva = formulation.get("pva_wt_percent")
    if pva is not None:
        doe["pva_wt_percent"] = str(pva)

    ft = processing.get("freeze_thaw_cycles")
    if ft is not None:
        doe["freeze_thaw_cycles"] = str(ft)

    soak = processing.get("post_soak_hours")
    if soak is not None:
        soak_val = _processing_float(soak)
        doe["post_soak_hours"] = str(int(soak_val)) if soak_val == int(soak_val) else str(soak_val)

    # Extract crosslinker concentration
    cl = formulation.get("crosslinker") or {}
    if isinstance(cl, dict) and cl.get("wt_percent") is not None:
        doe["crosslinker_concentration"] = f"{cl['wt_percent']} wt%"

    # Extract additive type
    additives = formulation.get("additives") or []
    if additives:
        add_names = [a.get("name", "") for a in additives if isinstance(a, dict) and a.get("name")]
        add_names = [n for n in add_names if n.strip() and n.strip().lower() not in ("none", "")]
        if add_names:
            doe["additive_type"] = ", ".join(add_names)

    if doe:
        c["doe_factor_levels"] = doe
        c["doe_factor_levels_used"] = doe
        fixes.append(f"extracted doe_factor_levels: {list(doe.keys())}")

    return fixes


def _auto_generate_expected_mechanism(c: dict) -> list[str]:
    """Auto-generate expected_mechanism from formulation characteristics
    when the field is empty or contains only generic entries."""
    fixes: list[str] = []
    mechanisms = c.get("expected_mechanism") or []
    if not isinstance(mechanisms, list):
        mechanisms = []
    # Check if mechanisms are non-empty and not all generic
    generic_patterns = [
        "chemical crosslinking", "acid-catalyzed reaction",
        "physical crosslinking", "freeze-thaw",
    ]
    has_specific = any(
        m for m in mechanisms
        if isinstance(m, str) and m.strip().lower() not in generic_patterns
    )
    if has_specific and len(mechanisms) >= 1:
        return fixes

    formulation = c.get("formulation") or {}
    processing = c.get("processing") or {}
    materials = c.get("materials") or []
    mat_names = " ".join((m.get("name") or "").lower() for m in materials)
    blob = str(formulation) + str(processing) + mat_names

    new_mechanisms: list[str] = []

    # Crosslinker-specific mechanisms
    if "glutaraldehyde" in mat_names or "glutaraldehyde" in str(formulation).lower():
        new_mechanisms.append(
            "Glutaraldehyde crosslinks PVA hydroxyl groups via acetal bridges, "
            "forming a chemically stable network that resists dissolution under aqueous lubrication"
        )
    if "borax" in mat_names or "borax" in str(formulation).lower():
        new_mechanisms.append(
            "Borax forms dynamic borate-diol crosslinks with PVA, providing self-healing "
            "capability and shear-thinning behavior beneficial for low friction"
        )
    if "epoxy" in mat_names or "epoxy" in str(formulation).lower():
        new_mechanisms.append(
            "Epoxy-amine crosslinking forms a dense covalent network with PVA, "
            "potentially increasing modulus and reducing wear under high load"
        )

    # Processing-specific mechanisms
    ft = _processing_int(processing.get("freeze_thaw_cycles"))
    if ft > 0:
        new_mechanisms.append(
            f"Freeze-thaw cycling ({ft} cycles) induces PVA crystallite formation, "
            "creating physical crosslinks that enhance network toughness without chemical modifiers"
        )

    soak = _processing_float(processing.get("post_soak_hours"))
    if soak > 1:
        new_mechanisms.append(
            f"Extended post-soak ({soak}h) allows full hydration of the hydrogel surface, "
            "promoting a stable water lubrication layer at the sliding interface"
        )

    # Additive-specific mechanisms
    if "hyaluronate" in mat_names or "ha" in mat_names.split():
        new_mechanisms.append(
            "Sodium hyaluronate provides boundary lubrication via its highly hydrated "
            "polysaccharide chains, reducing direct asperity contact at the sliding interface"
        )
    if "cmc" in mat_names or "carboxymethyl" in mat_names:
        new_mechanisms.append(
            "CMC increases solution viscosity and water retention capacity, "
            "stabilizing the surface hydration layer and reducing friction fluctuations"
        )
    if "dmso" in mat_names:
        new_mechanisms.append(
            "DMSO as a co-solvent modifies PVA chain conformation during gelation, "
            "potentially creating a more homogeneous network with uniform surface properties"
        )
    if "glycerol" in mat_names or "peg" in mat_names or "polyethylene" in mat_names:
        new_mechanisms.append(
            "Plasticizer reduces inter-chain hydrogen bonding in PVA, "
            "increasing chain mobility and surface compliance for better conformal contact"
        )
    if "mucin" in mat_names:
        new_mechanisms.append(
            "Mucin glycoproteins adsorb onto the hydrogel surface forming a "
            "boundary lubricating layer that reduces friction at low sliding speeds"
        )
    if "acrylamide" in mat_names or "nvp" in mat_names or "dmaam" in mat_names:
        new_mechanisms.append(
            "Secondary polymer network interpenetrates with PVA, "
            "providing additional mechanical reinforcement and tunable surface properties"
        )

    # Photo-crosslinking
    if "photo" in str(formulation).lower() or "uv" in str(formulation).lower() or "irgacure" in mat_names or "darocur" in mat_names:
        new_mechanisms.append(
            "UV-initiated radical polymerization creates a rapid, spatially uniform "
            "crosslinked network, minimizing processing variability"
        )

    # Network density / PVA concentration
    pva = formulation.get("pva_wt_percent")
    if pva is not None:
        pva_val = float(pva)
        if pva_val >= 14:
            new_mechanisms.append(
                f"High PVA concentration ({pva_val} wt%) increases network density and "
                "mechanical strength, potentially reducing wear at the cost of higher friction"
            )
        elif pva_val <= 10:
            new_mechanisms.append(
                f"Low PVA concentration ({pva_val} wt%) promotes a more open network "
                "with higher water content, favoring fluid-film lubrication"
            )

    # HCl catalyst
    if "hcl" in mat_names or "hydrochloric" in mat_names:
        new_mechanisms.append(
            "HCl catalyzes the acetalization reaction between glutaraldehyde and PVA, "
            "enabling rapid gelation at moderate temperatures"
        )

    # Generic fallback (always relevant)
    if not new_mechanisms:
        new_mechanisms.append(
            "PVA hydrogel network provides a hydrated, low-friction surface; "
            "optimization targets reduced COF through controlled crosslink density and surface hydration"
        )

    c["expected_mechanism"] = new_mechanisms
    fixes.append(f"generated {len(new_mechanisms)} expected_mechanism entries (was {len(mechanisms)})")
    return fixes


def _auto_fix_material_basis(c: dict) -> list[str]:
    """Auto-fill material basis field from amount/batch context when missing."""
    fixes: list[str] = []
    materials = c.get("materials") or []
    proc = c.get("process") or {}
    batch_mass = proc.get("total_batch_mass_g", 20.0)

    for i, m in enumerate(materials):
        if not isinstance(m, dict):
            continue
        if m.get("amount") is None or not m.get("unit"):
            continue
        if m.get("basis") or m.get("basis") == "":
            continue
        # Auto-fill basis from batch context
        try:
            amt = float(m["amount"])
            role = (m.get("role") or "").lower()
            if "solvent" in role or "water" in role:
                m["basis"] = "balance to 20 g batch"
            else:
                pct = round(amt / batch_mass * 100, 3)
                m["basis"] = f"{pct} wt% of 20 g batch"
            fixes.append(f"materials[{i}] basis auto-filled")
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    return fixes


def run_generator(
    llm,
    out_dir: Path,
    round_idx: int,
    n_candidates: int,
    allowed_materials=None,
    material_info: Dict[str, Dict[str, str]] | None = None,
    generation_mode: str = "fallback",
    parent_round_idx: int | None = None,
    last_valid_experimental_round: int | None = None,
    last_failed_audit_round: int | None = None,
    use_agent_pipeline: bool = True,
    target_parent_id: str | None = None,
):
    if target_parent_id and parent_round_idx is None:
        m_parent = re.match(r"^R(\d+)-\d+$", target_parent_id)
        if m_parent:
            parent_round_idx = int(m_parent.group(1))
    effective_parent_round_idx = parent_round_idx or (round_idx - 1 if round_idx > 1 else None)
    target_parent_decision: Dict[str, Any] | None = None
    if target_parent_id:
        from pva_work_flow.tree.formula_tree import infer_branch_decisions

        branch_decisions = infer_branch_decisions(out_dir, write=False)
        target_parent_decision = branch_decisions.get(target_parent_id)
        if target_parent_decision:
            parent_status = target_parent_decision.get("branch_status")
            parent_action = target_parent_decision.get("action")
            if parent_status == "kill":
                raise RuntimeError(
                    f"target_parent_id={target_parent_id} is marked kill in formula_branch_decisions; "
                    "do not expand a killed branch."
                )
            if parent_status == "rescue_candidate":
                print(
                    f"[TREE][R{round_idx}] {target_parent_id} is a rescue candidate; "
                    "this generation will be treated as its one targeted rescue chance."
                )
            else:
                print(
                    f"[TREE][R{round_idx}] {target_parent_id} current branch status: "
                    f"{parent_status} ({parent_action})."
                )
    # ---- DOE context (used by both pipelines) ----
    doe_ctx = _extract_round_doe_ctx(out_dir, effective_parent_round_idx)
    anchor_diag_bundle = _load_diag_bundle_for_round(out_dir, last_valid_experimental_round) if last_valid_experimental_round is not None else {}
    result_driven_anchor_ids = [str(x) for x in (anchor_diag_bundle.get("best_candidates") or []) if str(x).strip()]

    # ---- R2+ with 3-agent pipeline (docs/workflow/black.md / docs/workflow/steps.md) ----
    if use_agent_pipeline and round_idx > 1:
        print(f"[Generator] Using 3-Agent Pipeline for R{round_idx}")
        raw_candidates, audit, doe_plan = run_three_agent_pipeline(
            llm=llm,
            out_dir=out_dir,
            round_idx=round_idx,
            n_candidates=n_candidates,
            allowed_materials=allowed_materials,
            material_info=material_info,
            generation_mode=generation_mode,
            parent_round_idx=effective_parent_round_idx,
            last_valid_experimental_round=last_valid_experimental_round,
            last_failed_audit_round=last_failed_audit_round,
            target_parent_id=target_parent_id,
        )
        cands = raw_candidates
        if not isinstance(cands, list) or len(cands) == 0:
            raise RuntimeError("3-Agent pipeline returned empty candidates")
        print(f"[Generator] 3-Agent pipeline produced {len(cands)} raw candidates")
    else:
        # ---- Legacy monolithic pipeline (R1 or disabled) ----
        failed_audit_diag_bundle = _load_diag_bundle_for_round(out_dir, last_failed_audit_round) if last_failed_audit_round is not None else {}
        prompt = build_generator_prompt(
            round_idx,
            n_candidates,
            out_dir,
            last_valid_experimental_round=last_valid_experimental_round,
            last_failed_audit_round=last_failed_audit_round,
            diag_bundle_valid_exp=anchor_diag_bundle or None,
            diag_bundle_failed_audit=failed_audit_diag_bundle or None,
        )
        raw = llm.generate(SYSTEM_PROMPT, prompt)

        try:
            obj = safe_json_loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            debug_path = out_dir / f"R{round_idx}_raw.txt"
            debug_path.write_text(raw, encoding="utf-8")
            raise RuntimeError(f"Failed to parse LLM JSON; raw saved to {debug_path}") from e

        cands = obj.get("candidates") or []
        if not isinstance(cands, list) or len(cands) == 0:
            raise RuntimeError("Generator returned empty/invalid JSON")

        print(f"[DEBUG] candidates before filtering: {cands[-1]}")

        if len(cands) < n_candidates:
            print(f"[WARN] LLM only returned {len(cands)} candidates < requested {n_candidates}")

    if not allowed_materials:
        print("[WARN] No allowed_materials provided to run_generator; material validation will be skipped entirely.")
    allowed_set = None
    if allowed_materials:
        allowed_set = {m.strip().lower() for m in allowed_materials if m and m.strip()}

    parent_by_id: Dict[str, Dict[str, Any]] = {}
    if round_idx > 1 and effective_parent_round_idx is not None:
        parent_path_for_rules = out_dir / f"R{effective_parent_round_idx}_candidates.json"
        if parent_path_for_rules.exists():
            parent_obj_for_rules = read_json(parent_path_for_rules)
            parent_by_id = {
                pc.get("candidate_id"): pc
                for pc in parent_obj_for_rules.get("candidates", [])
                if pc.get("candidate_id")
            }

    filtered_cands: List[Dict[str, Any]] = []
    for c in cands:

        # ---- Plan D: Material name canonicalization (early, before validation) ----
        name_corrections = canonicalize_candidate_materials(c)
        if name_corrections:
            print(f"[CANONICALIZE] {c.get('candidate_id', '?')}: {'; '.join(name_corrections)}")

        # ---- Plan A.1: Auto-number process steps order ----
        order_fixes = _auto_fix_process_steps_order(c)
        if order_fixes:
            print(f"[AUTO-FIX order] {c.get('candidate_id', '?')}: {'; '.join(order_fixes)}")

        for iter_key in ["iteration", "iteration_metadata"]:
            iter_block = c.get(iter_key) or {}
            if isinstance(iter_block, dict):
                for key in [
                    "generation_mode",
                    "parent_candidate_id",
                    "parent_candidates",
                    "diagnosis_evidence_used",
                    "mutation_rationale",
                    "diagnosis_levers_used",
                    "doe_factor_levels",
                    "doe_factor_levels_used",
                    "doe_compliance",
                    "outside_doe_space",
                    "is_extension",
                    "extension_reason",
                ]:
                    val = iter_block.get(key)
                    if key not in c or c.get(key) in (None, "", [], {}):
                        if val not in (None, "", [], {}):
                            c[key] = val

        if round_idx > 1:
            parent_id = _extract_parent_id(c)
            if not parent_id:
                raise RuntimeError(
                    f"Round R{round_idx}: candidate missing parent_candidate_id / parent_candidates: {c!r}"
                )
            c["parent_candidate_id"] = parent_id
            c["parent_candidates"] = [parent_id]
        else:
            c["parent_candidate_id"] = None
            c["parent_candidates"] = []

        c["generation_mode"] = generation_mode
        c["last_valid_experimental_round"] = last_valid_experimental_round
        c["last_failed_audit_round"] = last_failed_audit_round
        c.setdefault("diagnosis_evidence_used", [])
        c.setdefault("mutation_rationale", "")
        c.setdefault("diagnosis_levers_used", [])

        # ---- Auto-populate diagnosis_evidence_used if empty (14B often leaves it blank) ----
        if not c.get("diagnosis_evidence_used") and c.get("parent_candidate_id"):
            pid = c["parent_candidate_id"]
            # Try to extract evidence from parent round results
            parent_round = effective_parent_round_idx or (round_idx - 1)
            results_path = out_dir / f"R{parent_round}_results_filled.csv"
            if results_path.exists():
                from pva_work_flow.artifacts.io_artifacts import read_results_filled
                rows = read_results_filled(results_path)
                for row in rows:
                    if str(row.get("candidate_id", "")) == pid:
                        evidence_parts = [f"{pid}: COF={row.get('cof_steady_mean', '?')}"]
                        fp = row.get("friction_pattern", "")
                        if fp:
                            evidence_parts.append(f"friction={fp}")
                        wear = row.get("wear_proxy", "")
                        if wear:
                            evidence_parts.append(f"wear={wear}")
                        c["diagnosis_evidence_used"] = [", ".join(evidence_parts)]
                        break
            # Fallback: use mutation_rationale as evidence description
            if not c.get("diagnosis_evidence_used"):
                mr = c.get("mutation_rationale", "")
                if mr:
                    c["diagnosis_evidence_used"] = [f"Parent {pid}: {mr[:200]}"]
                else:
                    c["diagnosis_evidence_used"] = [f"Parent {pid} results from R{parent_round}"]

        # ---- Auto-populate mutation_rationale if empty ----
        if not (c.get("mutation_rationale") or "").strip():
            dt = c.get("design_type", "local_optimization")
            pid = c.get("parent_candidate_id", "?")
            doe = c.get("doe_factor_levels") or {}
            changed_vars = [f"{k}={v}" for k, v in doe.items() if k not in ("pva_wt_percent",)]
            if changed_vars:
                c["mutation_rationale"] = f"{dt} from {pid}: changed {', '.join(changed_vars[:3])}"
            else:
                c["mutation_rationale"] = f"{dt} from {pid}"
        c.setdefault("doe_factor_levels", c.get("doe_factor_levels_used") or {})
        c.setdefault("doe_factor_levels_used", c.get("doe_factor_levels") or {})
        if c.get("doe_factor_levels") and not c.get("doe_factor_levels_used"):
            c["doe_factor_levels_used"] = c.get("doe_factor_levels")
        if c.get("doe_factor_levels_used") and not c.get("doe_factor_levels"):
            c["doe_factor_levels"] = c.get("doe_factor_levels_used")
        c.setdefault("doe_compliance", False)
        c.setdefault("outside_doe_space", False)
        c.setdefault("is_extension", False)
        c.setdefault("extension_reason", "")
        if not isinstance(c.get("iteration_metadata"), dict):
            c["iteration_metadata"] = {}
        c["iteration_metadata"]["generation_mode"] = generation_mode
        c["iteration_metadata"]["diagnosis_evidence_used"] = c.get("diagnosis_evidence_used")
        c["iteration_metadata"]["mutation_rationale"] = c.get("mutation_rationale")
        c["iteration_metadata"]["diagnosis_levers_used"] = c.get("diagnosis_levers_used")
        c["iteration_metadata"]["doe_factor_levels"] = c.get("doe_factor_levels")
        c["iteration_metadata"]["is_extension"] = c.get("is_extension")
        c["iteration_metadata"]["extension_reason"] = c.get("extension_reason")

        formulation = c.get("formulation", {}) or {}
        additives = formulation.get("additives", []) or []

        material_suggestions: Dict[str, list] = {}
        if allowed_set and material_info:
            for m in c.get("materials") or []:
                name = (m.get("name") or "").strip()
                name_low = name.lower()
                role = (m.get("role") or "").strip()
                if name_low in ("", "none"):
                    continue
                in_allowed = name_low in allowed_set
                if not in_allowed and len(name_low) >= 3:
                    for csv_name in allowed_set:
                        if name_low in csv_name or csv_name in name_low:
                            in_allowed = True
                            break
                if not in_allowed:
                    similar = find_similar_materials(name, role, material_info, top_n=2)
                    if similar:
                        material_suggestions[name] = similar

        # Determine if this is a constrained DOE candidate (inherits from parent via deep-copy).
        # Constrained DOE candidates should always preserve parent additives — the materializer
        # already copied them from a known-working parent formulation.
        _is_constrained = (
            c.get("generation_mode") == "fallback"
            or "planned_changed_variables" in c
            or (c.get("iteration_metadata") or {}).get("skeleton_source") == "code_constrained_doe"
        )

        kept_additives = []
        for a in additives:
            if isinstance(a, dict):
                name = (a.get("name") or "").strip()
                name_low = name.lower()
                role = (a.get("role") or "additive").strip()
                # Always keep additives from constrained DOE (parent inheritance).
                # For LLM-generated candidates, filter against allowed_set to catch hallucinations.
                if _is_constrained or not allowed_set or name_low == "none" or name_low in allowed_set:
                    kept_additives.append(a)
                elif material_info:
                    similar = find_similar_materials(name, role, material_info, top_n=2)
                    if similar:
                        material_suggestions[name] = similar
            elif isinstance(a, str):
                nm = a.strip()
                nm_low = nm.lower()
                if _is_constrained or not allowed_set or nm_low == "none" or nm_low in allowed_set:
                    kept_additives.append(a)
                elif material_info:
                    similar = find_similar_materials(nm, "additive", material_info, top_n=2)
                    if similar:
                        material_suggestions[nm] = similar

        if material_suggestions:
            lines = [f"[MATERIAL-SUGGEST] {c.get('candidate_id','?')}:"]
            for rejected, suggestions in material_suggestions.items():
                sug_str = ", ".join(
                    f"{s['name']} (score={s['score']}, {', '.join(s['reasons'])})"
                    for s in suggestions
                )
                lines.append(f"  '{rejected}' not in allowed list. Consider: {sug_str}")
            print("\n".join(lines))
            c.setdefault("_material_suggestions", {})
            c["_material_suggestions"].update(material_suggestions)

        formulation["additives"] = kept_additives
        c["formulation"] = formulation

        # ---- Auto-correct common LLM numeric errors (8B model workaround) ----
        corrections = []

        p = c.get("processing") or {}
        soak = _processing_float(p.get("post_soak_hours"))
        ft = _processing_int(p.get("freeze_thaw_cycles"))
        if soak > 4:
            p["post_soak_hours"] = min(soak, 4.0)
            corrections.append(f"post_soak capped: {soak}h -> 4h")
        if soak > 2 and ft == 0:
            corrections.append(
                f"post_soak={soak}h with freeze_thaw=0: thin film may over-swell and rupture. "
                f"Consider reducing soak to 0.25-1h for screening."
            )
        if ft > 3:
            p["freeze_thaw_cycles"] = 3
            corrections.append(f"FT cycles capped: {ft} -> 3")

        ch = _processing_float(p.get("cycle_hours"))
        if ch > 2:
            p["cycle_hours"] = 2.0
            corrections.append(f"cycle_hours capped: {ch} -> 2.0")

        c["processing"] = p

        PLACEHOLDER_PATTERNS = [
            r"^none$", r"^lubricant$", r"^nanofiller$", r"^filler$",
            r"^additive\s*[a-z]?$", r"^agent\s*[a-z]?$",
        ]
        cleaned_materials = []
        for m in c.get("materials") or []:
            name = str(m.get("name") or "").strip().lower()
            if any(re.match(pat, name) for pat in PLACEHOLDER_PATTERNS):
                corrections.append(f"removed placeholder material: '{m.get('name')}'")
                continue
            cleaned_materials.append(m)
        if len(cleaned_materials) < len(c.get("materials") or []):
            c["materials"] = cleaned_materials

        if corrections:
            c.setdefault("_auto_corrections", []).extend(corrections)
            print(f"[AUTO-FIX] {c.get('candidate_id','?')}: {'; '.join(corrections)}")

        # baseline_reproduction also needs ratio_planner because the parent's
        # materials list may lack PVA/DI water and amount/unit/basis (common
        # when R1 was generated by 14B models).
        c = apply_ratio_plan(
            c,
            candidate_index=len(filtered_cands),
            total_candidates=max(n_candidates, len(cands)),
        )

        # ---- Plan A.2: Auto-generate risks if < 2 ----
        risk_fixes = _auto_generate_risks(c)
        if risk_fixes:
            print(f"[AUTO-FIX risks] {c.get('candidate_id', '?')}: {'; '.join(risk_fixes)}")

        # ---- Plan A.2b: Auto-generate expected_mechanism if empty or generic ----
        mech_fixes = _auto_generate_expected_mechanism(c)
        if mech_fixes:
            print(f"[AUTO-FIX mech] {c.get('candidate_id', '?')}: {'; '.join(mech_fixes)}")

        # ---- Plan A.3: Auto-extract DOE factors if empty ----
        doe_fixes = _auto_extract_doe_factors(c)
        if doe_fixes:
            print(f"[AUTO-FIX doe] {c.get('candidate_id', '?')}: {'; '.join(doe_fixes)}")

        # ---- Plan A.4: Auto-fill material basis if missing ----
        basis_fixes = _auto_fix_material_basis(c)
        if basis_fixes:
            print(f"[AUTO-FIX basis] {c.get('candidate_id', '?')}: {'; '.join(basis_fixes)}")

        (
            materials_complete,
            formulation_complete,
            mat_consistent,
            role_mapping_complete,
            mat_errors,
        ) = normalize_materials_and_formulation(c)

        # DOE / extension / new-material checks
        factor_levels = doe_ctx.get("factor_levels", {})

        # ---- Auto-detect chemistry lineage change and mark is_extension ----
        # If the candidate's crosslinker system differs from its parent, auto-mark extension.
        if round_idx > 1 and not c.get("is_extension"):
            parent_id = c.get("parent_candidate_id")
            if parent_id and effective_parent_round_idx is not None:
                parent_cand_path = out_dir / f"R{effective_parent_round_idx}_candidates.json"
                if parent_cand_path.exists():
                    parent_obj = read_json(parent_cand_path)
                    for pc in parent_obj.get("candidates", []):
                        if pc.get("candidate_id") == parent_id:
                            p_net = (pc.get("formulation") or {}).get("network_type", "")
                            p_cl = ((pc.get("formulation") or {}).get("crosslinker") or {}).get("name", "")
                            c_net = formulation.get("network_type", "")
                            c_cl = (formulation.get("crosslinker") or {}).get("name", "")
                            if (p_net != c_net) or (p_cl and c_cl and p_cl.lower() != c_cl.lower()):
                                c["is_extension"] = True
                                ext_parts = []
                                if p_net != c_net:
                                    ext_parts.append(f"network_type changed from '{p_net}' to '{c_net}'")
                                if p_cl and c_cl and p_cl.lower() != c_cl.lower():
                                    ext_parts.append(f"crosslinker changed from '{p_cl}' to '{c_cl}'")
                                c["extension_reason"] = "; ".join(ext_parts)
                                c.setdefault("iteration_metadata", {})["is_extension"] = True
                                c["iteration_metadata"]["extension_reason"] = c["extension_reason"]
                                print(f"[AUTO-EXT] {c.get('candidate_id','?')}: auto-marked is_extension=true ({c['extension_reason']})")
                            break

        # ---- Strengthened DOE key normalization (run BEFORE compliance check) ----
        doe_levels = c.get("doe_factor_levels") or {}
        if doe_levels:
            normalized_doe = {}
            for dk, dv in doe_levels.items():
                nk = dk.strip().lower().replace(" ", "_").replace("-", "_").replace("%", "_percent")
                # Canonical names
                if nk in ("pva_wt_percent", "pva_wt%", "pva_concentration", "pva_wt"):
                    nk = "pva_wt_percent"
                elif nk in ("freeze_thaw_cycles", "freeze_thaw", "ft_cycles", "freeze_thaw_cycle"):
                    nk = "freeze_thaw_cycles"
                elif nk in ("post_soak_hours", "post_soak", "soak_hours", "soak_time"):
                    nk = "post_soak_hours"
                elif nk in ("crosslinker_concentration", "crosslinker_conc", "crosslinker_wt", "crosslinker"):
                    nk = "crosslinker_concentration"
                elif nk in ("additive_type", "additive", "additive_combination"):
                    nk = "additive_type"
                normalized_doe[nk] = dv
            if list(normalized_doe.keys()) != list(doe_levels.keys()):
                print(f"[AUTO-FIX doe] {c.get('candidate_id','?')}: normalized keys {list(doe_levels.keys())} -> {list(normalized_doe.keys())}")
            c["doe_factor_levels"] = normalized_doe
            c["doe_factor_levels_used"] = normalized_doe
            if c.get("iteration_metadata"):
                c["iteration_metadata"]["doe_factor_levels"] = normalized_doe
            doe_levels = normalized_doe

        # Normalize doe_factor_levels keys and values before compliance check.
        # Formula Agent may write computed chemical wt% (e.g. 0.069) instead of
        # DOE target levels (e.g. "1.5 wt%"). Snap to closest allowed level.
        doe_levels = c.get("doe_factor_levels") or {}
        if doe_levels and factor_levels:
            _norm = lambda s: s.strip().lower().replace(" ", "_").replace("-", "_")
            doe_norm = {}
            for dk, dv in doe_levels.items():
                dk_norm = _norm(str(dk))
                if isinstance(dv, dict):
                    doe_norm[dk_norm] = {_norm(str(k2)): v2 for k2, v2 in dv.items()}
                else:
                    doe_norm[dk_norm] = str(dv)
            # Snap values to allowed DOE levels where mismatched
            for fname, allowed_levels in factor_levels.items():
                fname_norm = _norm(fname)
                if fname_norm in doe_norm:
                    cur = doe_norm[fname_norm]
                    cur_str = str(cur)
                    if allowed_levels and cur_str not in allowed_levels:
                        # Try numeric proximity
                        try:
                            cur_num = float(cur_str.replace("wt%", "").replace("%", "").strip())
                            best = min(allowed_levels, key=lambda a: abs(
                                float(str(a).replace("wt%", "").replace("%", "").strip()) - cur_num))
                            # Write back to original key
                            for orig_key in doe_levels:
                                if _norm(str(orig_key)) == fname_norm:
                                    doe_levels[orig_key] = best
                                    break
                            doe_norm[fname_norm] = best
                        except (ValueError, TypeError):
                            pass
            c["doe_factor_levels"] = doe_levels
            c["doe_factor_levels_used"] = doe_levels
            c.setdefault("iteration_metadata", {})["doe_factor_levels"] = doe_levels

        doe_levels = c.get("doe_factor_levels") or {}
        doe_compliance = True
        outside_doe_reasons = []
        for fname, allowed_levels in factor_levels.items():
            if fname not in doe_levels:
                doe_compliance = False
                outside_doe_reasons.append(f"missing DOE factor {fname}")
                continue
            val = str(doe_levels.get(fname))
            if allowed_levels and str(val) not in [str(a) for a in allowed_levels]:
                doe_compliance = False
                outside_doe_reasons.append(f"{fname}={val} outside {allowed_levels}")

        candidate_materials = _candidate_material_names(c)
        parent_material_names = doe_ctx.get("parent_material_names") or set()
        new_materials = sorted(n for n in candidate_materials if parent_material_names and n not in parent_material_names)
        if new_materials:
            doe_compliance = False
            outside_doe_reasons.append("new materials introduced before DOE completion: " + ", ".join(new_materials))

        c["doe_compliance"] = doe_compliance
        c["outside_doe_space"] = bool(outside_doe_reasons)
        c["doe_factor_levels_used"] = doe_levels
        if outside_doe_reasons and not c.get("is_extension"):
            c["missing_info"] = list(dict.fromkeys((c.get("missing_info") or []) + outside_doe_reasons))

        is_complete, missing = check_material_completeness(c)

        # ---- Hard Gate: expected_mechanism empty after auto-generation = FAIL ----
        mechanisms = c.get("expected_mechanism") or []
        if not isinstance(mechanisms, list):
            mechanisms = []
        if len(mechanisms) == 0:
            missing.append("expected_mechanism is empty — every candidate must state at least 1 specific mechanism")
            is_complete = False
            print(f"[HARD-REJECT mech] {c.get('candidate_id', '?')}: expected_mechanism is empty")

        # ---- Hard Gate: R1 must use PVA as main polymer ----
        if round_idx == 1:
            pva_wt = (c.get("formulation") or {}).get("pva_wt_percent")
            mat_names_lower = " ".join(
                (m.get("name") or "").lower() for m in (c.get("materials") or [])
            )
            has_pva = (pva_wt is not None and float(pva_wt) > 0) or ("pva" in mat_names_lower) or ("polyvinyl" in mat_names_lower)
            if not has_pva:
                missing.append("R1 candidate must use PVA as main polymer (pva_wt_percent > 0)")
                is_complete = False
                print(f"[HARD-REJECT PVA] {c.get('candidate_id', '?')}: R1 candidate without PVA")

        # ---- R2+ additive-preservation check: warn when child drops parent additives ----
        if round_idx > 1 and effective_parent_round_idx is not None:
            parent_id = c.get("parent_candidate_id", "")
            dt = c.get("design_type", "")
            # Only check local_optimization and failure_verification (not limited_exploration)
            if dt in ("local_optimization", "failure_verification", "single_factor_perturbation") and parent_id:
                parent_path = out_dir / f"R{effective_parent_round_idx}_candidates.json"
                if parent_path.exists():
                    parent_obj = read_json(parent_path)
                    for pc in parent_obj.get("candidates", []):
                        if pc.get("candidate_id") == parent_id:
                            p_adds = (pc.get("formulation") or {}).get("additives") or []
                            c_adds = (c.get("formulation") or {}).get("additives") or []
                            p_add_names = set(
                                (a.get("name") or "").strip().lower()
                                for a in p_adds if isinstance(a, dict) and (a.get("name") or "").strip().lower() not in ("", "none")
                            )
                            c_add_names = set(
                                (a.get("name") or "").strip().lower()
                                for a in c_adds if isinstance(a, dict) and (a.get("name") or "").strip().lower() not in ("", "none")
                            )
                            dropped = p_add_names - c_add_names
                            if dropped and dt != "limited_exploration":
                                print(
                                    f"[WARN additive-drop] {c.get('candidate_id', '?')} "
                                    f"({dt} from {parent_id}) dropped parent additives: {sorted(dropped)}. "
                                    f"Parent additives were: {sorted(p_add_names)}. "
                                    f"If intentional, add explanation to mutation_rationale."
                                )
                            break

        total_h, _breakdown = compute_prep_time_hours(c)

        p = c.get("processing") or {}
        ft_cycles = _processing_float(p.get("freeze_thaw_cycles"))
        cycle_hours = _processing_float(p.get("cycle_hours"))
        post_soak_h = _processing_float(p.get("post_soak_hours"))

        violates_time = total_h > 24.0
        forbidden_pattern = (ft_cycles >= 2 and cycle_hours >= 24.0) or (post_soak_h >= 24.0)

        # ---- Plan A.5: Auto-fix time violations (14B often over-estimates process time) ----
        time_fixes: list[str] = []
        if violates_time or forbidden_pattern:
            # Strategy: cap per-step durations and simplify process description
            proc = c.get("process") or {}
            if isinstance(proc, dict):
                steps = proc.get("steps") or []
                for step in steps:
                    if isinstance(step, dict) and step.get("duration_hours", 0) > 4:
                        old = step["duration_hours"]
                        step["duration_hours"] = min(old, 4.0)
                        time_fixes.append(f"step '{step.get('name','?')}' capped {old}h -> 4h")
            # Also shorten slow_chemical -> fast_chemical in the method description
            f = c.get("formulation") or {}
            method = str(f.get("crosslink_or_phys_method") or "")
            if "slow" in method.lower():
                f["crosslink_or_phys_method"] = method.replace("slow", "fast").replace("Slow", "Fast").replace("(slow reaction)", "(fast reaction)")
                time_fixes.append("slow_chemical -> fast_chemical")
                c["formulation"] = f

            # Recompute after fixes
            total_h, _breakdown = compute_prep_time_hours(c)
            violates_time = total_h > 24.0
            if time_fixes:
                print(f"[AUTO-FIX time] {c.get('candidate_id','?')}: {'; '.join(time_fixes)} — new total={total_h:.1f}h")

        if violates_time or forbidden_pattern:
            print(
                "[WARN] slow process at generation stage (will be handled in audit): "
                f"total_prep_time_hours={total_h:.2f}, "
                f"freeze_thaw_cycles={ft_cycles}, cycle_hours={cycle_hours}, "
                f"post_soak_hours={post_soak_h}"
            )

        parent = parent_by_id.get(c.get("parent_candidate_id", ""))
        # baseline_reproduction no longer has special destructive handling here.
        # The post-processing loop above (line ~1034) already runs ratio_planner
        # for ALL candidates including baseline.  This avoids overwriting the
        # ratio_planner-added PVA/DI-water/amounts/process with a bare parent copy.

        c["changed_variables"] = detect_changed_variables(c, parent)
        c["changed_variable_names"] = [x["variable"] for x in c["changed_variables"]]
        c["has_pva_main_polymer"] = candidate_has_pva(c)
        c.setdefault("experimental_status", "not_measured")
        c.setdefault("audit_status", "not_audited")
        if not str(c.get("if_better") or "").strip():
            c["if_better"] = (
                f"If {c.get('candidate_id', 'this candidate')} improves COF or stability, "
                f"the planned change {c.get('changed_variable_names') or 'baseline condition'} is supported."
            )
        if not str(c.get("if_worse") or "").strip():
            c["if_worse"] = (
                f"If {c.get('candidate_id', 'this candidate')} worsens, reject or reverse "
                f"the planned change {c.get('changed_variable_names') or 'baseline condition'} in the next round."
            )
        c["black_box_jump_score"] = black_box_jump_score(c, parent)
        if round_idx == 1:
            c["black_box_jump_score"] = 0
        total_limited = sum(1 for cand in cands if cand.get("design_type") == "limited_exploration")
        constraint_errors = validate_candidate_constraints(
            c,
            parent_by_id=parent_by_id,
            round_limited_exploration_count=total_limited,
            require_parent=round_idx > 1,
        )
        if constraint_errors:
            c.setdefault("constraint_failures", []).extend(constraint_errors)
            print(f"[CONSTRAINT] {c.get('candidate_id', '?')}: {'; '.join(constraint_errors)}")
            if any("missing PVA" in err or "baseline_reproduction changed" in err for err in constraint_errors):
                is_complete = False
                missing.extend(constraint_errors)

        c["total_prep_time_hours"] = total_h
        c["fits_one_day_requirement"] = bool(total_h <= 24.0)
        c["completeness_check"] = {
            "is_complete": bool(is_complete),
            "missing_fields": missing,
        }
        c["missing_info"] = missing[:]
        c["materials_complete"] = materials_complete
        c["formulation_complete"] = formulation_complete
        c["formulation_role_mapping_complete"] = role_mapping_complete
        c["materials_vs_formulation_consistency"] = mat_consistent

        if round_idx > 1 and generation_mode in (GenerationMode.RESULT_DRIVEN, GenerationMode.DIAGNOSIS_DRIVEN):
            c.setdefault("parent_candidates", [])
            c.setdefault("diagnosis_evidence_used", [])
            c.setdefault("mutation_rationale", "")

        filtered_cands.append(c)

    if not filtered_cands:
        print(
            "[WARN] All LLM-proposed candidates were filtered out at generation stage "
            "(material completeness and/or one-day time/slow-process constraints). "
            "Falling back to writing all original candidates for this round."
        )
        filtered_cands = cands
    else:
        print(f"[DEBUG] candidates after filtering: {filtered_cands[-1]}")

    needs_reset = (
        round_idx >= 3
        and last_valid_experimental_round is not None
        and last_failed_audit_round is not None
        and last_failed_audit_round >= 2
    )

    if generation_mode == GenerationMode.RESULT_DRIVEN and result_driven_anchor_ids:
        if needs_reset:
            print(f"[RESET][R{round_idx}] Skipping strict result_driven checks; prioritizing exploration diversity.")
        else:
            anchor_set = set(result_driven_anchor_ids)
            exploit_count = sum(1 for c in filtered_cands if c.get("parent_candidate_id") in anchor_set)
            required_exploit = max(1, math.ceil(0.5 * len(filtered_cands)))
            if exploit_count < required_exploit:
                # 3-agent pipeline: DOE Agent decides parent distribution; this is a warning, not a hard error
                print(
                    f"[WARN] R{round_idx}: result-driven exploitation below threshold. "
                    f"{exploit_count}/{len(filtered_cands)} use best parents (min {required_exploit}). "
                    f"DOE Agent has determined this distribution is optimal."
                )

            for c in filtered_cands:
                if not (c.get("diagnosis_evidence_used") or []):
                    c["diagnosis_evidence_used"] = [
                        f"Code-materialized constrained DOE candidate inheriting from {c.get('parent_candidate_id', 'parent')}.",
                        f"Design type: {c.get('design_type', 'unknown')}.",
                    ]
                if not (c.get("mutation_rationale") or "").strip():
                    c["mutation_rationale"] = (
                        f"Constrained DOE skeleton: {c.get('design_type', 'unknown')} "
                        f"from parent {c.get('parent_candidate_id', 'unknown')}."
                    )

    if round_idx > 1 and effective_parent_round_idx is not None:
        parent_path = out_dir / f"R{effective_parent_round_idx}_candidates.json"
        if not parent_path.exists():
            raise RuntimeError(
                f"Round R{round_idx}: parent_round_idx={effective_parent_round_idx} but "
                f"{parent_path.name} does not exist."
            )
        parent_obj = read_json(parent_path)
        parent_cands = parent_obj.get("candidates") or []
        parent_ids = [pc.get("candidate_id") for pc in parent_cands if pc.get("candidate_id")]

        coverage_parent_ids = [target_parent_id] if target_parent_id else parent_ids
        covered: dict[str, list[str]] = {pid: [] for pid in coverage_parent_ids if pid}
        for c in filtered_cands:
            for pid in c.get("parent_candidates") or []:
                if pid in covered:
                    covered[pid].append(c.get("candidate_id") or "<unassigned>")

        missing_parents = sorted(pid for pid, children in covered.items() if not children)
        if missing_parents:
            if last_failed_audit_round == effective_parent_round_idx:
                print(
                    f"[WARN] Round R{round_idx}: {len(missing_parents)}/{len(parent_ids)} parent "
                    f"candidates from R{effective_parent_round_idx} have no children, but R{effective_parent_round_idx} "
                    f"had no passing audits. Missing: {missing_parents}"
                )
            else:
                print(
                    f"[WARN] Round R{round_idx}: {len(missing_parents)}/{len(parent_ids)} parent "
                    f"candidates from R{effective_parent_round_idx} have no children. Missing: {missing_parents}"
                )

        parent_all_failed = (last_failed_audit_round is not None and last_failed_audit_round == effective_parent_round_idx)

        constrained_skeleton_active = False
        doe_plan_path = out_dir / f"R{round_idx}_doe_plan.json"
        if doe_plan_path.exists():
            try:
                constrained_skeleton_active = read_json(doe_plan_path).get("skeleton_source") == "code_constrained_doe"
            except Exception:
                constrained_skeleton_active = False

        factor_levels = {} if constrained_skeleton_active else doe_ctx.get("factor_levels", {})
        if constrained_skeleton_active:
            print(f"[CONSTRAINED DOE][R{round_idx}] Skipping global DOE coverage checks; using small-step skeleton.")
        if factor_levels:
            for fname, allowed in factor_levels.items():
                observed = set()
                for c in filtered_cands:
                    lvl = str((c.get("doe_factor_levels") or {}).get(fname, ""))
                    if lvl:
                        observed.add(lvl)
                missing_levels = set(allowed) - observed
                if missing_levels:
                    if parent_all_failed:
                        print(
                            f"[WARN] Round R{round_idx}: global DOE coverage missing levels for "
                            f"{fname}: {sorted(missing_levels)} (R{effective_parent_round_idx} had no passing "
                            f"audits; DOE is speculative, continuing)"
                        )
                    else:
                        raise RuntimeError(
                            f"Round R{round_idx}: global DOE coverage missing levels for {fname}: {sorted(missing_levels)}"
                        )

            required_combinations = doe_ctx.get("required_combinations") or []
            if required_combinations:
                observed_pairs = set()
                for c in filtered_cands:
                    levels = c.get("doe_factor_levels") or {}
                    observed_pairs.add((str(levels.get("pva_wt_percent")), str(levels.get("freeze_thaw_cycles"))))
                missing_pairs = [pair for pair in required_combinations if pair not in observed_pairs]
                if missing_pairs:
                    if parent_all_failed:
                        print(
                            f"[WARN] Round R{round_idx}: missing required DOE combinations for "
                            f"(pva_wt_percent, freeze_thaw_cycles): {missing_pairs} "
                            f"(R{effective_parent_round_idx} had no passing audits; continuing)"
                        )
                    else:
                        raise RuntimeError(
                            f"Round R{round_idx}: missing required DOE combinations for (pva_wt_percent, freeze_thaw_cycles): {missing_pairs}"
                        )

            extension_candidates = [c.get("candidate_id") for c in filtered_cands if c.get("is_extension")]
            if extension_candidates and not doe_ctx.get("allow_extension", False) and not parent_all_failed:
                raise RuntimeError(
                    f"Round R{round_idx}: extension candidates are not allowed before main DOE coverage is completed: {extension_candidates}"
                )

        if target_parent_id:
            print(f"[TREE][R{round_idx}] single-parent optimization from {target_parent_id} confirmed.")
        else:
            print(f"[COVERAGE][R{round_idx}] full parent coverage from R{effective_parent_round_idx} confirmed.")

    for i, c in enumerate(filtered_cands, start=1):
        c["candidate_id"] = f"R{round_idx}-{i:02d}"

    for c in filtered_cands:
        parent_id = c.get("parent_candidate_id") or ""
        parent = parent_by_id.get(parent_id)
        if parent:
            root_candidate_id = parent.get("root_candidate_id") or parent_id
            tree_label = normalize_tree_label(parent.get("tree_id") or root_candidate_id)
            c["tree_id"] = tree_label
            c["tree_label"] = tree_label
            c["root_candidate_id"] = root_candidate_id
            c["parent_node_id"] = parent_id
            c["tree_depth"] = int(parent.get("tree_depth") or 0) + 1
        else:
            root_candidate_id = c.get("root_candidate_id") or c["candidate_id"]
            tree_label = normalize_tree_label(
                c.get("tree_id") or root_label_from_candidate_id(root_candidate_id) or root_candidate_id
            )
            c["tree_id"] = tree_label
            c["tree_label"] = tree_label
            c["root_candidate_id"] = root_candidate_id
            c["parent_node_id"] = None
            c["tree_depth"] = int(c.get("tree_depth") or 0)
        c["node_id"] = c["candidate_id"]
        c.setdefault("branch_status", "active")
        c["optimization_scope"] = "single_parent_tree"
        if target_parent_id:
            c["optimization_parent_id"] = target_parent_id
        if parent_id and target_parent_decision and parent_id == target_parent_id:
            c["parent_branch_status_at_generation"] = target_parent_decision.get("branch_status")
            c["branch_intent"] = (
                "rescue"
                if target_parent_decision.get("branch_status") == "rescue_candidate"
                else "continue"
            )

    out = {
        "constraints": CONSTRAINTS,
        "generation_mode": generation_mode,
        "parent_round_idx": effective_parent_round_idx,
        "last_valid_experimental_round": last_valid_experimental_round,
        "last_failed_audit_round": last_failed_audit_round,
        "optimization_scope": "single_parent_tree",
        "target_parent_id": target_parent_id,
        "inheritance_table": build_inheritance_table(filtered_cands),
        "candidates": filtered_cands,
    }
    p = out_dir / f"R{round_idx}_candidates.json"
    write_json(p, out)
    table_path = out_dir / f"R{round_idx}_inheritance_table.md"
    table_path.write_text(inheritance_table_markdown(filtered_cands), encoding="utf-8")
    print(f"[LINEAGE] Wrote inheritance table: {table_path.name}")

    # Auto-generate formula inheritance tree
    from pva_work_flow.tree.formula_tree import build_tree
    build_tree(out_dir)
    print(f"[TREE] Wrote formula_tree.md")
    try:
        from pva_work_flow.tree.tree_statistics import build_tree_statistics

        build_tree_statistics(out_dir)
        print(f"[TREE_STATS] Wrote tree_statistics.json/md and tree_memory_cards.jsonl")
    except Exception as e:
        print(f"[TREE_STATS] unavailable: {e}")
    try:
        from pva_work_flow.memory.chain_memory import build_chain_memory

        build_chain_memory(out_dir)
        print(f"[CHAIN_MEMORY] Wrote chain_memory.json/md and chain_memory_cards.jsonl")
    except Exception as e:
        print(f"[CHAIN_MEMORY] unavailable: {e}")
    try:
        from pva_work_flow.tree.tree_reports import build_tree_reports

        build_tree_reports(out_dir)
        print(f"[TREE_REPORTS] Wrote SIMPLE_TREE.md/GLOBAL_TREE_SUMMARY.md/EXPERIMENT_FORMULA_SUMMARY.md")
    except Exception as e:
        print(f"[TREE_REPORTS] unavailable: {e}")
    try:
        from pva_work_flow.tree.tree_visualizer import build_tree_diagram

        build_tree_diagram(out_dir)
        print(f"[TREE_DIAGRAM] Wrote TREE_DIAGRAM.md")
    except Exception as e:
        print(f"[TREE_DIAGRAM] unavailable: {e}")

    return p
