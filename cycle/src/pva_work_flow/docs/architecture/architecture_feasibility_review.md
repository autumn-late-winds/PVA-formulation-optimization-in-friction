# Architecture And Feasibility Review

本文档使用 `improve-codebase-architecture` 和 `grill-with-docs` 的视角，检查当前 PVA 闭环优化项目在继续构建时会遇到的“思路问题”和“便捷性问题”，并给出解决方向。

重点不是评价某一段代码写得好不好，而是回答：

> 如果目标是在 100 个以内配方里完成可解释迭代，这个项目还需要在哪些地方变得更容易运行、更容易理解、更容易维护？

---

## 0. 总体判断

当前项目已经完成了最关键的一次降难度：从“LLM 自由生成材料配方”改成“代码生成受限 DOE skeleton，LLM 负责解释”。这个方向是正确的。

## 0.1 本轮已解决的问题（2026-05-28）

根据本报告的最高优先级建议，当前已经完成第一批便捷性改进：

- 新增 `artifact_store.py`，提供 `RunWorkspace` 作为运行工作区的集中 Interface；
- 新增 `--status`，可以查看每轮候选、原始 CSV、结果、诊断、DOE plan 和继承表是否存在，并给出下一步建议；
- 新增 `--sync_results <run_dir>`，简化“从 Rn/ 和 Rn_compression/ 重建 results_filled”的操作；
- 新增 `--regenerate_round N --archive_old`，可以先归档旧轮次生成物，再安全重新生成该轮；
- 新增 `CONTEXT.md` 中文术语表，统一“受限 DOE 骨架、语义设计变量、继承关系表、运行工作区”等概念；
- 新增 `test_run_workspace_status`，让运行工作区状态判断有基础测试覆盖。
- 新增 `formula_materializer.py`，把受限模式下“复制父配方并只应用允许变量变化”的实现从 `pipeline_agents.py` 抽出，降低 `pipeline_agents.py` 的责任范围。
- 新增 [code_simplification_tree.md](code_simplification_tree.md)，把代码分成主线、辅助、遗留和待拆分四类，避免继续靠”所有文件都要理解”的方式维护项目。
- 2026-05-29 新增树状优化主线：R1 可生成约 10 个 root 配方，R2+ 可用 `--target_parent_id` 锁定单个父配方节点，并输出跨轮 `formula_tree.md`。

因此，本文档中 `RunWorkspace / ArtifactStore + --status` 已从”建议”变为”第一版已实现”。后续仍可继续增强：轮次清单落盘、旧文件 stale 检测、CSV 缓存和更完整的 golden workflow 测试。

## 0.2 第二轮改进（2026-05-28 — 同日完成）

基于本报告 Phase 2/3/5 建议，本轮已完成：

- **Phase 2 关键规则测试**：新增并扩展规则测试（`test_candidate_rules.py`、`test_constrained_doe.py`、`test_formula_materialization.py`、`test_formula_tree.py`、`test_tree_statistics.py`、`test_tree_reports.py`、`test_workflow_parent_cof.py`、`test_tree_naming.py`），覆盖 has_pva、changed_variables 检测、5 种 design_type 约束校验、black_box_jump_score、skeleton 生成、代码物化配方、`target_parent_id` 单父节点约束、历史父轮次读取、continue/rescue/kill 分支判定、真实 parent COF 诊断比较、`root-*` 树编号分层、baseline ratio preservation、跨树目录下的全局统计聚合，以及每棵 root 树/全局树/实验配方步骤报告。当前测试数为 80。
- **Phase 3 模块拆分**：从 `workflow.py` 抽出 `formulation_checks.py`（`compute_prep_time_hours`、`check_material_completeness`、`normalize_materials_and_formulation` 等 5 个函数），消除了 `_candidate_material_names` 和 `_text_blob_for_mechanism` 的伪私有跨模块导入。`workflow.py` 从 ~1100 行降至 ~700 行。
- **Phase 5 规则单一来源**：新增 `constrained_planning_policy.yaml`，定义 design_type 变量限制、black_box 评分规则、PVA 主体系约束、budget 阈值、mandatory fields 等；`config.py` 新增 `BUDGET` dict。
- **实验预算管理**：新增 `budget_manager.py`，实现 4 阶段推断（screening→local_optimization→mechanism_validation→robustness_validation）、实验完成计数、round shape 推荐和预算告警。已集成到 `cli.py`（每轮打印 budget 日志）和 `artifact_store.py`（`next_action()` 和 `format_status_report()` 包含 budget info）。
- **文件整理**：17 个 MD 文档按 5 类分入 `docs/` 子目录，`.sh` 移入 `scripts/`，`.yaml` 移入 `prompts/`；所有交叉引用已更新。
- 新增 `tests/conftest.py` 提供共享 fixtures（`base_parent`, `base_audit`, `tmp_workspace`）。

