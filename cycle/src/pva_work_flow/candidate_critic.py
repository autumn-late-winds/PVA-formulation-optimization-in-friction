"""
Layer 2.1 — Candidate + Critic (docs/design/hydrogel_agent_optimization_plan.md)

Generates multiple DOE plan candidates, scores each across 8 dimensions,
and selects the best one that passes hard-constraint checks.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .llm_engines import LLM
from .rule_checker import run_all_rule_checks
from .utils import safe_json_loads
from .candidate_rules import changed_variable_names


CRITIC_PROMPT = """\
You are the Critic Agent in a closed-loop PVA hydrogel optimization system.
Your task is NOT to generate new formulas, but to evaluate a candidate DOE plan.

Score the candidate DOE plan across these 8 dimensions (each 1-5):

1. data_usage (1-5): How well does it use the previous round's experimental results?
   - 1 = ignores results entirely. 5 = every design decision cites specific data.
2. traceability (1-5): Does every new formula have a clear parent formula or source conclusion?
   - 1 = no lineage. 5 = every entry has explicit parent_id and rationale.
3. variable_control (1-5): Does each formula change at most 1-2 key variables?
   - 1 = most formulas change 3+ variables. 5 = strict 1-2 variable control.
4. hypothesis_quality (1-5): Are the hypotheses specific and testable?
   - 1 = vague or no hypotheses. 5 = every formula states a falsifiable prediction.
5. doe_structure (1-5): Does the set include baseline, local_opt, single_factor, failure_verify, and controlled exploration?
   - 1 = random collection. 5 = well-structured DOE with all required types.
6. failure_learning (1-5): Does the plan learn from failed formulas?
   - 1 = ignores failures. 5 = explicit failure_verification formulas.
7. material_feasibility (1-5): Are all materials in the allowed list and practically usable?
   - 1 = unauthorized or impractical materials. 5 = all materials validated.
8. low_friction_alignment (1-5): Does every formula serve the low-friction PVA hydrogel goal?
   - 1 = off-target designs. 5 = all formulas directly optimize friction.

