# Project Change Tree

本文档用树形图总结当前这轮对项目做过的关键改动。核心目标是让项目从“LLM 自由生成材料配方”收缩为“代码硬约束下的 PVA 小步实验规划系统”。

```text
PVA post-process project
|
+-- 1. Project positioning
|   |
|   +-- Before
|   |   +-- LLM reads experiment history
|   |   +-- LLM freely proposes next-round materials and formulas
|   |   +-- High risk: black-box jumps, too many changed variables, weak lineage
|   |
|   +-- After
|       +-- LLM processes data and explains decisions
|       +-- Code builds constrained DOE skeletons
|       +-- Code copies parent formulas and applies only allowed changes
|       +-- Goal: traceable, small-step PVA hydrogel experiment planning
|       +-- Current study mode: about 10 initial roots, then single-parent tree expansion
|
+-- 2. New constrained planning path
|   |
|   +-- constrained_doe.py
|   |   +-- Builds R2+ DOE skeletons by code
|   |   +-- Can lock planning to one parent node via target_parent_id
|   |   +-- Parent node may come from an older round inferred from IDs such as R1-07
|   |   +-- Default max candidates: 6 in code, usually 3-4 in tree-mode CLI use
|   |   +-- Default design mix:
|   |   |   +-- 1 baseline_reproduction
|   |   |   +-- 2 single_factor_perturbation
|   |   |   +-- 1 local_optimization
|   |   +-- limited_exploration disabled by default
|   |   +-- Avoids using pva_wt_percent as fallback when a process variable can be changed
|   |
|   +-- pipeline_agents.py
|       +-- R2+ first tries code-generated constrained DOE
|       +-- Passes target_parent_id / parent_round_idx through the pipeline
|       +-- In constrained mode, Formula Agent no longer freely writes formulas
|       +-- Calls formula_materializer.py to materialize formulas:
|           +-- deepcopy parent candidate
|           +-- assign new candidate_id
|           +-- keep parent_candidate_id
|           +-- apply only variables_changed
|           +-- preserve if_better / if_worse
|
+-- 2.5 Main path simplification
|   |
|   +-- formula_materializer.py
|   |   +-- Extracted from pipeline_agents.py
|   |   +-- Small Interface: materialize_constrained_candidates()
|   |   +-- Deep Implementation: parent lookup, copy, metadata, variable application
|   |
|   +-- 代码精简树（见本文档附录）
|
+-- 3. Hard-rule enforcement
|   |
|   +-- candidate_rules.py
|   |   +-- Checks PVA main polymer
|   |   +-- Checks valid parent_candidate_id
|   |   +-- Detects changed_variables from parent vs child
|   |   +-- Uses semantic design variables only
|   |   |   +-- formulation variables count
|   |   |   +-- processing variables count
|   |   |   +-- material amount/unit/basis from ratio_planner do not count as extra changes
|   |   +-- Checks baseline_reproduction has zero changes
|   |   +-- Checks single_factor_perturbation has exactly 1 change
|   |   +-- Checks local_optimization has max 2 changes
|   |   +-- Checks limited_exploration max 1 per round
|   |   +-- Computes black_box_jump_score
|   |   +-- Builds R{N}_inheritance_table.md
|   |   +-- Adds tree_id and branch_status to inheritance tables
|   |
|   +-- rule_checker.py
|   |   +-- Uses changed_variable_names from candidate_rules
|   |   +-- Enforces design-type variable limits
|   |   +-- Enforces limited_exploration cap
|   |
|   +-- audit.py
|       +-- Recomputes changed_variables
|       +-- Writes audit_status separately from experimental_status
|       +-- Records black_box_jump_score
|       +-- Exports lineage table each round
|
+-- 4. Failure/status separation
|   |
|   +-- experiment_state.py
|   |   +-- Adds changed_variables
|   |   +-- Adds fixed_variables
|   |   +-- Adds audit_status
|   |   +-- Adds experimental_status
|   |   +-- Adds wet_experiment_completed
|   |
|   +-- pipeline/audit prompts and checks
|       +-- Audit failure = invalid/incomplete record or rule violation
|       +-- Experimental failure = wet-lab or measured performance failure
|       +-- A candidate with measurable COF must not be called gelation failure only because audit failed
|
+-- 5. Defaults lowered for 14B model
|   |
|   +-- cli.py
|   |   +-- --n_candidates default: 4
|   |   +-- --n_select default: 4
|   |
|   +-- pva_vllm.sh
|       +-- N_CANDIDATES default: 4
|       +-- N_SELECT default: 4
|
+-- 6. Per-round output artifacts
|   |
|   +-- R{N}_candidates.json
|   |   +-- Full candidate records
|   |   +-- Parent linkage
|   |   +-- Tree metadata: tree_id, node_id, parent_node_id, tree_depth, branch_status
|   |   +-- changed_variables
|   |   +-- audit_status / experimental_status
|   |
|   +-- R{N}_doe_plan.json
|   |   +-- constrained skeleton when skeleton_source=code_constrained_doe
|   |
|   +-- R{N}_inheritance_table.md
|   |   +-- Main human-readable lineage table
|   |   +-- candidate_id
|   |   +-- tree_id
|   |   +-- branch_status
|   |   +-- parent_candidate_id
|   |   +-- design_type
|   |   +-- changed_variables
|   |   +-- if_better
|   |   +-- if_worse
|   |   +-- black_box_jump_score
|   |
|   +-- R{N}_doe.csv
|   +-- R{N}_results_template.csv
|   +-- R{N}_results_filled.csv
|   +-- R{N}_diagnosis.json
|   |
|   +-- formula_tree.md
|       +-- Cross-round tree view
|       +-- Shows branch status and dCOF when results are available
|
+-- 6.5 Run workspace usability
|   |
|   +-- artifact_store.py
|   |   +-- RunWorkspace centralizes round artifact paths
|   |   +-- round_status() reports which files exist
|   |   +-- next_action() recommends the next workflow step
|   |   +-- archive_round_outputs() safely moves old generated artifacts
|   |
|   +-- cli.py
|       +-- --status prints run workspace status
|       +-- --sync_results rebuilds results_filled in the same run directory
|       +-- --regenerate_round N --archive_old archives old generated files before regeneration
|       +-- --tree_initial_roots initializes about 10 root formulas for tree-mode studies
|       +-- --target_parent_id expands only one selected formula node
|
+-- 7. Documentation updates
|   |
|   +-- project_description.md
|   |   +-- Updated project positioning
|   |   +-- Added constrained formula materialization
|   |   +-- Added semantic changed_variables explanation
|   |
|   +-- 工作流概述.md
|   |   +-- Updated workflow summary
|   |   +-- Added constrained_doe.py and code-materialized formulas
|   |
|   +-- inner_rules.md
|   |   +-- Added current implementation status
|   |
|   +-- model_iteration_evaluation.md
|   |   +-- Updated evaluation standard from free generation to constrained decision-making
|   |
|   +-- pva_project_difficulty_reduction_codex_plan.md
|   |   +-- Marked MVP difficulty-reduction items as implemented
|   |
|   +-- steps.md
|       +-- Rebuilt as current constrained workflow and black-box jump guard notes
|
+-- 8. Validation status
    |
    +-- python -m compileall cycle/src/pva_work_flow
    |   +-- Passed
    |
    +-- pytest cycle/tests
    |   +-- 6 tests passed
    |
    +-- mock 2-round full workflow
        +-- Passed during development
        +-- R2 used code-generated constrained DOE
        +-- R2 formulas were materialized from parent candidates
        +-- R2 inheritance table showed semantic changed_variables only
```