## 0.3 第三轮改进：实验备注与错误码系统（2026-05-28）

Bruker CSV 无法捕获凝胶破裂、未成胶等关键实验现象。本轮新增：

- **`experiment_errors.yaml`** — 10 种标准错误码（ERROR1 破裂 ~ ERROR10 其他异常），每种含 severity/impact/suggested_action。
- **`experiment_notes.py`** — 手动实验备注读写模块。支持按 candidate 填写 error_codes + free_text。自动注入 diagnosis prompt 影响 LLM 判断。`is_candidate_mechanically_failed()` 供 parent selection 逻辑使用。
- **`constrained_doe.py`** — parent selection 升级：按 COF 升序排列父配方，优先跳过有机物性失败（破裂/未成胶）的候选。
- **`workflow.py`** — diagnosis prompt 自动注入实验备注上下文。
- **`cli.py`** — 新增 `--write_notes_template N`（生成备注模板）、`--list_error_codes`（列出错误码）。`--sync_results` 自动应用已有备注。
- **`artifact_store.py`** — `round_status()` 和 `format_status_report()` 显示备注状态。

但从工程可行性看，下一阶段的主要风险不再是“模型能不能想出配方”，而是：

```text
项目越来越能跑
|
+-- 但运行入口还不够顺手
+-- 轮次状态还不够显式
+-- 大模块仍然承担太多责任
+-- CSV 计算和结果重建还缺增量机制
+-- 测试还不能覆盖真正的闭环规则
+-- 文档、prompt、代码规则仍可能再次分叉
```

如果这些问题不解决，后续最容易出现的情况是：代码能跑，但用户不知道下一步该跑什么；模型输出了文件，但不知道哪些文件是旧的、哪些是新的；实验数据更新了，但诊断和 R2/R3 没有同步更新。

---

## 1. 思路层面的主要问题

### 1.1 项目仍容易滑回“模型自由规划”

**现象**

虽然现在已经有 `constrained_doe.py`，但项目中仍保留旧的 LLM DOE planning、Formula Agent、Candidate Critic、全局 DOE coverage 等路径。它们本身不是错的，但会让项目出现两套心智模型：

```text
旧心智模型：LLM 自由规划大 DOE
新心智模型：代码生成小步继承 skeleton
```

**风险**

- 新用户可能不知道应该信哪个路径。
- 旧的 `R2_doe_plan.json` 可能看起来“更丰富”，但其实不适合 100 个实验内的小步迭代。
- 后续修改 prompt 时，可能又把 LLM 自由度放大。

**建议**

把项目主线明确命名为：

```text
Constrained Round Planner
```

并在 CLI 中把它作为默认路径。旧路径保留为 `--experimental_llm_doe` 或 `--legacy_planner`，默认不启用。

**优先级：高**

---

### 1.2 当前目标还缺“阶段意识”

**现象**

项目已经讨论了 100 个实验预算，但代码现在主要按轮次运行，没有显式判断当前处于：

- 初筛阶段；
- 主变量筛选阶段；
- 局部优化阶段；
- 机制验证阶段；
- 重复性确认阶段。

**风险**

每轮都生成 4 个候选虽然安全，但 R2、R8、R16 的策略不应该完全一样。接近 100 个配方时，系统应该自动减少探索、增加重复和确认。

**建议**

