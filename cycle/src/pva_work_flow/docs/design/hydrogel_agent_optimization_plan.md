# Hydrogel LLM Agent Optimization Plan

## 当前实现状态（2026-05-28）

当前项目已经完成第一层“降低黑盒跳跃”的关键实现：

- 2026-05-29 更新：当前论文主线改为“约 10 个初始 root 配方 + 单父节点配方优化树”。R1 可用 `--tree_initial_roots 10` 生成初始根节点；R2+ 可用 `--target_parent_id` 每次只展开一个父配方节点。
- 在树状优化主线中，父本多样性来自多棵独立 root tree，而不是同一轮 DOE 混合多个父配方。
- 2026-05-29 补充：多棵树之间共享统计知识。`tree_statistics.py` 会把前几棵树的变量效果、kill 频率、rescue 成功率和 root tree 排名转成 `tree_statistics.json/md` 与 `tree_memory_cards.jsonl`，作为后续树的统计 RAG 先验。
- R2+ 默认不再让 LLM 自由生成完整配方，而是由 `constrained_doe.py` 生成受限继承骨架。
- `pipeline_agents.py` 在受限模式下直接复制父配方并只应用 `variables_changed`，将 Formula Writer 的自由度降到最低。
- `candidate_rules.py` 负责父配方检查、baseline 完全复现、语义 `changed_variables` 自动检测、`limited_exploration` 限制和黑盒风险评分。
- `audit.py` 与 `experiment_state.py` 已区分 `audit_status` 和 `experimental_status`。
- 每轮输出 `R{N}_inheritance_table.md`，作为“是否黑盒跳跃”的主要审查依据。

这意味着当前优化路线已经从“多 Agent 自由协作”进一步收缩为“代码硬约束 + LLM 解释补充 + 配方树追踪 + 跨树统计记忆”。后续优化应优先完善预算管理、报告导出、branch_status 判定、统计 RAG 和人工确认机制，而不是增加新材料探索自由度。

---

## 目标

本文件用于指导 Codex 对当前“水凝胶配方与实验流程生成”项目进行优化。当前模型规模约为 14B，属于中小型模型。核心问题不是模型完全不会生成配方，而是模型在多轮迭代时容易表现为黑盒式跳跃：第一轮给出若干配方，观察实验结果后，第二轮又给出另一批看似合理但缺少继承关系和变量控制逻辑的配方。

本优化计划的核心目标是将系统从：

```text
生成式配方助手
```

升级为：

```text
有审计记录、可追踪、可解释的闭环实验设计 Agent
```

也就是说，下一轮配方不能像“凭空冒出来”，而必须能说明：

1. 每个新配方继承自哪个上一轮配方或哪条实验结论。
2. 相比母配方改变了哪些变量。
3. 为什么改变这些变量。
4. 为什么其他变量保持不变。
5. 这个配方要验证什么假设。
6. 如果实验结果变好，说明什么。
7. 如果实验结果变差，说明什么。
8. 这组配方整体如何构成有逻辑的 DOE，而不是随机试配方。

本文件将优化策略分为三个层次：

- 第一层：必须先做，主要解决黑盒问题。
- 第二层：中期增强，主要提高稳定性和历史记忆能力。
- 第三层：后期增强，主要通过 verifier 和偏好训练提升模型本身行为。

---

# 一、总体系统架构建议

建议将当前系统改造成如下闭环流程：

```text
用户输入
  ↓
上一轮配方 + 上一轮实验结果 + 材料约束 + 项目目标
  ↓
Step 1. Structured Parser
  ↓
Step 2. Result Auditor Agent
  ↓
Step 3. DOE Planner Agent
  ↓
Step 4. Rule Checker
  ↓
Step 5. Formula and Protocol Writer Agent
  ↓
Step 6. Self Critic / Black-box Risk Checker
  ↓
最终输出：下一轮配方 + 继承关系表 + 实验流程 + 可解释性总结
  ↓
实验完成后写入 Experiment Memory
```

第一阶段不要求全部实现复杂模块。最小可用版本应至少实现：

```text
Structured Parser → Result Auditor → DOE Planner → Formula Writer → Rule Checker
```

---

# 二、第一层：必须先做

第一层是当前最重要的优化。目标是在不重新训练模型的情况下，显著降低黑盒感，使模型输出的下一轮配方有清晰的继承关系和变量控制逻辑。

第一层包括四个模块：

1. 结构化实验状态表。
2. 串行多 Agent。
3. 强制输出配方继承关系表。
4. Rule Checker。

---

## 1.1 结构化实验状态表

### 1.1.1 为什么要做

当前模型如果只读自然语言形式的实验描述，容易丢失关键信息，例如某个配方是否成胶、摩擦系数是否低、失败原因是什么、哪个变量可能起作用等。对于 14B 模型来说，长文本中的信息提取能力和多轮一致性有限。因此需要将每轮实验结果转成固定 schema。