+-- 9. Phase 2-5 improvements (2026-05-28 second batch)
    |
    +-- Tests (Phase 2)
    |   +-- tests/conftest.py — shared fixtures
    |   +-- tests/test_candidate_rules.py — 35 tests for all 10 rules functions
    |   +-- tests/test_constrained_doe.py — 14 tests for skeleton generation and tree-mode parent targeting
    |   +-- tests/test_formula_materialization.py — 10 tests for code-materialized formulas
    |   +-- Total: 80 tests after formula_tree, tree_statistics, tree_reports, workflow parent COF, tree_naming, and ratio_planner baseline tests
    |
    +-- Module extraction (Phase 3)
    |   +-- formulation_checks.py — extracted from workflow.py (5 functions, ~320 lines)
    |   +-- workflow.py reduced from ~1100 → ~700 lines
    |
    +-- Single-source rules (Phase 5)
    |   +-- constrained_planning_policy.yaml — canonical rule definitions
    |   +-- config.py — added BUDGET dict aligned with policy YAML
    |
    +-- Budget management (Phase 1)
    |   +-- budget_manager.py — 4-stage inference, round shape recommendation
    |   +-- cli.py — prints [BUDGET] log before each round
    |   +-- artifact_store.py — budget-aware next_action() and format_status_report()
    |
    +-- File organization
        +-- docs/ — 17 MD files in 5 subdirectories (overview/workflow/rules/design/architecture)
        +-- scripts/ — pva_vllm.sh, vllm_start.sh
        +-- prompts/ — prompts_agents.yaml, prompts_en.yaml