新增一个 `budget_manager.py` 或 `experiment_stage.py`，提供小接口：

```python
infer_stage(completed_unique_formulas, best_candidate_repeats, remaining_budget)
recommend_round_shape(stage, remaining_budget)
```

输出示例：

```json
{
  "stage": "local_optimization",
  "remaining_budget": 72,
  "round_size": 4,
  "allow_limited_exploration": false,
  "required_roles": ["baseline_reproduction", "single_factor_perturbation", "local_optimization"]
}
```

**优先级：高**

---

### 1.3 “失败”仍然需要更细分

**现象**

当前已经分开了 `audit_status` 和 `experimental_status`，这是很重要的改进。但实验失败内部还可以继续细分：

```text
experimental_status
|
+-- measured_good
+-- measured_high_friction
+-- measured_unstable_pattern
+-- measured_high_wear
+-- gelation_failed
+-- sample_broke
+-- data_unusable
+-- missing_data
```

**风险**

如果所有不理想结果都叫 failure，下一轮会很难决定是修配方、修工艺、修测试，还是重测。

**建议**

把 `experimental_status` 变成“粗状态 + failure_mode”的组合：

```json
{
  "experimental_status": "measured",
  "failure_mode": "asymmetric_friction",
  "data_quality": "usable"
}
```

**优先级：中高**

---

## 2. 代码架构层面的主要问题

### 2.1 `generator.py` 太大，Interface 太宽

**观察**

`generator.py` 约 1497 行，承担了 prompt 构造、父配方注入、材料白名单、自动修复、DOE 检查、ratio plan 调用、约束标记、继承表输出等任务。

**问题**

这个 Module 的 Interface 已经不只是 `run_generator()`，而是调用者和维护者必须理解大量隐含顺序：

```text
LLM output
-> canonicalize materials
-> auto-fix process
-> auto-generate risks
-> auto-generate mechanism
-> ratio planner
-> changed_variables
-> constraints
-> DOE coverage
-> lineage table
```

这导致 Locality 不够：修一个候选后处理规则，可能要在一个大函数里找很久。

**建议拆分为深 Module**

```text
generator.py
|
+-- prompt_builder.py
|   +-- build_generator_prompt
|
+-- candidate_normalizer.py
|   +-- canonicalize materials
|   +-- process auto-fixes
|   +-- expected_mechanism / risks fallback
|
+-- candidate_assembly.py
|   +-- apply ratio plan
|   +-- attach metadata
|   +-- fill if_better / if_worse
|
+-- candidate_gate.py
    +-- hard rejects
    +-- constrained DOE exceptions
    +-- lineage table output
```

**收益**

- Interface 更小；
- 每个 Module 更深；
- 单元测试可以直接测 `candidate_normalizer` 和 `candidate_gate`；
- 以后改规则不需要翻 1500 行。

**优先级：高**

---

### 2.2 `pipeline_agents.py` 同时包含 LLM agent 和代码物化逻辑

**观察**

`pipeline_agents.py` 约 820 行，同时包含：

- Audit Agent 输入构造；
- DOE Planning Agent；
- Formula Agent 单阶段/两阶段；
- LLM 输出修复；
- code-materialized formula builder；
- run_three_agent_pipeline 编排。

**问题**

当前主线已经变成“代码物化配方”，但文件名和结构仍然像“三个 LLM agent pipeline”。这会让维护者误以为 Formula Agent 仍是核心。

**建议**

把主线改名或提取为：

```text
round_planner.py
|
+-- audit_previous_round()
+-- build_constrained_plan()
+-- materialize_candidates_from_plan()
+-- append_experiment_memory()
```

LLM 自由 Formula Agent 可以移到：

```text
legacy_llm_formula_agent.py
```

**收益**

- 项目主线更清晰；
- 代码结构符合现在的项目哲学；
- 避免“旧路径”和“新路径”混在一起。

**优先级：高**

---

### 2.3 `workflow.py` 同时处理材料规范化、wetlab、diagnosis、DOE factor

**观察**