结构化状态表的作用是：

1. 降低模型理解负担。
2. 明确每个配方的关键变量。
3. 让下一轮设计可以追溯到具体实验结论。
4. 方便后续 Rule Checker 检查变量变化。
5. 方便后续 RAG 或数据库检索。

### 1.1.2 建议新增文件

建议 Codex 新增以下文件：

```text
schemas/experiment_schema.py
schemas/experiment_schema.json
utils/experiment_parser.py
utils/experiment_memory.py
examples/experiment_state_example.json
```

如果项目当前是纯 Python，可以先使用 Python dict 或 Pydantic model。若希望更轻量，也可以先只定义 JSON schema。

### 1.1.3 建议的 ExperimentRound 数据结构

```json
{
  "project_goal": "Develop low-friction PVA-based hydrogel under defined load while maintaining gel integrity and reproducibility.",
  "round_id": "R1",
  "load_condition": "10 N",
  "evaluation_metrics": [
    "friction_coefficient",
    "gelation_status",
    "sample_integrity",
    "mechanical_stability",
    "transparency",
    "uniformity",
    "reproducibility"
  ],
  "allowed_materials": [
    "PVA",
    "water",
    "borax",
    "glutaraldehyde",
    "glycerol"
  ],
  "forbidden_materials": [],
  "formula_records": []
}
```

### 1.1.4 建议的 FormulaRecord 数据结构

```json
{
  "formula_id": "R1-01",
  "parent_formula_id": null,
  "design_type": "initial_exploration",
  "total_mass_g": 10.0,
  "composition": {
    "PVA": {
      "amount_g": 1.0,
      "wt_percent": 10.0
    },
    "water": {
      "amount_g": 9.0
    },
    "crosslinker": {
      "name": "borax",
      "amount_g": 0.1,
      "concentration": "1 wt%"
    },
    "additives": [
      {
        "name": "glycerol",
        "amount_g": 0.5,
        "wt_percent": 5.0
      }
    ]
  },
  "processing": {
    "heating_temperature_c": 90,
    "heating_time_min": 60,
    "stirring_condition": "constant stirring until fully dissolved",
    "crosslinking_condition": "add borax solution after cooling to 50 C",
    "freeze_thaw_cycles": 3,
    "gelation_time_h": 12,
    "pH": null
  },
  "observations": {
    "friction_coefficient": 0.018,
    "load": "10 N",
    "gelation_status": "complete",
    "sample_integrity": "intact",
    "mechanical_stability": "soft but stable",
    "transparency": "transparent",
    "uniformity": "uniform",
    "failure_notes": "slight deformation under load"
  },
  "interpretation": {
    "performance_rank": "good",
    "main_advantage": "low friction",
    "main_problem": "mechanical stability insufficient under load",
    "possible_positive_factors": [
      "moderate PVA concentration",
      "glycerol improved flexibility"
    ],
    "possible_negative_factors": [
      "network may be too soft"
    ],
    "next_round_suggestion": "increase PVA concentration or slightly increase crosslinking density while keeping glycerol constant"
  }
}
```

### 1.1.5 字段说明

必须保留的字段：

| 字段 | 作用 |
|---|---|
| formula_id | 配方编号，例如 R1-01 |
| parent_formula_id | 母配方编号，用于追踪继承关系 |
| design_type | 配方类型，例如 baseline、local_optimization、single_factor_perturbation |
| composition | 材料组成 |
| processing | 实验流程参数 |
| observations | 实验结果和现象 |
| interpretation | 对实验结果的解释 |

注意：第一轮初始配方可以没有 parent_formula_id，但从第二轮开始，每个配方都应该有 parent_formula_id 或 source_conclusion。

### 1.1.6 Codex 实现任务

请实现以下功能：

```text
1. 定义 ExperimentRound 和 FormulaRecord 的数据结构。
2. 提供 load_experiment_round(path) 函数，从 JSON 文件读取实验轮次。
3. 提供 save_experiment_round(round_obj, path) 函数，将实验轮次保存为 JSON。
4. 提供 validate_formula_record(record) 函数，检查必要字段是否缺失。
5. 提供 summarize_round(round_obj) 函数，输出用于 LLM prompt 的简洁摘要。
```

建议函数接口：

```python
def load_experiment_round(path: str) -> dict:
    pass


def save_experiment_round(round_obj: dict, path: str) -> None:
    pass


def validate_formula_record(record: dict) -> list[str]:
    """Return a list of validation errors. Return empty list if valid."""
    pass


def summarize_round(round_obj: dict) -> str:
    """Convert structured experiment state into a concise LLM-readable summary."""
    pass
```

