"""
Three-agent pipeline for closed-loop PVA hydrogel optimization.

Task 1: Audit Agent  — data → structured audit (best/worst/variables)
Task 2: DOE Agent    — audit → inheritance table (no formulas)
Task 3: Formula Agent — inheritance table → complete formulas

Follows docs/workflow/black.md, docs/rules/inner_rules.md, and docs/workflow/steps.md.
"""

from __future__ import annotations

import json
from copy import deepcopy
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from .config import CONSTRAINTS
from .artifact_store import RunWorkspace
from .io_artifacts import read_results_filled
from .llm_engines import LLM
from .utils import read_json, safe_json_loads, _to_float_or_none


# ---- Prompt loading ----
_prompts_agents_path = Path(__file__).parent / "prompts" / "prompts_agents.yaml"
_AGENT_PROMPTS = yaml.safe_load(_prompts_agents_path.read_text(encoding="utf-8"))
AUDIT_AGENT_PROMPT = _AGENT_PROMPTS["AUDIT_AGENT_PROMPT"]
DOE_PLANNING_AGENT_PROMPT = _AGENT_PROMPTS["DOE_PLANNING_AGENT_PROMPT"]
FORMULA_AGENT_PROMPT = _AGENT_PROMPTS["FORMULA_AGENT_PROMPT"]
FORMULA_CONCEPT_PROMPT = _AGENT_PROMPTS.get("FORMULA_CONCEPT_PROMPT", "")
FORMULA_STRUCTURING_PROMPT = _AGENT_PROMPTS.get("FORMULA_STRUCTURING_PROMPT", "")