+-- 10. Experiment notes & error-code system (2026-05-28)
    |
    +-- experiment_errors.yaml — 10 standard error codes (ERROR1–ERROR10)
    +-- experiment_notes.py — manual observation notes per candidate
    +-- constrained_doe.py — parent selection skips mechanically-failed candidates
    +-- workflow.py — diagnosis prompt auto-injects experiment notes
    +-- cli.py — --write_notes_template N, --list_error_codes
    +-- artifact_store.py — status report shows experiment_notes state

+-- 11. Tree-mode optimization support
    |
    +-- cli.py
    |   +-- Added --tree_initial_roots
    |   +-- Added --target_parent_id
    |
    +-- constrained_doe.py
    |   +-- Builds a skeleton around one selected parent node
    |   +-- Supports parent_round_idx inferred from target_parent_id
    |
    +-- pipeline_agents.py / formula_materializer.py
    |   +-- Pass parent_round_idx through audit, DOE, and materialization
    |
    +-- generator.py
    |   +-- Writes tree_id/tree_label (root-*), node_id, parent_node_id, root_candidate_id, tree_depth, branch_status
    |
    +-- candidate_rules.py / formula_tree.py
        +-- Inheritance table includes tree columns
        +-- formula_tree.md renders cross-round tree status and dCOF

    +-- tree_statistics.py
        +-- Aggregates variable effects, rescue success, kill rates, and root-tree ranking
        +-- Writes tree_statistics.json, tree_statistics.md, tree_memory_cards.jsonl
        +-- Supplies cross-tree statistical RAG without mixing parent lineages

    +-- tree_reports.py
        +-- Writes per-tree SIMPLE_TREE.md under each trees/root-* directory
        +-- Writes GLOBAL_TREE_SUMMARY.md and EXPERIMENT_FORMULA_SUMMARY.md outside trees/
        +-- Keeps human-facing experiment review separate from machine-facing statistical RAG

## One-Line Summary

当前项目已经从”让 14B 模型自由推荐下一轮材料配方”改成”代码生成受限 DOE 和父配方继承，LLM 负责实验数据解释、机制说明和下一轮小步建议”；最新主线支持约 10 个初始 root 配方分别展开单父节点优化树，并通过跨树统计 RAG 让后续树学习前面树的变量效果和失败经验。

---

代码精简树详见：[code_simplification_tree.md](code_simplification_tree.md)