### 1.1.7 验收标准

完成后应满足：

1. 可以用 JSON 保存和读取每轮实验记录。
2. 每个配方都有明确组成、工艺、结果和解释。
3. 任何缺少关键字段的配方都能被检查出来。
4. 结构化摘要可以直接提供给后续 Agent 使用。

---

## 1.2 串行多 Agent

### 1.2.1 为什么要做

14B 模型不适合一次性完成全部任务。如果直接让它从上一轮结果生成下一轮完整配方，它容易跳过中间推理，直接给出看似合理的新配方。

因此需要拆成串行多 Agent：

```text
Agent 1：Result Auditor
Agent 2：DOE Planner
Agent 3：Formula and Protocol Writer
Agent 4：Critic / Rule Checker
```

每个 Agent 的输入和输出都应该结构化。下一个 Agent 只能基于上一个 Agent 的输出继续，不能重新自由发挥。

---

### 1.2.2 Agent 1：Result Auditor

#### 任务

读取上一轮配方和实验结果，输出实验结果审计报告。

#### 输入

```text
1. 项目目标
2. 材料约束
3. 上一轮配方结构化表
4. 上一轮实验结果
```

#### 输出

```json
{
  "best_formulas": [],
  "failed_formulas": [],
  "promising_variables": [],
  "risky_variables": [],
  "variables_to_keep_constant": [],
  "variables_to_optimize_next": [],
  "round_level_conclusions": []
}
```

#### Prompt 模板

```text
你是水凝胶闭环优化项目中的 Result Auditor。你的任务不是生成新配方，而是审计上一轮实验结果。

请根据输入的上一轮配方和实验结果，完成以下任务：

1. 找出表现最好的 1 到 3 个配方，并说明依据。
2. 找出失败或表现较差的配方，并说明失败原因。
3. 总结可能降低摩擦系数的变量。
4. 总结可能导致成胶失败、脆化、过软、摩擦升高或样品不稳定的变量。
5. 判断下一轮应保持不变的变量。
6. 判断下一轮可以优先优化的变量。
7. 输出必须基于实验结果，不允许凭空推断。

请按照 JSON 格式输出，不要生成下一轮配方。
```

---

### 1.2.3 Agent 2：DOE Planner

#### 任务

基于 Result Auditor 的输出，设计下一轮配方继承关系表。此阶段不输出完整实验步骤。

#### 输入

```text
1. Result Auditor 输出
2. 当前允许材料列表
3. 用户要求的下一轮配方数量
4. 项目目标
```

#### 输出

一个配方继承关系表，结构如下：

```json
{
  "next_round_id": "R2",
  "design_principles": [],
  "formula_lineage_table": [
    {
      "new_formula_id": "R2-01",
      "design_type": "baseline_reproduction",
      "parent_formula_id": "R1-04",
      "source_conclusion": "R1-04 showed the lowest friction coefficient with complete gelation.",
      "variables_kept_constant": [],
      "variables_changed": [],
      "change_magnitude": "none",
      "hypothesis": "Reproduce the best condition to test repeatability.",
      "expected_result": "Similar friction coefficient and gel integrity as R1-04.",
      "if_better_then": "The previous result may be robust or slightly improved by handling variation.",
      "if_worse_then": "The previous result may lack reproducibility and should not be used as the sole parent formula.",
      "black_box_risk_score": 1
    }
  ]
}
```

#### Prompt 模板

```text
你是水凝胶闭环优化项目中的 DOE Planner。你的任务是生成下一轮实验设计的继承关系表，而不是生成完整实验流程。

请根据 Result Auditor 的审计结果，设计下一轮配方集合。

硬性规则：
1. 每个新配方必须有 parent_formula_id 或明确的 source_conclusion。
2. 每个新配方最多改变 1 到 2 个关键变量。
3. 至少 1 个配方必须是上一轮最佳配方的复现基线。
4. 至少 50% 的配方必须来自上一轮最佳或次优配方的局部优化。
5. 至少 1 个配方必须用于验证上一轮失败原因。
6. 探索配方不能超过总数的 25%。
7. 不允许引入不在允许材料列表中的材料。
8. 黑盒风险评分为 4 或 5 的配方不能进入最终列表。

配方类型只能从以下类型中选择：
- baseline_reproduction
- local_optimization
- single_factor_perturbation
- failure_cause_validation
- limited_exploration

请输出 JSON，包括 design_principles 和 formula_lineage_table。
不要输出完整实验步骤。
```

---

### 1.2.4 Agent 3：Formula and Protocol Writer

#### 任务

只根据 DOE Planner 已经批准的继承关系表，生成完整配方和实验流程。

#### 关键限制

Formula Writer 不允许新增 DOE Planner 没有批准的新材料、新变量或新工艺方向。

#### 输入