# ---- Helper: build structured R1 data for audit agent ----
def _build_audit_input(
    out_dir: Path,
    round_idx: int,
    parent_round_idx: int | None = None,
) -> str:
    """Build structured JSON input for the Audit Agent."""
    parent_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    workspace = RunWorkspace(out_dir)
    results_path = workspace.results_path(parent_round)
    cand_path = workspace.candidates_path(parent_round)

    candidates_data: list[dict] = []
    if cand_path.exists():
        obj = read_json(cand_path)
        for c in obj.get("candidates", []):
            f = c.get("formulation", {}) or {}
            p = c.get("processing", {}) or {}
            adds = f.get("additives", []) or []
            cl = f.get("crosslinker") or {}
            ic = f.get("initiator_or_catalyst") or {}
            candidates_data.append({
                "candidate_id": c.get("candidate_id", "?"),
                "formulation": {
                    "pva_wt_percent": f.get("pva_wt_percent"),
                    "network_type": f.get("network_type"),
                    "crosslink_or_phys_method": f.get("crosslink_or_phys_method"),
                    "crosslinker": {"name": cl.get("name"), "wt_percent": cl.get("wt_percent")} if cl else None,
                    "initiator_or_catalyst": {"name": ic.get("name"), "wt_percent": ic.get("wt_percent")} if ic else None,
                    "additives": [{"name": a.get("name"), "role": a.get("role"), "wt_percent": a.get("wt_percent")} for a in adds],
                } if f else {},
                "processing": {
                    "freeze_thaw_cycles": p.get("freeze_thaw_cycles"),
                    "post_soak_hours": p.get("post_soak_hours"),
                } if p else {},
                "notes": c.get("notes", ""),
            })

    results_data: list[dict] = []
    if results_path.exists():
        for r in read_results_filled(results_path):
            results_data.append({
                "candidate_id": r.get("candidate_id", ""),
                "cof_steady_mean": r.get("cof_steady_mean", ""),
                "cof_std": r.get("cof_std", ""),
                "wear_proxy": r.get("wear_proxy", ""),
                "compression_modulus_MPa": r.get("compression_modulus_MPa", ""),
                "friction_pattern": r.get("friction_pattern", ""),
                "plateau_ratio": r.get("plateau_ratio", ""),
                "asymmetry": r.get("asymmetry", ""),
                "cv_amplitude": r.get("cv_amplitude", ""),
                "stick_slip_score": r.get("stick_slip_score", ""),
                "stable_proportion": r.get("stable_proportion", ""),
                "failure_type": r.get("failure_type", ""),
                "notes": r.get("notes", ""),
            })

    # ---- Detect candidates with experimental data vs audit-only status ----
    results_by_id: dict[str, dict] = {}
    for r in results_data:
        rid = r.get("candidate_id", "")
        if rid:
            results_by_id[rid] = r

    # Enrich candidates_data with audit status and data-presence flags
    for cd in candidates_data:
        cid = cd.get("candidate_id", "")
        has_data = False
        has_measurable_cof = False
        if cid in results_by_id:
            rd = results_by_id[cid]
            cof_raw = rd.get("cof_steady_mean", "")
            has_data = bool(cof_raw != "" and cof_raw is not None)
            try:
                cof_val = float(str(cof_raw))
                has_measurable_cof = (cof_val > 0.0001)
            except (ValueError, TypeError):
                has_measurable_cof = False
        cd["has_experimental_data"] = has_data
        cd["has_measurable_cof"] = has_measurable_cof
        cd["audit_note"] = (
            "HAS MEASURABLE COF DATA — gelation SUCCEEDED regardless of JSON audit status"
            if has_measurable_cof else
            "HAS experimental data but COF may be anomalous"
            if has_data else
            "NO experimental data collected for this candidate"
        )

    payload = {
        "round": f"R{parent_round}",
        "constraints": CONSTRAINTS,
        "candidates": candidates_data,
        "results": results_data,
        "CRITICAL": (
            "AUDIT STATUS (PASS/FAIL in candidates JSON) IS UNRELATED TO EXPERIMENTAL OUTCOME. "
            "A candidate can FAIL audit (incomplete JSON fields) but still have valid experimental data. "
            "A candidate can PASS audit but have poor experimental results. "
            "If a candidate has has_measurable_cof=true, the gel FORMED SUCCESSFULLY — "
            "do NOT characterize it as 'gelation failure' regardless of its audit status. "
            "Classify failures based on EXPERIMENTAL DATA (COF, friction_pattern, wear, modulus), "
            "NOT based on whether JSON fields were complete."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---- docs/rules/inner_rules.md gate ----
def _check_inner_rules(entry: dict, audit: dict) -> Tuple[bool, list[str]]:
    """Apply docs/rules/inner_rules.md 4-condition gate. Returns (pass, reasons)."""
    failures: list[str] = []

    # Rule 1: clear parent or source
    parent = entry.get("parent_id", "")
    if not parent or parent == "?":
        if entry.get("design_type") != "limited_exploration":
            failures.append("rule1: no clear parent_id")
        elif not entry.get("design_rationale", "").strip():
            failures.append("rule1: limited_exploration without explicit source justification")

    # Rule 2: only 1-2 key variables changed
    changed = entry.get("variables_changed", [])
    if len(changed) > 2:
        failures.append(f"rule2: {len(changed)} variables changed, max 2 allowed")

    # Rule 3: answers a clear experimental question
    has_question = (
        bool(entry.get("design_rationale", "").strip())
        or bool(entry.get("expected_outcome", "").strip())
    )
    if not has_question:
        failures.append("rule3: no experimental question stated")

    # Rule 4: serves low-friction goal
    rationale_lower = (entry.get("design_rationale", "") + entry.get("expected_outcome", "")).lower()
    if not any(kw in rationale_lower for kw in ("friction", "cof", "wear", "lubric", "stability")):
        failures.append("rule4: design rationale does not reference friction/wear/lubrication goals")

    return len(failures) == 0, failures


# ---- Black-box risk auto-validation ----
def _validate_inheritance_table(table: list[dict], round_idx: int) -> Tuple[list[dict], list[dict]]:
    """Validate inheritance table: reject same-round parent refs, risk>=4, flag risk=3."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for entry in table:
        pid = entry.get("parent_id", "")
        nid = entry.get("next_id", "")

        # ---- IRON RULE: reject same-round parent references ----
        if pid and pid.startswith(f"R{round_idx}-"):
            print(f"[DOE Agent] REJECTED {nid}: parent_id '{pid}' is same-round (must be R{round_idx-1})")
            rejected.append(entry)
            continue

        risk = int(entry.get("black_box_risk", 3))
        if risk >= 4:
            rejected.append(entry)
        else:
            accepted.append(entry)
    return accepted, rejected


# ================================================================
# Task 1: Audit Agent
# ================================================================
def run_audit_agent(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    parent_round_idx: int | None = None,
) -> dict:
    """Step 1 of docs/workflow/steps.md: read R1 data, output structured audit."""
    if round_idx <= 1:
        raise ValueError("Audit Agent requires round_idx >= 2 (needs R1 data)")

    source_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    input_json = _build_audit_input(out_dir, round_idx, parent_round_idx=parent_round_idx)

    # ---- Inject RAG context (Layer 2.2) ----
    rag_context = ""
    try:
        from .experiment_rag import build_rag_context_for_round
        rag_context = build_rag_context_for_round(
            memory_path=out_dir / "experiment_records.jsonl",
        )
        if rag_context:
            print(f"[Audit Agent] Injected RAG context ({len(rag_context)} chars)")
    except Exception as e:
        print(f"[Audit Agent] RAG context unavailable: {e}")

    tree_stats_context = ""
    try:
        from .tree_statistics import build_tree_statistics_context

        tree_stats_context = build_tree_statistics_context(out_dir)
        if tree_stats_context:
            print(f"[Audit Agent] Injected cross-tree statistics ({len(tree_stats_context)} chars)")
    except Exception as e:
        print(f"[Audit Agent] Cross-tree statistics unavailable: {e}")

    chain_memory_context = ""
    try:
        from .chain_memory import build_chain_memory_context

        chain_memory_context = build_chain_memory_context(out_dir)
        if chain_memory_context:
            print(f"[Audit Agent] Injected chain memory ({len(chain_memory_context)} chars)")
    except Exception as e:
        print(f"[Audit Agent] Chain memory unavailable: {e}")

    context_blocks = []
    if rag_context:
        context_blocks.append(rag_context)
    if tree_stats_context:
        context_blocks.append(tree_stats_context)
    if chain_memory_context:
        context_blocks.append(chain_memory_context)
    try:
        from .formulation_rag import build_formulation_rag_context

        formulation_rag_context = build_formulation_rag_context(
            out_dir=out_dir,
            round_idx=round_idx,
            phase="Audit Agent: interpret previous round results and identify literature-supported risks/levers",
        )
        if formulation_rag_context:
            context_blocks.append(formulation_rag_context)
            print(f"[Audit Agent] Injected formulation literature RAG ({len(formulation_rag_context)} chars)")
    except Exception as e:
        print(f"[Audit Agent] formulation literature RAG unavailable: {e}")
    historical_context = "\n\n".join(context_blocks)

    context_text = f"{historical_context}\n\n" if historical_context else ""
    user_prompt = (
        f"Below is the complete R{source_round} experimental data.\n\n"
        f"Analyze it and output ONLY the audit JSON as specified in your instructions.\n\n"
        "When RAG context is provided, use it to support or challenge failure explanations. "
        "Do not treat literature priors as project measurements; cite them only as external evidence.\n\n"
        f"{context_text}"
        f"=== R{source_round} DATA ===\n{input_json}"
    )

    print(f"[Audit Agent] Sending {len(input_json)} chars of R{source_round} data to LLM...")
    raw = llm.generate(AUDIT_AGENT_PROMPT, user_prompt)

    try:
        result = safe_json_loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        debug_path = out_dir / f"R{round_idx}_audit_agent_raw.txt"
        debug_path.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"Audit Agent returned unparseable JSON; saved to {debug_path}") from e

    audit = result.get("audit", result)
    audit.setdefault("round_summary", "")
    audit.setdefault("best_candidates", [])
    audit.setdefault("failed_candidates", [])
    audit.setdefault("effective_variables", [])
    audit.setdefault("risky_variables", [])
    audit.setdefault("variables_to_hold_constant", [])
    audit.setdefault("variables_to_optimize", [])
    audit.setdefault("crosslinker_systems_tested", [])
    audit.setdefault("additive_performance", [])
    audit.setdefault("unanswered_questions", [])

    # ---- Post-audit integrity check: detect "gelation failure" mislabeling ----
    # Cross-reference audit output with actual experimental data
    results_path = RunWorkspace(out_dir).results_path(source_round)
    cof_data: dict[str, float] = {}
    if results_path.exists():
        for row in read_results_filled(results_path):
            cid = row.get("candidate_id", "")
            cof_raw = row.get("cof_steady_mean", "")
            if cid and cof_raw not in ("", None):
                try:
                    cof_data[cid] = float(str(cof_raw))
                except (ValueError, TypeError):
                    pass

    gelation_failure_keywords = ["gelation", "did not gel", "no gel", "failed to gel", "not form"]
    for fc in audit.get("failed_candidates", []):
        cid = fc.get("candidate_id", "")
        category = str(fc.get("failure_category", "")).lower()
        evidence = str(fc.get("evidence", "")).lower()
        combined = category + " " + evidence

        is_gelation_claim = any(kw in combined for kw in gelation_failure_keywords)
        has_measurable_cof = cid in cof_data and cof_data[cid] > 0.0001

        if is_gelation_claim and has_measurable_cof:
            print(
                f"[AUDIT INTEGRITY WARNING] Audit Agent labeled {cid} as gelation-related failure "
                f"('{fc.get('failure_category', '?')}'), but {cid} has measurable COF={cof_data[cid]:.6f}. "
                f"This candidate DID form a gel. The failure_category should reflect the actual "
                f"experimental outcome (e.g. 'high_friction', 'irregular_friction'), not gelation."
            )

    # Save audit for traceability
    audit_path = out_dir / f"R{round_idx}_audit_agent.json"
    from .utils import write_json
    write_json(audit_path, audit)
    print(f"[Audit Agent] Audit saved to {audit_path.name}")

    return audit


# ================================================================
# Task 2: DOE Planning Agent
# ================================================================
def run_doe_planning_agent(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    audit: dict,
    n_candidates: int,
    target_parent_id: str | None = None,
    parent_round_idx: int | None = None,
) -> dict:
    """Step 2 of docs/workflow/steps.md: audit → inheritance table."""
    try:
        from .constrained_doe import build_constrained_doe_skeleton
        doe_plan = build_constrained_doe_skeleton(
            out_dir=out_dir,
            round_idx=round_idx,
            audit=audit,
            requested_count=n_candidates,
            allow_limited_exploration=False,
            target_parent_id=target_parent_id,
            parent_round_idx=parent_round_idx,
        )
        from .utils import write_json
        doe_path = out_dir / f"R{round_idx}_doe_plan.json"
        write_json(doe_path, doe_plan)
        print(
            f"[DOE Agent] Using code-generated constrained DOE skeleton: "
            f"{len(doe_plan.get('inheritance_table', []))} entries, saved to {doe_path.name}"
        )
        return doe_plan
    except Exception as e:
        print(f"[DOE Agent] Constrained DOE skeleton unavailable, falling back to LLM DOE planning: {e}")

    # Load parent formulations for reference
    parent_round = parent_round_idx if parent_round_idx is not None else round_idx - 1
    cand_path = RunWorkspace(out_dir).candidates_path(parent_round)
    parent_formulas = []
    if cand_path.exists():
        obj = read_json(cand_path)
        for c in obj.get("candidates", []):
            f = c.get("formulation", {}) or {}
            p = c.get("processing", {}) or {}
            parent_formulas.append({
                "candidate_id": c.get("candidate_id"),
                "pva_wt_percent": f.get("pva_wt_percent"),
                "network_type": f.get("network_type"),
                "crosslink_or_phys_method": f.get("crosslink_or_phys_method"),
                "freeze_thaw_cycles": p.get("freeze_thaw_cycles"),
                "post_soak_hours": p.get("post_soak_hours"),
            })

    payload = {
        "target_round": f"R{round_idx}",
        "requested_count": n_candidates,
        "audit": audit,
        "parent_formulas_summary": parent_formulas,
        "max_exploration": 1,
    }

    user_prompt = (
        f"Plan the R{round_idx} Design of Experiments.\n\n"
        f"Target: {n_candidates} formulations.\n"
        f"Max limited_exploration: 1.\n\n"
        f"=== INPUT ===\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    print(f"[DOE Agent] Planning R{round_idx} DOE ({n_candidates} candidates)...")
    raw = llm.generate(DOE_PLANNING_AGENT_PROMPT, user_prompt)

    try:
        result = safe_json_loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        debug_path = out_dir / f"R{round_idx}_doe_agent_raw.txt"
        debug_path.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"DOE Agent returned unparseable JSON; saved to {debug_path}") from e

    doe_plan = result.get("doe_plan", result)
    table = doe_plan.get("inheritance_table", [])

    # Apply black-box risk validation + same-round parent rejection
    accepted, rejected = _validate_inheritance_table(table, round_idx)
    if rejected:
        print(f"[DOE Agent] REJECTED {len(rejected)} entries with black_box_risk >= 4:")
        for r in rejected:
            print(f"  - {r.get('next_id', '?')}: risk={r.get('black_box_risk', '?')}")

    # Apply docs/rules/inner_rules.md gate to each accepted entry
    final_table = []
    gated_out = []
    for entry in accepted:
        passed, reasons = _check_inner_rules(entry, audit)
        if passed:
            final_table.append(entry)
        else:
            gated_out.append((entry, reasons))

    if gated_out:
        print(f"[DOE Agent] GATED OUT {len(gated_out)} entries by inner_rules:")
        for entry, reasons in gated_out:
            print(f"  - {entry.get('next_id', '?')}: {'; '.join(reasons)}")

    doe_plan["inheritance_table"] = final_table
    doe_plan["rejected_count"] = len(rejected) + len(gated_out)
    doe_plan["accepted_count"] = len(final_table)

    # ---- Parent diversity check ----
    parent_ids = set(t.get("parent_id", "") for t in final_table)
    if len(parent_ids) < 2 and len(final_table) >= 4:
        print(f"[DOE Agent] WARNING: Only {len(parent_ids)} parent(s) used across {len(final_table)} entries. "
              f"Consider diversifying parents. Parents: {parent_ids}")

    # Save
    doe_path = out_dir / f"R{round_idx}_doe_plan.json"
    from .utils import write_json
    write_json(doe_path, doe_plan)
    print(f"[DOE Agent] Plan: {len(final_table)}/{len(table)} accepted, saved to {doe_path.name}")

    if len(final_table) < 2:
        raise RuntimeError(
            f"DOE Agent produced only {len(final_table)} valid entries. "
            f"Need at least 2 to proceed. Check rejected/gated entries."
        )

    return doe_plan


# ================================================================
# Task 3: Formula Generation Agent (Two-Stage for 14B models)
# ================================================================
def _run_formula_single_stage(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    table: list[dict],
    parent_round_idx: int | None = None,
) -> list[dict]:
    """Original single-stage formula generation (used as fallback or for capable models)."""
    formulation_rag_context = ""
    try:
        from .formulation_rag import build_formulation_rag_context

        formulation_rag_context = build_formulation_rag_context(
            out_dir=out_dir,
            round_idx=round_idx,
            phase="Formula Agent: complete formulations with literature-supported mechanisms and risk mitigations",
        )
        if formulation_rag_context:
            print(f"[Formula Agent] Injected formulation literature RAG ({len(formulation_rag_context)} chars)")
    except Exception as e:
        print(f"[Formula Agent] formulation literature RAG unavailable: {e}")
    chain_memory_context = ""
    try:
        from .chain_memory import build_chain_memory_context

        chain_memory_context = build_chain_memory_context(out_dir)
        if chain_memory_context:
            print(f"[Formula Agent] Injected chain memory ({len(chain_memory_context)} chars)")
    except Exception as e:
        print(f"[Formula Agent] chain memory unavailable: {e}")

    user_prompt = (
        f"Generate complete R{round_idx} experimental formulas.\n\n"
        f"{formulation_rag_context}\n\n"
        f"{chain_memory_context}\n\n"
        f"=== INHERITANCE TABLE (DO NOT ADD VARIABLES BEYOND THIS) ===\n"
        f"{json.dumps(table, ensure_ascii=False, indent=2)}\n\n"
        f"=== CONSTRAINTS ===\n"
        f"load: 10 N, counterface: stainless steel ball, medium: DI water, "
        f"temperature: 25°C, speed: {CONSTRAINTS['speed_mm_s']} mm/s, "
        f"total batch: 20.0 g\n"
    )

    print(f"[Formula Agent] Generating {len(table)} complete formulas...")
    raw = llm.generate(FORMULA_AGENT_PROMPT, user_prompt)

    try:
        result = safe_json_loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        debug_path = out_dir / f"R{round_idx}_formula_agent_raw.txt"
        debug_path.write_text(raw, encoding="utf-8")
        raise RuntimeError(f"Formula Agent returned unparseable JSON; saved to {debug_path}") from e

    return _validate_and_fix_candidates(
        result,
        table,
        out_dir,
        round_idx,
        parent_round_idx=parent_round_idx,
    )


def _run_formula_two_stage(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    table: list[dict],
    parent_round_idx: int | None = None,
) -> list[dict]:
    """Two-stage formula generation: concepts first, then structured completion.

    Stage 1: Generate lightweight concept outlines (all N candidates).
    Stage 2: Batch-process concepts into complete structured formulas.
    """
    # ---- Stage 1: Concept Generation ----
    formulation_rag_context = ""
    try:
        from .formulation_rag import build_formulation_rag_context

        formulation_rag_context = build_formulation_rag_context(
            out_dir=out_dir,
            round_idx=round_idx,
            phase="Formula Agent concept stage: propose literature-supported formula concepts inside the DOE table",
        )
        if formulation_rag_context:
            print(f"[Formula Agent:Stage1] Injected formulation literature RAG ({len(formulation_rag_context)} chars)")
    except Exception as e:
        print(f"[Formula Agent:Stage1] formulation literature RAG unavailable: {e}")
    chain_memory_context = ""
    try:
        from .chain_memory import build_chain_memory_context

        chain_memory_context = build_chain_memory_context(out_dir)
        if chain_memory_context:
            print(f"[Formula Agent:Stage1] Injected chain memory ({len(chain_memory_context)} chars)")
    except Exception as e:
        print(f"[Formula Agent:Stage1] chain memory unavailable: {e}")

    concept_user_prompt = (
        f"Generate lightweight concept outlines for R{round_idx}.\n\n"
        f"Target: {len(table)} formula concepts (ONE per inheritance table row).\n\n"
        f"{formulation_rag_context}\n\n"
        f"{chain_memory_context}\n\n"
        f"=== INHERITANCE TABLE ===\n"
        f"{json.dumps(table, ensure_ascii=False, indent=2)}\n\n"
        f"=== CONSTRAINTS ===\n"
        f"load: 10 N, counterface: stainless steel ball, medium: DI water, "
        f"temperature: 25°C, speed: {CONSTRAINTS['speed_mm_s']} mm/s, "
        f"total batch: 20.0 g\n"
    )

    print(f"[Formula Agent:Stage1] Generating {len(table)} lightweight concepts...")
    concept_raw = llm.generate(
        FORMULA_CONCEPT_PROMPT if FORMULA_CONCEPT_PROMPT else FORMULA_AGENT_PROMPT,
        concept_user_prompt,
    )

    try:
        concept_result = safe_json_loads(concept_raw)
    except (ValueError, json.JSONDecodeError) as e:
        # Fall back to single-stage if concept generation fails
        print(f"[Formula Agent:Stage1] Concept generation failed ({e}), falling back to single-stage")
        return _run_formula_single_stage(
            llm,
            out_dir,
            round_idx,
            table,
            parent_round_idx=parent_round_idx,
        )

    concepts = concept_result.get("candidates", [])
    if not isinstance(concepts, list) or len(concepts) == 0:
        print(f"[Formula Agent:Stage1] No concepts generated, falling back to single-stage")
        return _run_formula_single_stage(llm, out_dir, round_idx, table)

    print(f"[Formula Agent:Stage1] Generated {len(concepts)}/{len(table)} concepts")

    # Force IDs from inheritance table
    table_ids = [entry.get("next_id", "") for entry in table]
    table_parents = [entry.get("parent_id", "") for entry in table]
    for i, c in enumerate(concepts):
        if i < len(table_ids) and table_ids[i]:
            c["candidate_id"] = table_ids[i]
        if i < len(table_parents) and table_parents[i]:
            c["parent_candidate_id"] = table_parents[i]
            c.setdefault("parent_candidates", [table_parents[i]])

    # If concept count is low, try single-stage instead
    if len(concepts) < max(3, len(table) // 2):
        print(f"[Formula Agent:Stage1] Only got {len(concepts)} concepts, falling back to single-stage")
        return _run_formula_single_stage(llm, out_dir, round_idx, table)

    # ---- Stage 2: Structured Completion (batch processing) ----
    if FORMULA_STRUCTURING_PROMPT:
        batch_size = 4  # Process 4 concepts at a time
        completed: list[dict] = []
        for batch_start in range(0, len(concepts), batch_size):
            batch = concepts[batch_start:batch_start + batch_size]
            struct_prompt = (
                f"Complete these R{round_idx} formula concepts into full structured formulas.\n\n"
                f"=== CONCEPTS TO COMPLETE ===\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n\n"
                f"=== CONSTRAINTS ===\n"
                f"load: 10 N, counterface: stainless steel ball, medium: DI water, "
                f"temperature: 25°C, speed: {CONSTRAINTS['speed_mm_s']} mm/s, "
                f"total batch: 20.0 g\n\n"
                f"For each concept, add: materials array with amounts, complete process.steps, "
                f"expected_mechanism, risks_and_mitigations (>=2), and doe_factor_levels."
            )
            batch_raw = llm.generate(FORMULA_STRUCTURING_PROMPT, struct_prompt)
            try:
                batch_result = safe_json_loads(batch_raw)
                batch_cands = batch_result.get("candidates", [batch_result.get("candidate")])
                if isinstance(batch_cands, dict):
                    batch_cands = [batch_cands]
                if isinstance(batch_cands, list):
                    # Merge structured fields back into concepts
                    for bc in batch_cands:
                        cid = bc.get("candidate_id", "")
                        for concept in batch:
                            if concept.get("candidate_id") == cid:
                                concept.update({k: v for k, v in bc.items() if v})
                                break
                    completed.extend(batch)
                else:
                    completed.extend(batch)
            except (ValueError, json.JSONDecodeError):
                # Keep concepts as-is for this batch; auto-fix will handle missing fields
                completed.extend(batch)
            print(f"[Formula Agent:Stage2] Batch {batch_start//batch_size + 1}: "
                  f"{len(batch)} concepts processed -> {len(completed)} total")
        concepts = completed

    return _validate_and_fix_candidates(
        {"candidates": concepts, "self_review": {}},
        table,
        out_dir,
        round_idx,
        parent_round_idx=parent_round_idx,
    )


def _validate_and_fix_candidates(
    result: dict,
    table: list[dict],
    out_dir: Path,
    round_idx: int,
    parent_round_idx: int | None = None,
) -> list[dict]:
    """Common validation and ID-fixing for formula agent output."""
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) == 0:
        raise RuntimeError("Formula Agent returned empty candidates list")
    if len(candidates) > len(table):
        print(f"[Formula Agent] Truncating {len(candidates)} candidates to skeleton size {len(table)}")
        candidates = candidates[:len(table)]

    # ---- Force IDs from inheritance table (prevent LLM re-numbering) ----
    table_ids = [entry.get("next_id", "") for entry in table]
    table_parents = [entry.get("parent_id", "") for entry in table]
    table_by_id = {entry.get("next_id", ""): entry for entry in table if entry.get("next_id")}
    parent_by_id: dict[str, dict] = {}
    parent_round = parent_round_idx
    if parent_round is None:
        for parent_id in table_parents:
            if isinstance(parent_id, str) and parent_id.startswith("R") and "-" in parent_id:
                try:
                    parent_round = int(parent_id.split("-", 1)[0][1:])
                    break
                except ValueError:
                    pass
    if parent_round is None:
        parent_round = round_idx - 1
    parent_path = RunWorkspace(out_dir).candidates_path(parent_round)
    if parent_path.exists():
        parent_obj = read_json(parent_path)
        parent_by_id = {
            pc.get("candidate_id"): pc
            for pc in parent_obj.get("candidates", [])
            if pc.get("candidate_id")
        }
    for i, c in enumerate(candidates):
        if i < len(table_ids) and table_ids[i]:
            expected_id = table_ids[i]
            actual_id = c.get("candidate_id", "")
            if actual_id != expected_id:
                print(f"[Formula Agent] FIX: ID '{actual_id}' -> '{expected_id}'")
                c["candidate_id"] = expected_id
        if i < len(table_parents) and table_parents[i]:
            expected_parent = table_parents[i]
            actual_parent = c.get("parent_candidate_id", "")
            if actual_parent != expected_parent:
                print(f"[Formula Agent] FIX: parent '{actual_parent}' -> '{expected_parent}'")
                c["parent_candidate_id"] = expected_parent
                c["parent_candidates"] = [expected_parent]
                if c.get("iteration_metadata"):
                    c["iteration_metadata"]["parent_candidate_id"] = expected_parent
                    c["iteration_metadata"]["parent_candidates"] = [expected_parent]
        entry = table_by_id.get(c.get("candidate_id", ""))
        if entry:
            c["design_type"] = entry.get("design_type", c.get("design_type"))
            c.setdefault("if_better", entry.get("if_better", ""))
            c.setdefault("if_worse", entry.get("if_worse", ""))
            c.setdefault("expected_outcome", entry.get("expected_outcome", ""))
            if entry.get("variables_changed"):
                c["planned_changed_variables"] = entry.get("variables_changed")

            if c.get("design_type") == "baseline_reproduction":
                parent = parent_by_id.get(c.get("parent_candidate_id"))
                if parent:
                    keep = {
                        "candidate_id": c.get("candidate_id"),
                        "parent_candidate_id": c.get("parent_candidate_id"),
                        "parent_candidates": c.get("parent_candidates"),
                        "design_type": "baseline_reproduction",
                        "if_better": c.get("if_better", ""),
                        "if_worse": c.get("if_worse", ""),
                        "expected_outcome": c.get("expected_outcome", ""),
                        "expected_mechanism": c.get("expected_mechanism", parent.get("expected_mechanism", [])),
                        "risks_and_mitigations": c.get("risks_and_mitigations", parent.get("risks_and_mitigations", [])),
                        "mutation_rationale": c.get("mutation_rationale", "Repeat parent formula exactly."),
                    }
                    copied = deepcopy(parent)
                    copied.update({k: v for k, v in keep.items() if v not in (None, "", [], {})})
                    c.clear()
                    c.update(copied)

    # Self-review check
    review = result.get("self_review", {})
    review_failures = [k for k, v in review.items() if str(v).strip().lower() in ("✗", "false", "no", "fail")]
    if review_failures:
        print(f"[Formula Agent] Self-review flagged issues: {review_failures}")

    # ---- Detect and fix self-round parent references ----
    # Candidates in R{N} must have parents from R{N-1}, not from the same round.
    round_num = round_idx
    for c in candidates:
        pid = c.get("parent_candidate_id", "")
        if pid and pid.startswith(f"R{round_num}-"):
            # Try to find the real parent from the inheritance table
            for entry in table:
                if entry.get("next_id") == c.get("candidate_id"):
                    real_parent = entry.get("parent_id", "")
                    if real_parent and not real_parent.startswith(f"R{round_num}-"):
                        print(f"[Formula Agent] FIX: self-ref parent '{pid}' -> '{real_parent}'")
                        c["parent_candidate_id"] = real_parent
                        c["parent_candidates"] = [real_parent]
                        if c.get("iteration_metadata"):
                            c["iteration_metadata"]["parent_candidate_id"] = real_parent
                            c["iteration_metadata"]["parent_candidates"] = [real_parent]
                    break

    # Validate: no variables beyond inheritance table
    table_var_names: set[str] = set()
    for entry in table:
        for vc in entry.get("variables_changed", []):
            table_var_names.add(vc.get("variable", "").strip().lower())
        for vu in entry.get("variables_unchanged", []):
            table_var_names.add(vu.strip().lower())

    for c in candidates:
        doe_levels = c.get("doe_factor_levels") or c.get("iteration_metadata", {}).get("doe_factor_levels") or {}
        extra_vars = set(doe_levels.keys()) - table_var_names - {"pva_wt_percent", "additive_type",
                                                                  "freeze_thaw_cycles", "post_soak_hours",
                                                                  "crosslinker_concentration"}
        if extra_vars:
            print(f"[Formula Agent] WARNING: {c.get('candidate_id', '?')} has variables not in table: {extra_vars}")

    print(f"[Formula Agent] Generated {len(candidates)} candidates with self-review")
    return candidates


def run_formula_agent(
    llm: LLM, out_dir: Path, round_idx: int, doe_plan: dict,
    use_two_stage: bool = True,
) -> list[dict]:
    """Step 3 of docs/workflow/steps.md: inheritance table → complete formulas.

    When use_two_stage=True (default), splits into:
      Stage 1: Lightweight concept generation (gets all N candidates)
      Stage 2: Structured completion (batch-processes concepts into full formulas)

    Falls back to single-stage if prompts are missing or concept stage fails.
    """
    table = doe_plan.get("inheritance_table", [])

    if doe_plan.get("skeleton_source") == "code_constrained_doe":
        from .formula_materializer import materialize_constrained_candidates
        return materialize_constrained_candidates(
            out_dir,
            round_idx,
            table,
            parent_round_idx=doe_plan.get("parent_round_idx"),
        )

    if use_two_stage and FORMULA_CONCEPT_PROMPT:
        return _run_formula_two_stage(
            llm,
            out_dir,
            round_idx,
            table,
            parent_round_idx=doe_plan.get("parent_round_idx"),
        )
    else:
        return _run_formula_single_stage(
            llm,
            out_dir,
            round_idx,
            table,
            parent_round_idx=doe_plan.get("parent_round_idx"),
        )


# ================================================================
# Orchestration: run the full 3-agent pipeline
# ================================================================
def run_three_agent_pipeline(
    llm: LLM,
    out_dir: Path,
    round_idx: int,
    n_candidates: int,
    allowed_materials=None,
    material_info=None,
    generation_mode: str = "result_driven",
    parent_round_idx: int | None = None,
    last_valid_experimental_round: int | None = None,
    last_failed_audit_round: int | None = None,
    target_parent_id: str | None = None,
) -> Tuple[list[dict], dict, dict]:
    """Full 3-agent pipeline: Audit → DOE Plan → Formulas.

    Returns (candidates, audit, doe_plan) for downstream processing
    (ratio_planner, audit, etc.).
    """
    print(f"\n{'='*60}")
    print(f"  3-Agent Pipeline: R{round_idx} ({n_candidates} candidates)")
    print(f"{'='*60}")

    # ---- Task 1: Audit Agent ----
    print("\n--- Task 1: Audit Agent ---")
    audit = run_audit_agent(llm, out_dir, round_idx, parent_round_idx=parent_round_idx)

    # ---- Task 2: Code-generated constrained DOE skeleton ----
    print(f"\n--- Task 2: Constrained DOE Skeleton (code-generated) ---")
    allowed = []
    try:
        from .utils import load_allowed_materials
        materials_csv = Path(__file__).resolve().parents[2] / "materials" / "materials_en.csv"
        allowed, _ = load_allowed_materials(materials_csv)
    except Exception:
        pass
    doe_plan = run_doe_planning_agent(
        llm,
        out_dir,
        round_idx,
        audit,
        n_candidates,
        target_parent_id=target_parent_id,
        parent_round_idx=parent_round_idx,
    )

    # ---- Layer 1.4: Rule Checker on DOE plan ----
    print("\n--- Rule Checker (Layer 1.4) ---")
    try:
        from .rule_checker import run_all_rule_checks
        rule_result = run_all_rule_checks(doe_plan, allowed_materials=allowed)
        if not rule_result["passed"]:
            print(f"[Rule Checker] {len(rule_result['errors'])} errors:")
            for e in rule_result["errors"][:5]:
                print(f"  - {e}")
        if rule_result["warnings"]:
            for w in rule_result["warnings"]:
                print(f"  [WARN] {w}")
        doe_plan["rule_check"] = rule_result
    except Exception as e:
        print(f"[Rule Checker] Skipped: {e}")

    # ---- Task 3: Formula Generation Agent ----
    print("\n--- Task 3: Formula Generation Agent ---")
    candidates = run_formula_agent(llm, out_dir, round_idx, doe_plan)

    # ---- Layer 1.1: Append to experiment memory ----
    print("\n--- Experiment Memory (Layer 2.2) ---")
    try:
        from .experiment_state import build_experiment_round_from_pipeline
        from .experiment_rag import append_experiment_record
        exp_round = build_experiment_round_from_pipeline(
            round_id=f"R{round_idx}",
            candidates=candidates,
        )
        memory_path = out_dir / "experiment_records.jsonl"
        for rec in exp_round.get("formula_records", []):
            append_experiment_record(rec, memory_path)
        print(f"[Memory] Appended {len(exp_round.get('formula_records', []))} records to {memory_path.name}")
    except Exception as e:
        print(f"[Memory] Skipped: {e}")

    print(f"\n[Pipeline] Complete: {len(candidates)} candidates generated")
    return candidates, audit, doe_plan