Output JSON:
{
  "scores": {
    "data_usage": N, "traceability": N, "variable_control": N,
    "hypothesis_quality": N, "doe_structure": N, "failure_learning": N,
    "material_feasibility": N, "low_friction_alignment": N
  },
  "total_score": N,
  "hard_errors": ["<error description or empty list>"],
  "black_box_jumps": ["<black-box jump description or empty list>"],
  "recommended_action": "accept|minor_revision|major_revision|reject",
  "revision_notes": ["<what to fix>"]
}
"""


def _call_critic(llm: LLM, candidate_doe: dict, audit: dict, round_id: str) -> dict:
    """Call the Critic LLM to score one candidate DOE."""
    payload = {
        "round_id": round_id,
        "audit_summary": {
            "best_candidates": [b.get("candidate_id") for b in audit.get("best_candidates", [])[:3]],
            "failed_count": len(audit.get("failed_candidates", [])),
            "key_levers": [v.get("variable") for v in audit.get("variables_to_optimize", [])[:3]],
        },
        "candidate_doe": candidate_doe,
    }
    user_prompt = json.dumps(payload, ensure_ascii=False, indent=2)
    raw = llm.generate(CRITIC_PROMPT, user_prompt)
    return safe_json_loads(raw)


def critique_candidate(
    llm: LLM,
    candidate_doe: dict,
    audit: dict,
    round_id: str,
    allowed_materials: list[str] | None = None,
) -> dict:
    """Full critique: LLM scoring + hard-rule check."""
    # LLM scoring
    try:
        critic_output = _call_critic(llm, candidate_doe, audit, round_id)
    except (ValueError, json.JSONDecodeError):
        critic_output = {
            "scores": {},
            "total_score": 0,
            "hard_errors": ["critic_llm_parse_failed"],
            "black_box_jumps": [],
            "recommended_action": "reject",
            "revision_notes": ["LLM output unparseable"],
        }

    # Hard-rule check
    rule_result = run_all_rule_checks(
        candidate_doe,
        allowed_materials=allowed_materials or [],
    )

    deterministic_jumps: list[str] = []
    for entry in candidate_doe.get("inheritance_table", candidate_doe.get("formula_lineage_table", [])) or []:
        eid = entry.get("next_id", entry.get("new_formula_id", "?"))
        score = 0
        changed = changed_variable_names(entry.get("changed_variables", entry.get("variables_changed", [])))
        if not entry.get("parent_id") and not entry.get("parent_candidate_id"):
            score += 2
        if len(changed) > 2:
            score += len(changed) - 2
        if entry.get("design_type") == "baseline_reproduction" and changed:
            score += 3
        if entry.get("design_type") != "limited_exploration" and entry.get("new_materials"):
            score += 2 + len(entry.get("new_materials") or [])
        if score >= 4:
            deterministic_jumps.append(f"{eid}: black_box_jump_score={score}")

    # Merge
    hard_errors = list(critic_output.get("hard_errors", []))
    if not rule_result["passed"]:
        hard_errors.extend(rule_result["errors"])
    hard_errors.extend(deterministic_jumps)

    scores = critic_output.get("scores", {})
    total = sum(int(scores.get(d, 0)) for d in [
        "data_usage", "traceability", "variable_control",
        "hypothesis_quality", "doe_structure", "failure_learning",
        "material_feasibility", "low_friction_alignment",
    ])

    return {
        "scores": scores,
        "total_score": total,
        "hard_errors": hard_errors,
        "black_box_jumps": list(critic_output.get("black_box_jumps", [])) + deterministic_jumps,
        "recommended_action": critic_output.get("recommended_action", "reject"),
        "revision_notes": critic_output.get("revision_notes", []),
        "rule_check_passed": rule_result["passed"],
        "rule_check_warnings": rule_result["warnings"],
    }


def select_best_candidate(
    critiques: list[dict],
    candidates: list[dict],
) -> dict | None:
    """Select the best candidate from multiple critiques.

    Rules:
    1. Filter out candidates with hard_errors.
    2. Filter out any with black_box_risk > 3 (checked via rule_check).
    3. Select highest total_score from remaining.
    4. Return None if no candidate passes.
    """
    valid: list[Tuple[int, int, dict]] = []
    for i, (crit, cand) in enumerate(zip(critiques, candidates)):
        if crit.get("hard_errors"):
            continue
        if not crit.get("rule_check_passed", False):
            continue
        score = int(crit.get("total_score", 0))
        valid.append((score, i, cand))

    if not valid:
        return None

    valid.sort(key=lambda x: x[0], reverse=True)
    return valid[0][2]


def generate_and_select_doe(
    llm: LLM,
    audit: dict,
    round_id: str,
    n_candidates: int = 3,
    allowed_materials: list[str] | None = None,
    doe_planning_fn=None,
) -> Tuple[dict | None, list[dict], list[dict]]:
    """Full Layer 2.1 flow: generate N DOE plans → critique each → select best.

    Args:
        llm: LLM instance for both generation and critique.
        audit: structured audit from Audit Agent.
        round_id: e.g. "R2".
        n_candidates: how many DOE plans to generate (default 3).
        allowed_materials: optional allow-list for Rule 8.
        doe_planning_fn: function(llm, audit, round_id, n) -> doe_plan dict.
                         If None, raises ValueError (caller must provide).

    Returns:
        (best_doe, all_does, all_critiques)
    """
    if doe_planning_fn is None:
        raise ValueError("doe_planning_fn is required — provide the DOE planning function")

    # Generate N candidate DOE plans
    candidates: list[dict] = []
    for i in range(n_candidates):
        try:
            plan = doe_planning_fn(llm, audit, round_id, n_candidates=8)
        except Exception as e:
            print(f"[Candidate+Critic] DOE plan {i+1} generation failed: {e}")
            continue
        plan["_candidate_index"] = i
        candidates.append(plan)

    if not candidates:
        raise RuntimeError("All DOE plan generation attempts failed")

    print(f"[Candidate+Critic] Generated {len(candidates)} DOE candidates, running critique...")

    # Critique each
    critiques: list[dict] = []
    for i, cand in enumerate(candidates):
        print(f"  Critiquing candidate {i+1}/{len(candidates)}...")
        crit = critique_candidate(llm, cand, audit, round_id, allowed_materials)
        critiques.append(crit)
        action = crit.get("recommended_action", "?")
        score = crit.get("total_score", 0)
        errors = len(crit.get("hard_errors", []))
        print(f"    score={score}/40, action={action}, hard_errors={errors}")

    # Select best
    best = select_best_candidate(critiques, candidates)
    if best is None:
        # Fallback: pick the one with fewest hard_errors, then highest score
        ranked = sorted(
            enumerate(critiques),
            key=lambda x: (len(x[1].get("hard_errors", [])), -x[1].get("total_score", 0)),
        )
        best = candidates[ranked[0][0]]
        print(f"[Candidate+Critic] No candidate passed all checks; using best-effort fallback")

    best_idx = best.get("_candidate_index", -1)
    print(f"[Candidate+Critic] Selected candidate {best_idx} ({best.get('design_theme', '?')})")

    return best, candidates, critiques