```text
1. formula_lineage_table
2. 母配方详细组成
3. 材料约束
4. 实验操作约束
```

#### 输出

```json
{
  "next_round_formulas": [
    {
      "formula_id": "R2-01",
      "design_type": "baseline_reproduction",
      "parent_formula_id": "R1-04",
      "design_purpose": "Confirm reproducibility of the best R1 formulation.",
      "composition": {},
      "processing": {},
      "step_by_step_protocol": [],
      "critical_notes": [],
      "expected_risks": [],
      "question_answered_by_this_formula": "Whether R1-04 performance is reproducible."
    }
  ]
}
```

#### Prompt 模板

```text
你是水凝胶闭环优化项目中的 Formula and Protocol Writer。

你只能根据 DOE Planner 提供的 formula_lineage_table 生成完整配方和实验流程。

严格规则：
1. 不允许新增 formula_lineage_table 中没有出现的材料。
2. 不允许改变 formula_lineage_table 中未批准改变的变量。
3. 每个配方必须保留 parent_formula_id、design_type 和 design_purpose。
4. 每个配方必须说明它能回答什么实验问题。
5. 实验步骤必须完整、可操作、可复现。
6. 如果某个参数不确定，请标注为“需要用户确认”或“建议保持母配方条件”，不要凭空补充。

请输出 JSON 或 Markdown 表格，结构必须清晰。
```

---

## 1.3 强制输出配方继承关系表

### 1.3.1 为什么要做

配方继承关系表是解决黑盒问题的核心。只要每个新配方都必须先在继承关系表中出现，模型就不能直接凭空输出新配方。

### 1.3.2 表格字段

每个下一轮配方必须包含以下字段：

| 字段 | 含义 |
|---|---|
| new_formula_id | 下一轮配方编号 |
| design_type | 设计类型 |
| parent_formula_id | 母配方编号 |
| source_conclusion | 来源结论 |
| variables_kept_constant | 保持不变的变量 |
| variables_changed | 改变的变量 |
| change_magnitude | 改变幅度 |
| hypothesis | 设计假设 |
| expected_result | 预期结果 |
| if_better_then | 如果结果变好，说明什么 |
| if_worse_then | 如果结果变差，说明什么 |
| black_box_risk_score | 黑盒风险评分 |

### 1.3.3 黑盒风险评分

```text
1 分：完全可追踪，来自明确母配方，只改变一个变量。
2 分：基本可追踪，来自明确母配方，改变两个相关变量。
3 分：有一定依据，但继承关系不够强。
4 分：探索性较强，依据较弱。
5 分：几乎凭空出现。
```

规则：

```text
黑盒风险评分为 4 或 5 的配方不能进入最终输出，除非用户明确要求探索。
```

### 1.3.4 Codex 实现任务

请实现：

```python
def validate_lineage_table(lineage_table: list[dict]) -> list[str]:
    """Check whether every planned formula has traceable origin and acceptable black-box risk."""
    pass


def count_design_types(lineage_table: list[dict]) -> dict:
    """Count how many formulas belong to each design type."""
    pass


def check_variable_change_limit(lineage_table: list[dict], max_changed_variables: int = 2) -> list[str]:
    """Check whether each formula changes no more than the allowed number of key variables."""
    pass
```

---

## 1.4 Rule Checker

### 1.4.1 为什么要做

Prompt 约束对小模型不够稳定。模型可能仍然会：

1. 引入未授权材料。
2. 一次改变太多变量。
3. 忘记设置基线复现。
4. 缺少失败原因验证。
5. 给出没有母配方的设计。
6. 偏离低摩擦目标。

因此需要 Rule Checker 对模型输出做硬检查。

### 1.4.2 规则列表

建议实现以下规则：

```text
Rule 1：所有新配方必须有 parent_formula_id 或 source_conclusion。
Rule 2：所有新配方必须有 design_type。
Rule 3：每个新配方最多改变 2 个关键变量。
Rule 4：至少 1 个配方为 baseline_reproduction。
Rule 5：至少 50% 配方为 local_optimization 或 single_factor_perturbation，且来源于最佳或次优配方。
Rule 6：至少 1 个配方为 failure_cause_validation。
Rule 7：limited_exploration 数量不得超过总配方数的 25%。
Rule 8：不允许使用 allowed_materials 之外的材料。
Rule 9：black_box_risk_score 不能大于 3。
Rule 10：每个配方必须有 hypothesis、expected_result、if_better_then、if_worse_then。
```

### 1.4.3 建议新增文件

```text
utils/rule_checker.py
```

### 1.4.4 建议函数接口