`workflow.py` 约 1111 行，包含材料完整性、材料/formulation 同步、wetlab 导出、text-only diagnosis、candidate repairs、DOE factor definitions、diagnosis 调用。

**问题**

这是一个典型的浅 Module：名字叫 workflow，但里面其实有很多独立领域知识。调用者获得的 Leverage 不够明确，维护者需要读完整文件才能知道某个行为在哪里。

**建议**

按领域拆成：

```text
wetlab_export.py
  +-- run_prepare_wetlab

formulation_sync.py
  +-- normalize_materials_and_formulation
  +-- check_material_completeness

diagnosis_runner.py
  +-- run_diagnose
  +-- run_text_only_diagnose

doe_factor_builder.py
  +-- build_structured_doe
  +-- lever_to_doe mapping
```

**收益**

- 每个文件对应一个清楚概念；
- 诊断逻辑和湿实验导出不再互相干扰；
- 更方便写 fixture 测试。

**优先级：中高**

---

### 2.4 缺一个 Run Workspace / Artifact Store

**观察**

现在很多地方直接拼文件名：

```text
R1_candidates.json
R1_results_filled.csv
R2_doe_plan.json
R2_inheritance_table.md
R1/
R1_compression/
```

这些约定散落在 `cli.py`、`workflow.py`、`pipeline_agents.py`、`generator.py`、`audit.py` 里。

**问题**

用户最容易卡在这里：

- 我现在有 R1 CSV，下一步跑什么？
- `--build_results` 的输入目录和 `--out_dir` 是不是同一个？
- 旧的 R2 文件会不会被复用？
- 哪个文件是最新的？
- 如果我只更新了一个 CSV，要不要重跑全部？

**实现状态**

第一版已经实现：`artifact_store.py` 中新增 `RunWorkspace`，集中提供 candidates、results、diagnosis、DOE plan、raw CSV 目录、状态报告和归档接口。

**后续建议**

新增一个深 Module：

```text
artifact_store.py
|
+-- class RunWorkspace
    +-- candidates_path(round)
    +-- results_path(round)
    +-- diagnosis_path(round)
    +-- doe_plan_path(round)
    +-- raw_friction_dir(round)
    +-- raw_compression_dir(round)
    +-- round_manifest(round)
    +-- next_action()
```

再配一个 `R{N}_manifest.json`：

```json
{
  "round": 1,
  "candidates": "present",
  "raw_friction_csv": "present",
  "raw_compression_csv": "present",
  "results_filled": "stale_or_present",
  "diagnosis": "missing",
  "next_recommended_command": "python -m pva_work_flow.cli --mode diagnose --round 1 ..."
}
```

**收益**

- 用户体验会明显变好；
- CLI 可以告诉用户下一步；
- 避免旧文件误用；
- 后续做 GUI 或网页前端也有基础。

**优先级：已完成第一版，后续继续增强**

---

## 3. 运行便捷性问题

### 3.1 缺少 `status` 命令

**问题**

用户最常问的不是“怎么写代码”，而是：

```text
我现在做到哪一步了？
下一步该跑什么？
哪些文件缺失？
哪些文件是旧的？
```

**实现状态**

第一版已经实现：

```bash
python -m pva_work_flow.cli --status --out_dir src\sft_qwen3_14b_out
```

会输出每轮文件状态、是否为 legacy DOE plan，以及推荐下一步操作。

**后续建议**

CLI 增加：

```bash
python -m pva_work_flow.cli --status --out_dir src\sft_qwen3_14b_out
```

输出：

```text
Run workspace: src/sft_qwen3_14b_out

R1:
  candidates: present
  raw friction CSV: 17 files
  compression CSV: 7 files
  results_filled: present
  diagnosis: present
  recommended next: generate R2 with constrained planner

R2:
  candidates: present but stale/legacy
  doe_plan: legacy large DOE, not constrained skeleton
  recommended next: regenerate R2 with --n_candidates 4
```

**优先级：已完成第一版**

---

### 3.2 缺少 `regenerate_round` 的安全语义

**问题**

现在如果旧的 `R2_candidates.json` 存在，生成会跳过。对于你当前这种“旧版 R2 不适合新策略”的情况，用户需要手动判断、删除或换目录。