```python
def check_allowed_materials(formula: dict, allowed_materials: list[str]) -> list[str]:
    pass


def check_required_lineage_fields(plan: dict) -> list[str]:
    pass


def check_design_type_distribution(lineage_table: list[dict], total_n: int) -> list[str]:
    pass


def check_black_box_risk(lineage_table: list[dict], max_score: int = 3) -> list[str]:
    pass


def run_all_rule_checks(plan: dict, allowed_materials: list[str]) -> dict:
    """Return {'passed': bool, 'errors': list[str], 'warnings': list[str]}"""
    pass
```

### 1.4.5 Rule Checker 输出格式

```json
{
  "passed": false,
  "errors": [
    "R2-05 has no parent_formula_id or source_conclusion.",
    "R2-06 uses a material not in allowed_materials: tannic acid.",
    "No baseline_reproduction formula found."
  ],
  "warnings": [
    "R2-04 changes two variables simultaneously. This is allowed but should be justified."
  ]
}
```

### 1.4.6 验收标准

完成后应满足：

1. 如果模型输出没有继承关系，系统能报错。
2. 如果模型引入未授权材料，系统能报错。
3. 如果模型一次改变太多变量，系统能报错。
4. 如果没有基线复现，系统能报错。
5. 如果黑盒风险高于阈值，系统能阻止进入最终输出。

---

# 三、第二层：中期增强

第二层用于提高系统稳定性、长期记忆能力和候选方案质量。建议在第一层跑通后再实现。

第二层包括：

1. Candidate + Critic。
2. 实验轨迹 RAG。

---

## 2.1 Candidate + Critic

### 2.1.1 为什么要做

14B 模型单次生成结果可能不稳定。一次生成可能刚好合理，也可能出现黑盒跳跃。更稳妥的方式是让模型生成多套候选 DOE，然后由 Critic Agent 逐一打分，选择最好的方案。

流程：

```text
输入上一轮结果
  ↓
DOE Planner 生成 3 到 5 套候选设计
  ↓
Critic Agent 对每套候选设计评分
  ↓
选择最高分且无硬性错误的一套
  ↓
Formula Writer 生成完整配方和实验流程
```

### 2.1.2 候选方案数量

建议默认：

```text
num_candidates = 3
```

如果模型速度允许，可以设为 5。

### 2.1.3 Critic 评分维度

每套候选 DOE 需要按以下维度打分：

| 维度 | 分数范围 | 含义 |
|---|---|---|
| data_usage | 1 到 5 | 是否充分利用上一轮实验结果 |
| traceability | 1 到 5 | 是否有清晰继承关系 |
| variable_control | 1 到 5 | 是否控制变量 |
| hypothesis_quality | 1 到 5 | 假设是否明确可检验 |
| doe_structure | 1 到 5 | 是否构成合理 DOE |
| failure_learning | 1 到 5 | 是否从失败配方学习 |
| material_feasibility | 1 到 5 | 材料和工艺是否可行 |
| low_friction_alignment | 1 到 5 | 是否围绕低摩擦目标 |

总分：

```text
total_score = sum(all_dimensions)
```

但如果存在硬性错误，例如未授权材料、无母配方、黑盒风险过高，则不管总分多高都不能通过。

### 2.1.4 Critic Agent Prompt

```text
你是水凝胶闭环优化系统中的 Critic Agent。你的任务不是生成新配方，而是评价候选 DOE 是否适合作为下一轮实验设计。

请根据以下维度打分：

1. data_usage：是否充分利用上一轮实验结果。
2. traceability：每个新配方是否有明确母配方或来源结论。
3. variable_control：是否每个配方最多改变 1 到 2 个关键变量。
4. hypothesis_quality：每个配方是否有明确可检验假设。
5. doe_structure：是否包括基线复现、局部优化、单因素扰动、失败原因验证和必要探索。
6. failure_learning：是否吸收上一轮失败配方的信息。
7. material_feasibility：是否符合材料和工艺约束。
8. low_friction_alignment：是否服务于低摩擦 PVA 水凝胶目标。

请输出：
1. 每个维度 1 到 5 分。
2. 总分。
3. 硬性错误列表。
4. 黑盒跳跃点列表。
5. 是否推荐进入最终配方生成。
6. 需要修改的地方。
```

### 2.1.5 Codex 实现任务

建议新增：

```text
agents/candidate_generator.py
agents/critic_agent.py
utils/candidate_selector.py
```

建议函数接口：

```python
def generate_candidate_doe(input_state: dict, n_candidates: int = 3) -> list[dict]:
    pass


def critique_candidate(candidate: dict, input_state: dict) -> dict:
    pass


def select_best_candidate(candidates: list[dict], critiques: list[dict]) -> dict:
    pass
```

选择规则：

```text
1. 先过滤掉有 hard_errors 的候选。
2. 再过滤掉任何 black_box_risk_score > 3 的候选。
3. 在剩余候选中选择总分最高者。
4. 如果没有候选通过，则返回错误，并要求 DOE Planner 重新生成。
```

---

## 2.2 实验轨迹 RAG

### 2.2.1 为什么要做

当实验轮数增加后，模型不能每次都靠长 prompt 读取全部历史。需要将历史实验记录转换成可检索数据库，让模型在生成下一轮之前检索相关历史。

实验轨迹 RAG 不只是检索文献，而是检索项目自己的历史经验。

### 2.2.2 需要存储的内容

每个配方应存储：

```json
{
  "round_id": "R2",
  "formula_id": "R2-03",
  "parent_formula_id": "R1-04",
  "design_type": "local_optimization",
  "changed_variables": ["PVA_wt_percent"],
  "unchanged_variables": ["crosslinker", "glycerol", "freeze_thaw_cycles"],
  "hypothesis": "Increasing PVA may improve sample integrity while maintaining low friction.",
  "composition_summary": "PVA 12 wt%, borax 1 wt%, glycerol 5 wt%",
  "processing_summary": "90 C dissolution, 3 freeze-thaw cycles",
  "friction_coefficient": 0.015,
  "load": "10 N",
  "gelation_status": "complete",
  "failure_notes": "none",
  "interpretation": "PVA increase improved integrity without severe friction penalty.",
  "next_action": "test slightly lower crosslinker to reduce friction"
}
```

### 2.2.3 检索类型

建议支持以下检索：

```text
1. 检索历史最佳低摩擦配方。
2. 检索所有失败配方。
3. 检索某个变量的历史影响，例如 PVA 浓度增加的结果。
4. 检索某个材料的历史表现，例如 glycerol 是否改善柔韧性。
5. 检索某个设计类型，例如 failure_cause_validation。
6. 检索与当前目标最相似的历史配方。
```

### 2.2.4 Codex 实现任务

建议新增：

```text
memory/experiment_store.py
memory/retriever.py
memory/index_builder.py
```

第一版可以不用向量数据库，先用 JSONL + keyword filter。后续再换 Chroma、FAISS 或 SQLite FTS。

建议函数接口：

```python
def append_experiment_record(record: dict, path: str = "memory/experiment_records.jsonl") -> None:
    pass


def load_experiment_records(path: str = "memory/experiment_records.jsonl") -> list[dict]:
    pass


def retrieve_best_formulas(records: list[dict], metric: str = "friction_coefficient", top_k: int = 5) -> list[dict]:
    pass


def retrieve_failed_formulas(records: list[dict], top_k: int = 5) -> list[dict]:
    pass


def retrieve_by_variable(records: list[dict], variable_name: str, top_k: int = 10) -> list[dict]:
    pass


def build_context_from_retrieval(records: list[dict]) -> str:
    pass
```

### 2.2.5 输入到 Agent 的上下文格式

检索结果不要直接以原始 JSON 全量塞给模型。应该整理成短摘要，例如：

```text
Relevant historical findings:

1. R2-03 inherited from R1-04 and increased PVA from 10 wt% to 12 wt%. It improved sample integrity while friction changed from 0.018 to 0.015 under 10 N.
2. R2-05 increased borax concentration from 1 wt% to 2 wt%. It caused brittleness and friction increased to 0.026.
3. R1-07 used glycerol 15 wt% and showed good flexibility but weak gel integrity.

Implication for next round:
- PVA increase may be useful within 10 to 12 wt%.
- Excess borax should be avoided.
- Glycerol should be optimized in a narrow range rather than increased aggressively.
```

### 2.2.6 验收标准

完成后应满足：

1. 每轮实验可以自动写入历史记录。
2. 新一轮设计前可以检索历史最佳和失败案例。
3. Agent prompt 中包含相关历史，而不是全部历史。
4. 下一轮设计能引用历史实验记录。

---

# 四、第三层：后期增强

第三层用于进一步提高模型本身的行为质量。建议在第一层和第二层稳定后再做。

第三层包括：

1. Verifier 模型。
2. DPO 或偏好训练。

---

## 3.1 Verifier 模型

### 3.1.1 为什么要做

生成模型和评价模型最好分开。Generator 负责提出方案，Verifier 负责判断方案是否合格。

对于 14B 模型，可以先使用同一个模型作为 Verifier，也可以微调一个更小的模型专门做分类和评分。

Verifier 不需要生成配方，只需要回答：

```text
这个下一轮配方设计是否可追踪、合理、符合约束？
```

### 3.1.2 Verifier 输入

```json
{
  "project_goal": "low-friction PVA hydrogel",
  "previous_round": {},
  "candidate_next_round_plan": {},
  "allowed_materials": [],
  "rules": []
}
```

### 3.1.3 Verifier 输出