**实现状态**

第一版已经实现：

```bash
python -m pva_work_flow.cli --regenerate_round 2 --archive_old --out_dir src\sft_qwen3_14b_out
```

它会把旧的 R2 生成物移动到 `archive/R2_YYYYMMDD_HHMMSS/`，再进入单轮生成模式。

**后续建议**

增加更明确的命令：

```bash
python -m pva_work_flow.cli --regenerate_round 2 --archive_old
```

行为：

```text
R2_candidates.json -> archive/R2_candidates_20260528_1500.json
R2_doe_plan.json -> archive/...
重新生成 R2
```

**优先级：已完成第一版**

---

### 3.3 `--build_results` 输入和输出容易混淆

**问题**

当前 `_run_build_results(build_dir, out_dir)` 会从 `build_dir` 找 `Rn/`，但把结果写到 `out_dir`。如果用户传错，很容易出现“读 A 目录，写 B 目录”。

**实现状态**

第一版已经实现：

```bash
python -m pva_work_flow.cli --sync_results src\sft_qwen3_14b_out
```

该命令等价于把输入目录和输出目录都设为同一个运行工作区，适合真实实验 CSV 回填后的常规操作。

**后续建议**

提供一个更贴合实验目录的命令：

```bash
python -m pva_work_flow.cli --sync_results --run_dir src\sft_qwen3_14b_out
```

内部等价于：

```text
build_dir = run_dir
out_dir = run_dir
compression_dir = run_dir/Rn_compression
```

**优先级：已完成第一版**

---

## 4. 加速计算问题

### 4.1 Bruker CSV 解析需要增量缓存

**观察**

R1 已经有 17 个摩擦 CSV，单个文件可以到数 MB。未来 100 个配方，每个 2-3 次重复，CSV 数量可能达到 200-300 个。

**问题**

如果每次 `--build_results` 都重读所有 CSV，会越来越慢。

**建议**

新增缓存：

```text
.pva_cache/
  bruker_metrics/
    file_hash_or_mtime.json
```

缓存内容：

```json
{
  "source_file": "R1/1-1.csv",
  "mtime": "...",
  "size": 4568420,
  "cof_mean": 0.029,
  "wear_proxy": 38.7,
  "friction_pattern": "irregular",
  "plateau_ratio": 0.2379,
  "stable_proportion": 0.386
}
```

只有文件大小或修改时间变化时才重算。

**优先级：高**

---

### 4.2 CSV 解析可以并行

**问题**

每个 CSV 的解析互相独立，适合并行。

**建议**

在 `build_results_from_bruker_csvs()` 内部使用：

```python
concurrent.futures.ProcessPoolExecutor
```

或先用线程池做 I/O 并发。输出顺序按 candidate_id 排序，保证结果稳定。

**优先级：中高**

---

### 4.3 LLM 调用需要缓存和复现键

**问题**

诊断和生成调用 LLM 成本高，而且同一输入重复跑可能得到不同输出。

**建议**

对 LLM 调用建立 cache key：

```text
hash(system_prompt + user_prompt + model_name + temperature)
```

保存：

```text
.pva_cache/llm/{hash}.json
```

调试时可以加：

```bash
--use_llm_cache
--refresh_llm_cache
```

**优先级：中**

---

## 5. 测试问题

### 5.1 现有测试太薄

**观察**

当前 `cycle/tests` 里主要是 `test_imports.py`，约 3000 行以内，更多是导入和基础烟测。

**问题**

项目最重要的逻辑不是“能导入”，而是：

- baseline 是否真的零变化；
- code-materialized formula 是否只改允许变量；
- audit failure 是否不会被当成 experimental failure；
- 旧 R2 是否会被识别为 legacy；
- `R{N}_inheritance_table.md` 是否正确。

**建议新增测试**

```text
tests/test_candidate_rules.py
tests/test_constrained_doe.py
tests/test_formula_materialization.py
tests/test_run_workspace_status.py
tests/fixtures/r1_minimal/
tests/fixtures/r1_with_csv_metrics/
```