```json
{
  "passed": true,
  "overall_score": 86,
  "dimension_scores": {
    "data_usage": 5,
    "traceability": 5,
    "variable_control": 4,
    "hypothesis_quality": 4,
    "doe_structure": 5,
    "failure_learning": 4,
    "material_feasibility": 5,
    "low_friction_alignment": 5
  },
  "hard_errors": [],
  "warnings": [],
  "black_box_jumps": [],
  "recommended_action": "accept"
}
```

### 3.1.4 Verifier 标签体系

建议人工标注或半自动构造以下标签：

```text
accept
minor_revision
major_revision
reject
```

同时标注问题类型：

```text
no_parent_formula
too_many_variables_changed
unauthorized_material
missing_baseline
missing_failure_validation
weak_hypothesis
black_box_jump
unsafe_or_inoperable_protocol
off_target_design
```

### 3.1.5 Codex 实现任务

建议新增：

```text
verifier/verifier_prompt.py
verifier/verifier_runner.py
verifier/verifier_dataset_builder.py
```

建议函数接口：

```python
def build_verifier_prompt(previous_round: dict, candidate_plan: dict, rules: dict) -> str:
    pass


def run_verifier(prompt: str) -> dict:
    pass


def parse_verifier_output(output: str) -> dict:
    pass
```

### 3.1.6 第一版实现方式

第一版不需要真的训练新模型，可以直接使用 prompt-based verifier。

也就是说：

```text
Generator LLM 生成候选方案
同一个或另一个 LLM 使用 verifier prompt 打分
Rule Checker 做硬规则检查
```

等积累足够样本后，再训练专门的 Verifier。

---

## 3.2 DPO 或偏好训练

### 3.2.1 为什么要做

如果后续希望模型本身更倾向于输出可追踪、可解释的闭环设计，可以使用偏好训练。

核心思想：给模型成对样本。

```text
chosen：好的输出
rejected：差的输出
```

模型学习偏向 chosen，远离 rejected。

### 3.2.2 好输出标准

chosen 样本应该满足：

```text
1. 有上一轮结果审计。
2. 有明确最佳和失败配方分析。
3. 有下一轮设计原则。
4. 有配方继承关系表。
5. 每个配方最多改变 1 到 2 个变量。
6. 有基线复现。
7. 有局部优化。
8. 有失败原因验证。
9. 不引入无依据新材料。
10. 有完整实验流程。
11. 有黑盒风险自查。
```

### 3.2.3 差输出标准

rejected 样本通常具有以下问题：

```text
1. 直接给新配方，没有上一轮结果审计。
2. 没有母配方。
3. 随机引入新材料。
4. 每个配方同时改变多个关键变量。
5. 缺少基线复现。
6. 缺少失败原因验证。
7. 没有说明假设。
8. 无法判断实验结果好坏说明什么。
9. 配方看似丰富但没有 DOE 结构。
10. 偏离低摩擦 PVA 水凝胶目标。
```

### 3.2.4 偏好数据格式

建议格式：

```json
{
  "prompt": "Based on R1 formulations and experimental results, design R2 formulations for low-friction PVA hydrogel.",
  "chosen": "...output with audit, lineage table, controlled DOE, protocol...",
  "rejected": "...output with random new formulas and no lineage..."
}
```

### 3.2.5 Codex 实现任务

建议新增：

```text
training/build_preference_dataset.py
training/preference_data_schema.json
training/examples/chosen_rejected_example.jsonl
```

建议函数接口：

```python
def build_preference_pair(prompt: str, chosen: str, rejected: str) -> dict:
    pass


def validate_preference_pair(pair: dict) -> list[str]:
    pass


def export_preference_dataset(pairs: list[dict], output_path: str) -> None:
    pass
```

### 3.2.6 什么时候开始做 DPO

不建议一开始就做 DPO。建议满足以下条件后再做：

```text
1. 已经有至少 50 到 100 个高质量 chosen/rejected 样本。
2. 第一层的结构化状态表和继承关系表已经稳定。
3. Rule Checker 已经能自动判断硬性错误。
4. 人工已经确认什么样的输出才是好输出。
```

---

# 五、推荐实施顺序

建议 Codex 按以下顺序逐步优化。

## 阶段 1：最小可用闭环系统

优先实现：

```text
1. experiment_schema
2. experiment_parser
3. Result Auditor prompt
4. DOE Planner prompt
5. Formula Writer prompt
6. lineage_table validation
7. Rule Checker
```

目标：让系统每轮输出都包含：

```text
上一轮结果审计
下一轮设计原则
配方继承关系表
完整配方和实验流程
黑盒风险自查
```

## 阶段 2：多候选和评审

实现：

```text
1. generate multiple DOE candidates
2. Critic Agent scoring
3. candidate selection
4. failed candidate retry
```

目标：提高 14B 模型输出稳定性。

## 阶段 3：实验轨迹记忆

实现：

```text
1. JSONL experiment memory
2. retrieve best formulas
3. retrieve failed formulas
4. retrieve by variable
5. build retrieved context for prompts
```

目标：让模型能够利用多轮历史实验，而不是只依赖当前 prompt。

## 阶段 4：Verifier 和偏好数据

实现：

```text
1. prompt-based verifier
2. verifier output parser
3. preference dataset builder
4. chosen/rejected 数据积累
```

目标：为后续训练 Verifier 或 DPO 做准备。

---

# 六、建议的目录结构

可以按以下方式组织代码：

```text
project_root/
  agents/
    result_auditor.py
    doe_planner.py
    formula_writer.py
    critic_agent.py
  prompts/
    result_auditor_prompt.txt
    doe_planner_prompt.txt
    formula_writer_prompt.txt
    critic_prompt.txt
    verifier_prompt.txt
  schemas/
    experiment_schema.py
    experiment_schema.json
    lineage_schema.json
  utils/
    experiment_parser.py
    experiment_memory.py
    rule_checker.py
    lineage_validator.py
    candidate_selector.py
  memory/
    experiment_records.jsonl
    experiment_store.py
    retriever.py
  verifier/
    verifier_runner.py
    verifier_dataset_builder.py
  training/
    build_preference_dataset.py
    preference_data_schema.json
  examples/
    experiment_state_example.json
    lineage_table_example.json
    candidate_doe_example.json
  main.py
```

---

# 七、最终输出格式要求

无论后端如何实现，最终给用户的输出都应固定为以下结构：

```text
一、上一轮结果审计
1. 最佳配方及依据
2. 失败配方及原因
3. 可能有效的变量
4. 可能有风险的变量
5. 下一轮应保持不变的变量
6. 下一轮应优先优化的变量

二、下一轮设计原则
1. 优化主线
2. 变量控制策略
3. 配方类型分配
4. 本轮实验要回答的问题

三、配方继承关系表
表格列：
- 下一轮配方编号
- 设计类型
- 母配方或来源结论
- 保持不变变量
- 改变变量
- 改变幅度
- 设计依据
- 预期结果
- 如果变好说明什么
- 如果变差说明什么
- 黑盒风险评分

四、完整配方和实验流程
每个配方包括：
- 配方编号
- 设计类型
- 母配方
- 设计目的
- 材料组成
- 实验步骤
- 注意事项
- 预期风险
- 能回答的实验问题

五、闭环可解释性总结
说明为什么本轮设计不是随机试配方。

六、下一轮实验记录模板
列出实验完成后应记录哪些指标，方便进入再下一轮优化。
```

---

# 八、关键注意事项

1. 不要让模型直接从上一轮结果生成完整配方。
   必须先经过结果审计和 DOE 规划。

2. Formula Writer 不应该有自由新增材料的权限。
   它只能把 DOE Planner 已批准的设计具体化。

3. 继承关系表是硬性中间产物。
   没有继承关系表，不允许生成最终配方。

4. Rule Checker 必须在最终输出前运行。
   如果 Rule Checker 不通过，应返回错误并要求重新规划。

5. 小模型的能力增强主要来自流程约束，而不是让模型一次性承担全部推理。

6. 第一版不必实现复杂 RAG 或 DPO。
   先把结构化输入、串行 Agent、继承关系表和 Rule Checker 跑通。

7. 所有设计都必须服务于低摩擦 PVA 基水凝胶目标。
   不要因为材料学知识丰富就频繁跳到新体系。

8. 探索配方可以有，但必须少量、标注清楚，并说明依据。

---

# 九、Codex 优先任务清单

请 Codex 优先完成以下任务：

```text
Task 1：新增 experiment_schema 和示例 JSON。
Task 2：实现实验状态读取、保存、验证和摘要函数。
Task 3：新增 Result Auditor prompt，并实现调用接口。
Task 4：新增 DOE Planner prompt，并强制输出 lineage_table。
Task 5：实现 lineage_table validator。
Task 6：新增 Formula Writer prompt，限制其只能使用 lineage_table。
Task 7：实现 Rule Checker。
Task 8：在主流程中串联 Auditor → Planner → Rule Checker → Writer → Final Check。
Task 9：输出固定 Markdown 结果。
Task 10：保存每轮结果到 experiment_records.jsonl。
```

第一轮优化完成后，系统至少应做到：

```text
1. 下一轮每个配方都有母配方或来源结论。
2. 每个配方最多改变 1 到 2 个关键变量。
3. 输出包含配方继承关系表。
4. 至少有一个基线复现配方。
5. 至少有一个失败原因验证配方。
6. 不会引入未授权材料。
7. 可以解释每个新配方的设计目的。
8. 用户能看出模型不是随机换配方。
```