**优先级：最高**

---

### 5.2 需要 golden workflow fixture

**建议**

保存一个最小 R1 fixture：

```text
fixtures/golden_run/
  R1_candidates.json
  R1_results_filled.csv
  expected_R2_doe_plan.json
  expected_R2_inheritance_table.md
```

测试目标：

```text
给定固定 R1，生成 R2 skeleton 必须稳定。
```

这样以后改代码不会悄悄改变实验策略。

**优先级：高**

---

## 6. 文档和规则一致性问题

### 6.1 文档、prompt、代码规则有再次分叉风险

**问题**

规则现在存在于多个地方：

- `inner_rules.md`
- `model_iteration_evaluation.md`
- `prompts_agents.yaml`
- `candidate_rules.py`
- `rule_checker.py`
- `audit.py`

这些规则语义相近，但不完全由同一个来源生成。

**建议**

新增一个机器可读规则文件：

```text
rules/constrained_planning_policy.yaml
```

示例：

```yaml
max_candidates_default: 4
limited_exploration:
  enabled_by_default: false
  max_per_round: 1
design_types:
  baseline_reproduction:
    max_changed_variables: 0
  single_factor_perturbation:
    exact_changed_variables: 1
  local_optimization:
    max_changed_variables: 2
semantic_change_detection:
  include:
    - formulation
    - processing
  exclude:
    - material.amount
    - material.unit
    - material.basis
```

代码读取它，文档引用它，prompt 从它生成摘要。

**优先级：中高**

---

## 7. 建议的下一阶段路线图

```text
下一阶段工程路线
|
+-- Phase 1: 运行便捷性
|   +-- 增加 RunWorkspace / ArtifactStore
|   +-- 增加 --status
|   +-- 增加 --sync_results
|   +-- 增加 --regenerate_round --archive_old
|
+-- Phase 2: 关键规则测试
|   +-- test_candidate_rules.py
|   +-- test_constrained_doe.py
|   +-- test_formula_materialization.py
|   +-- golden R1 -> expected R2 fixture
|
+-- Phase 3: 模块变深
|   +-- 拆 generator.py
|   +-- 拆 workflow.py
|   +-- 把 constrained 主线从 pipeline_agents.py 提成 round_planner.py
|
+-- Phase 4: 加速
|   +-- Bruker CSV metrics cache
|   +-- 并行 CSV 解析
|   +-- LLM prompt cache
|
+-- Phase 5: 规则单一来源
    +-- constrained_planning_policy.yaml
    +-- prompt snippets generated from policy
    +-- docs reference policy
```

---

## 8. Top Recommendation

第一版已经完成的是：

```text
RunWorkspace / ArtifactStore + --status
```

理由：

1. 这直接解决用户最真实的痛点：不知道当前做到哪一步、下一步该跑什么。
2. 它能防止旧 R2、旧 diagnosis、旧 results 被误用。
3. 它会把散落在各个文件里的路径约定集中起来，提高 Locality。
4. 后续加速缓存、regenerate、实验预算管理都可以挂在这个 Module 上。

推荐的最小接口：

```python
class RunWorkspace:
    def __init__(self, root: Path): ...
    def round_status(self, round_idx: int) -> dict: ...
    def next_action(self) -> dict: ...
    def candidates_path(self, round_idx: int) -> Path: ...
    def results_path(self, round_idx: int) -> Path: ...
    def archive_round_outputs(self, round_idx: int) -> Path: ...
```

下一步最应该做的是：

```text
关键规则测试 + golden R1 -> expected R2 fixture
```

理由：

1. 当前项目最重要的可靠性来自代码硬规则，因此测试必须覆盖这些规则；
2. 给定同一个 R1，R2 的受限 DOE skeleton 应该稳定，不能因为一次重构悄悄改变策略；
3. 这能保护后续拆分 `generator.py`、`workflow.py`、`pipeline_agents.py` 时不破坏主线行为。

建议优先新增：

```text
tests/test_candidate_rules.py
tests/test_constrained_doe.py
tests/test_formula_materialization.py
tests/fixtures/golden_run/
```
