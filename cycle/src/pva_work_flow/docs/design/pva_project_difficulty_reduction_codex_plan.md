# 降低 PVA 水凝胶闭环优化项目难度的修改建议

## 0. 文档目的

本文件用于指导 Codex 对现有项目进行修改。目标不是继续扩大系统能力，而是主动降低任务难度，使项目从“开放式材料发现”调整为“受约束的材料实验规划系统”。

## 当前实现状态（2026-05-28）

本文档中的最小可行修改方案已经基本落地，当前代码状态如下：

- 2026-05-29 更新：系统已支持配方优化树主线。R1 可通过 `--tree_initial_roots 10` 准备约 10 个初始 root；R2+ 可通过 `--target_parent_id` 锁定一个父配方节点，只做该节点的局部小步分支。
- 文章数据丰富性来自多棵独立 root tree；单次优化操作仍只从一个配方出发，避免不同父本混在同一组 DOE 中导致因果关系不清。
- 2026-05-29 补充：系统新增跨树统计 RAG。树内不混父节点；树间用 `tree_statistics.py` 汇总变量效果、rescue_success_rate、kill_rate 和 root tree ranking，让后续树使用已有实验统计先验。
- 已新增 `constrained_doe.py`：R2+ 默认生成最多 4 个受限 DOE 继承条目，默认关闭 `limited_exploration`。
- 已新增/强化 `candidate_rules.py`：检查 `parent_candidate_id`、PVA 主体系、baseline 完全复现、变量变化数量、黑盒跳跃风险，并输出每轮继承表。
- `pipeline_agents.py` 在 `skeleton_source=code_constrained_doe` 时会绕开 LLM 自由配方生成，直接复制父配方并只修改 `variables_changed` 中列出的变量。
- `changed_variables` 已改成语义设计变量检测：只比较 formulation/process 的实验设计变量，不把 `ratio_planner` 派生出的材料 amount/unit/basis 当作额外变量变化。
- CLI 和 `pva_vllm.sh` 的默认候选数量已经收缩为 4，更适合 14B 模型和小实验预算。
- `audit_status` 与 `experimental_status` 已分开记录，避免把 JSON 审计失败误判为实验失败。
- 每轮会输出 `R{N}_inheritance_table.md`，用于人工检查父子关系、设计类型、变量变化、if_better/if_worse 和风险分数。
- 已新增 `RunWorkspace` 和 CLI 便捷命令：`--status` 查看当前运行目录状态，`--sync_results` 从实验 CSV 重建结果，`--regenerate_round N --archive_old` 安全归档旧轮次后重生成。

因此，当前系统已经从“让 14B 模型自主发明下一轮材料配方”调整为“代码约束的小步 DOE + LLM 辅助解释 + 树状局部优化 + 跨树统计记忆”的可运行版本。后续若继续降低难度，优先方向应是实验预算管理、人工确认开关、branch_status 判定、统计 RAG 和更稳定的报告导出，而不是扩大模型自由度。

当前项目的原始目标可以概括为：

> 用大模型处理实验数据，并给出下一轮实验材料和配方建议。

这个目标本身难度很高。特别是在使用 14B 级别模型、湿实验配方总数最多约 100 个的条件下，如果仍然要求模型自主理解全部历史数据、自由提出新材料体系、设计完整配方并解释机制，系统很容易出现黑盒跳跃、变量失控、错误归因和不可执行配方。

因此，建议将项目目标改为：

> 用 14B 大模型在实验日志、材料白名单、固定变量范围、规则审计和模板化 DOE 动作的约束下，辅助分析实验结果，并推荐下一轮小步迭代实验。

核心思想是：

> 不让模型直接发明配方，而是让模型解释结果、提出假设、在有限动作中选择下一步，并由规则系统生成和审查候选配方。

---

## 1. 修改后的项目定位

### 1.1 原始定位

原始定位更接近：

> LLM driven autonomous material discovery system

即模型读取历史数据后，自主决定下一轮材料体系、配方组成和实验方向。

这个定位的问题是：

1. 材料空间过大。
2. 实验数据量有限。
3. 14B 模型长链推理能力不足。
4. 多目标优化任务复杂。
5. 配方生成容易变成黑盒随机探索。
6. 最多 100 个湿实验不足以支持大范围材料搜索。

### 1.2 推荐定位

建议调整为：

> LLM assisted constrained experimental planning system for PVA hydrogel tribology optimization

中文可以写成：

> 面向 PVA 水凝胶摩擦性能优化的受约束大模型实验规划系统。

在这个新定位下：

1. PVA 固定为主体材料。
2. 每一轮只允许围绕上一轮结果做小步修改。
3. 大模型不直接决定所有配方。
4. 配方候选先由规则模板或 DOE 动作生成骨架。
5. 14B 模型负责解释、排序、补充假设和生成报告。
6. 最终候选必须经过代码层审查后才能进入湿实验。

---

## 2. 难度降低的核心策略

建议将系统难度从原来的 9 到 10 分降低到 5 到 6 分。降低难度主要依赖以下四个策略。

### 2.1 科学目标降维

不要把目标定义为：

> 在 100 个实验内找到全局最优 PVA 水凝胶配方。

建议改为：

> 在 100 个实验内，围绕 PVA 主体系识别 2 到 3 个关键有效变量，并获得一组比初始基线更稳定、更低摩擦的候选配方。

这能降低目标难度，因为项目不再追求全局最优，而是追求可解释的局部优化和实验逻辑闭环。

### 2.2 材料空间降维

不要允许模型自由引入任意材料。建议将材料空间拆成三层：

#### 固定层

这些变量默认不变，除非人工明确修改：

```text
主体材料：PVA
测试载荷：10 N
对摩副：不锈钢
润滑介质：去离子水
滑动速度：5 mm/s
样品尺寸：固定
测试时间：固定
```

#### 可调层

这些变量允许模型或 DOE 模板调整：

```text
PVA 浓度
GA 浓度
HCl 浓度
CMC 浓度
透明质酸钠浓度
冻融循环次数
浸泡时间
固化温度
固化时间
```

#### 受限探索层

这些变量只能在 limited_exploration 类型中出现，每轮最多一个：

```text
一个新添加剂
一个新后处理方式
一个替代交联策略
```

### 2.3 模型职责降维

不要让 14B 模型完成完整的“数据读取 → 机制推理 → DOE 规划 → 配方生成 → 审查”链条。

建议将 14B 的职责限制为：

1. 总结上一轮实验现象。
2. 提出 2 到 3 个机制假设。
3. 判断哪些变量值得保留、微调或验证。
4. 在固定动作类型中选择下一轮策略。
5. 为候选配方补充解释、风险和 if better / if worse 路径。
6. 生成实验设计报告。

以下任务必须交给代码规则系统，而不是 14B：

1. 材料是否在白名单内。
2. baseline reproduction 是否完全一致。
3. parent_candidate_id 是否有效。
4. 每个候选改变了多少变量。
5. 是否发生黑盒跳跃。
6. 配方总量、单位、basis 是否完整。
7. 是否超出 100 个实验预算。
8. 是否违反 allowed design action 分布。

### 2.4 实验策略降维

不要让 100 个实验用于大范围探索。建议分配为：

```text
阶段 1：初筛与基线建立          20 个
阶段 2：最佳体系局部优化        30 个
阶段 3：机制验证                25 个
阶段 4：重复性与稳健性验证      20 个
机动名额                         5 个
总计                           100 个
```

也可以更保守地设置为：

```text
探索型实验：不超过 25 个
局部优化实验：约 40 个
机制验证实验：约 20 个
重复性实验：约 15 个
```

---

## 3. 修改后的工作流

建议将现有工作流从“LLM 直接生成完整候选配方”改为“两阶段候选生成”。

### 3.1 原始工作流

```text
实验结果
→ Audit Agent
→ DOE Planning Agent
→ Formula Generation Agent
→ Rule Checker
→ 输出下一轮候选配方
```

该流程中，Formula Generation Agent 承担了过多自由生成任务，容易导致材料跳跃和变量失控。

### 3.2 推荐工作流

```text
实验结果
→ 数据解析与指标计算
→ 代码识别最佳、次优、失败和异常配方
→ Audit Agent 解释结果
→ Constrained DOE Skeleton Generator 生成候选骨架
→ DOE Planning Agent 对骨架进行解释和排序
→ Formula Generation Agent 只填充允许范围内的具体参数
→ Rule Checker 严格审查
→ Candidate Critic 进行黑盒风险评分
→ 输出下一轮候选配方和实验报告
```

其中最关键的新增模块是：

```text
Constrained DOE Skeleton Generator
```

这个模块应由代码实现，不依赖 LLM。它根据固定规则生成候选骨架，例如：

```json
{
  "candidate_id": "R2-02",
  "design_type": "local_optimization",
  "parent_candidate_id": "R1-04",
  "allowed_action": "decrease_GA",
  "fixed_variables": ["PVA", "HCl", "CMC", "soak_time"],
  "changed_variables": [
    {
      "variable": "GA_concentration",
      "old_value": 1.0,
      "new_value": 0.5,
      "change_level": "small"
    }
  ]
}
```

LLM 的任务不是从零生成这个结构，而是解释为什么这个候选值得做。

---

## 4. 推荐的候选配方类型

所有 R2 及之后的候选配方都必须属于以下五类之一。

### 4.1 baseline_reproduction

目的：确认上一轮最佳配方是否可重复。

硬性规则：

1. 必须完全继承父配方。
2. 不允许改变任何材料、浓度、工艺参数。
3. 如果有任何变量变化，不能标记为 baseline_reproduction。
4. 每一轮至少 1 个。

### 4.2 local_optimization

目的：围绕上一轮最佳或次优配方做小步优化。

硬性规则：

1. 必须继承一个表现较好的父配方。
2. 每个候选最多改变 1 到 2 个变量。
3. 不允许引入新材料，除非 design_type 改为 limited_exploration。
4. 每一轮至少 40% 到 50% 的候选应属于此类。

### 4.3 single_factor_perturbation

目的：验证某一个变量是否真正影响性能。

硬性规则：

1. 只能改变 1 个变量。
2. 其他所有变量保持不变。
3. 必须写清楚验证的机制假设。
4. 适合验证 CMC 浓度、GA 浓度、浸泡时间、冻融次数等因素。

### 4.4 failure_verification

目的：从失败配方中提取有价值的信息，而不是直接丢弃失败方向。

硬性规则：

1. 继承一个表现较差但仍有诊断价值的配方。
2. 只能修复一个可能失败因素。
3. 必须说明这次实验希望回答什么问题。
4. 每轮可以有 0 到 1 个，不建议超过 1 个。

### 4.5 limited_exploration

目的：有限探索新材料或新处理方式。

硬性规则：

1. 每轮最多 1 个。
2. 必须保留 PVA 主体。
3. 必须保留至少一个已知有效变量。
4. 新材料必须来自材料白名单。
5. 必须说明它解决上一轮哪个具体问题。

---

## 5. 每轮候选比例建议

如果每轮生成 8 个候选，建议比例为：

```text
1 个 baseline_reproduction
3 个 local_optimization
2 个 single_factor_perturbation
1 个 failure_verification
1 个 limited_exploration
```

如果每轮生成 6 个候选，建议比例为：

```text
1 个 baseline_reproduction
3 个 local_optimization
1 个 single_factor_perturbation
1 个 failure_verification 或 limited_exploration
```

如果当前 14B 模型输出长度不足，每轮可以只生成 4 个候选：

```text
1 个 baseline_reproduction
2 个 local_optimization
1 个 single_factor_perturbation
```

优先保证逻辑质量，不强行要求 12 个候选。

---

## 6. 实验预算控制

新增一个实验预算管理逻辑，避免模型在 100 个配方以内失控探索。

### 6.1 建议新增配置

可以在配置文件中加入：

```yaml
experiment_budget:
  total_formula_budget: 100
  max_formulas_per_round: 8
  min_replicates_for_best_formula: 3
  max_limited_exploration_fraction: 0.25
  recommended_stage_allocation:
    screening: 20
    local_optimization: 30
    mechanism_validation: 25
    robustness_validation: 20
    reserve: 5
```

### 6.2 每轮生成前必须检查

1. 已经完成多少个配方。
2. 剩余配方预算是多少。
3. 当前处于哪个实验阶段。
4. 本轮是否还允许 limited_exploration。
5. 最佳配方是否已经完成重复性验证。
6. 是否应该从探索转入局部优化或稳健性验证。

---

## 7. 推荐新增模块

### 7.1 constrained_doe.py

建议新增文件：

```text
cycle/src/pva_work_flow/constrained_doe.py
```

该模块负责用规则生成候选骨架。

建议实现以下函数：

```python
def infer_experiment_stage(experiment_state):
    """根据已完成配方数量、最佳 COF 趋势、重复性情况判断当前阶段。"""

def select_parent_candidates(experiment_records, top_k=3):
    """选择最佳、次优、失败但有诊断价值的父配方。"""

def generate_baseline_reproduction(parent_candidate):
    """生成完全重复父配方的候选骨架。"""

def generate_local_optimization(parent_candidate, allowed_variables):
    """围绕父配方生成局部优化候选骨架。"""

def generate_single_factor_perturbation(parent_candidate, variable_grid):
    """生成单因素扰动候选骨架。"""

def generate_failure_verification(failed_candidate, diagnosis):
    """生成失败验证候选骨架。"""

def generate_limited_exploration(parent_candidate, allowed_new_materials):
    """在严格限制下生成探索型候选骨架。"""

def build_constrained_doe_skeleton(experiment_records, config):
    """综合生成下一轮候选骨架。"""
```

### 7.2 design_action_schema.py

建议新增文件：

```text
cycle/src/pva_work_flow/design_action_schema.py
```

用于定义允许的动作类型，例如：

```python
ALLOWED_ACTIONS = {
    "PVA_concentration": ["increase_small", "decrease_small", "keep"],
    "GA_concentration": ["increase_small", "decrease_small", "keep"],
    "HCl_concentration": ["increase_small", "decrease_small", "keep"],
    "CMC_concentration": ["increase_small", "decrease_small", "remove", "keep"],
    "soak_time": ["increase", "decrease", "keep"],
    "freeze_thaw_cycles": ["increase_by_1", "decrease_by_1", "keep"]
}
```

### 7.3 budget_manager.py

建议新增文件：

```text
cycle/src/pva_work_flow/budget_manager.py
```

负责管理 100 个实验配方预算。

建议实现：

```python
def count_completed_formulas(experiment_records):
    """统计已完成实验配方数量。"""

def get_remaining_budget(experiment_records, total_budget=100):
    """返回剩余可用配方数量。"""

def recommend_round_size(remaining_budget, current_stage):
    """根据剩余预算和阶段推荐本轮候选数量。"""

def exploration_allowed(experiment_records, config):
    """判断本轮是否允许 limited_exploration。"""

def replicate_required(best_candidate, experiment_records):
    """判断当前最佳配方是否需要重复验证。"""
```

---

## 8. 对现有文件的修改建议

### 8.1 prompts_agents.yaml

修改重点：

1. 将 system prompt 中的目标从“generate next round formulas”改为“explain and rank constrained DOE skeletons”。
2. 明确 14B 不能自由引入新材料。
3. 明确每个候选必须继承 parent_candidate_id。
4. 明确 audit failure 不等于 experimental failure。
5. 明确 baseline_reproduction 必须完全一致。
6. 要求输出 if_better 和 if_worse。
7. 降低每次生成候选数量要求，优先质量而非数量。

建议加入类似规则：

```text
You are not allowed to invent a completely new material system.
You must work within the constrained DOE skeleton provided by the code.
Your job is to explain, rank, and complete the provided candidate skeletons.
Do not change parent_candidate_id.
Do not change design_type.
Do not add new materials unless design_type is limited_exploration.
For baseline_reproduction, every material and process parameter must be identical to the parent candidate.
```

### 8.2 pipeline_agents.py

修改重点：

1. 在 DOE Planning Agent 之前调用 `build_constrained_doe_skeleton()`。
2. 将候选骨架传给 LLM。
3. LLM 只能补充 rationale、hypothesis、if_better、if_worse、risks。
4. 禁止 LLM 修改 skeleton 中的 parent_candidate_id、design_type 和 changed_variables。
5. 如果 LLM 修改了这些字段，自动恢复代码生成的原始值。

建议流程：

```text
Audit Agent
→ constrained_doe.build_constrained_doe_skeleton
→ DOE Planning Agent explains skeleton
→ Formula Generation Agent fills executable details
→ Rule Checker validates
```

### 8.3 generator.py

修改重点：

1. Formula Generation Agent 不再从零生成配方。
2. 输入必须是通过审查的 DOE skeleton。
3. 只允许在 skeleton 定义的变量范围内填充具体数值。
4. 对于 local_optimization 和 single_factor_perturbation，自动复制父配方未变变量。
5. 对于 baseline_reproduction，直接复制父配方，不调用 LLM 改写。

建议新增逻辑：

```python
if design_type == "baseline_reproduction":
    candidate = deepcopy(parent_candidate)
    candidate["candidate_id"] = new_candidate_id
    candidate["design_type"] = "baseline_reproduction"
    candidate["parent_candidate_id"] = parent_candidate["candidate_id"]
    return candidate
```

### 8.4 rule_checker.py

修改重点：

新增或强化以下规则：

1. `check_parent_id_validity`
2. `check_baseline_exact_reproduction`
3. `check_max_changed_variables`
4. `check_no_new_material_except_limited_exploration`
5. `check_pva_required`
6. `check_experiment_budget`
7. `check_design_type_distribution`
8. `check_if_better_if_worse_nonempty`
9. `check_audit_failure_not_used_as_experimental_failure`
10. `check_material_name_canonicalization`

### 8.5 candidate_critic.py

修改重点：

增加黑盒跳跃风险评分：

```text
black_box_jump_score
```

评分依据：

1. 新材料数量。
2. 删除父配方中表现相关的关键添加剂。
3. 同时改变变量数量。
4. 是否没有 parent_candidate_id。
5. 是否没有引用上一轮实验结果。
6. 是否从 PVA 体系跳到非 PVA 体系。
7. 是否把 audit failure 当成 experimental failure。
8. 是否把 baseline reproduction 写成非完全重复。

建议规则：

```text
black_box_jump_score >= 4: reject
black_box_jump_score 2 to 3: require human review
black_box_jump_score 0 to 1: pass
```

### 8.6 experiment_state.py

修改重点：

1. 增加实验阶段字段。
2. 记录每个配方的父子关系。
3. 记录每个配方的 design_type。
4. 记录每个配方的 changed_variables。
5. 记录每个配方是否已经重复验证。
6. 记录每个配方是否为 wet experiment completed。
7. 记录 audit failure 和 experimental failure 的区别。

建议新增字段：

```json
{
  "candidate_id": "R2-02",
  "round_id": "R2",
  "parent_candidate_id": "R1-04",
  "design_type": "local_optimization",
  "experiment_stage": "local_optimization",
  "changed_variables": ["GA_concentration"],
  "fixed_variables": ["PVA", "HCl", "CMC", "soak_time"],
  "is_replicate": false,
  "wet_experiment_completed": true,
  "audit_status": "passed",
  "experimental_status": "measured"
}
```

### 8.7 model_iteration_evaluation.md

修改重点：

将评价标准从“模型是否能生成好配方”改为“模型是否能在约束下做连续实验决策”。

建议新增评分项：

1. 是否遵守 constrained DOE skeleton。
2. 是否正确解释父配方继承关系。
3. 是否每个候选只改变允许变量。
4. 是否避免黑盒跳跃。
5. 是否合理使用 100 个实验预算。
6. 是否将最佳配方重复性验证纳入规划。
7. 是否区分探索、优化、验证和重复性实验。

---

## 9. 新的数据结构建议

### 9.1 DOE skeleton

建议每个候选先生成 skeleton：

```json
{
  "candidate_id": "R2-02",
  "design_type": "local_optimization",
  "parent_candidate_id": "R1-04",
  "source_observation": {
    "metric": "COF",
    "value": 0.008,
    "note": "R1-04 showed the lowest COF but asymmetric friction"
  },
  "fixed_variables": [
    "PVA_concentration",
    "HCl_concentration",
    "CMC_concentration",
    "soak_time"
  ],
  "changed_variables": [
    {
      "variable": "GA_concentration",
      "old_value": 1.0,
      "new_value": 0.5,
      "change_level": "small",
      "reason_code": "reduce_crosslink_density"
    }
  ],
  "new_materials_allowed": false,
  "requires_llm_fields": [
    "mechanistic_hypothesis",
    "if_better",
    "if_worse",
    "risks_and_mitigations"
  ]
}
```

### 9.2 LLM explanation output

LLM 只需要补充：

```json
{
  "candidate_id": "R2-02",
  "mechanistic_hypothesis": "Reducing GA concentration may decrease excessive crosslink density and improve surface hydration uniformity while retaining the CMC assisted lubrication mechanism.",
  "if_better": "If COF remains low and asymmetry decreases, the result supports that the previous friction instability was partly caused by excessive crosslink density.",
  "if_worse": "If COF increases, the original GA concentration may be necessary to maintain the network integrity, and later rounds should restore GA while optimizing post treatment.",
  "risks_and_mitigations": [
    {
      "risk": "Lower GA may weaken gel strength.",
      "mitigation": "Monitor compressive modulus and reject the candidate if the gel becomes too soft."
    },
    {
      "risk": "Lower crosslinking may increase swelling.",
      "mitigation": "Record swelling ratio before friction testing."
    }
  ]
}
```

---

## 10. 强制规则清单

Codex 应优先实现以下硬规则。

### 10.1 PVA 主体系规则

```text
Every candidate must contain PVA as the main polymer unless explicitly approved by human override.
```

### 10.2 父配方规则

```text
Every R2 or later candidate must have a valid parent_candidate_id from a previous completed round.
```

### 10.3 baseline 完全一致规则

```text
If design_type is baseline_reproduction, all materials, amounts, concentrations, process parameters and post treatment parameters must be identical to the parent candidate.
```

### 10.4 变量数量规则

```text
local_optimization: maximum 2 changed variables
single_factor_perturbation: exactly 1 changed variable
failure_verification: maximum 1 repaired failure factor
limited_exploration: maximum 1 new material or 1 new process
```

### 10.5 新材料限制规则

```text
New materials are only allowed in limited_exploration.
Each round can contain at most 1 limited_exploration candidate.
```

### 10.6 审计失败和实验失败区分规则

```text
Audit failure means the candidate record is incomplete or violates formatting or whitelist rules.
Experimental failure means the wet experiment failed or produced unusable material.
A candidate with measured COF must not be treated as experimental failure only because it failed audit.
```

### 10.7 实验预算规则

```text
The system must track the number of completed wet experiment formulas.
When remaining budget is low, exploration should be disabled and replication should be prioritized.
```

### 10.8 重复性规则

```text
A formula cannot be considered robust unless it has been repeated at least 3 times or manually marked as confirmed.
```

---

## 11. 推荐的阶段判断逻辑

### 11.1 screening 阶段

条件：

```text
completed_formulas < 20
```

目标：

1. 建立 PVA 基线。
2. 找到可成胶、可测量、COF 不太差的体系。
3. 初步筛选 2 到 3 个添加剂或处理因素。

禁止：

1. 过早优化小数点后浓度。
2. 过多重复。
3. 大量引入新材料。

### 11.2 local_optimization 阶段

条件：

```text
20 <= completed_formulas < 50
```

目标：

1. 围绕最优体系做局部优化。
2. 每个候选改变 1 到 2 个变量。
3. 主要优化 COF、摩擦稳定性和模量平衡。

禁止：

1. 跳到无关材料体系。
2. 每轮多个新材料。
3. 删除已表现有效的关键添加剂而不解释。

### 11.3 mechanism_validation 阶段

条件：

```text
50 <= completed_formulas < 75
```

目标：

1. 验证关键机制假设。
2. 做单因素扰动。
3. 区分 CMC、GA、HCl、浸泡时间等变量的贡献。

禁止：

1. 继续盲目探索。
2. 每个候选改变多个变量。
3. 只追求最低 COF，不解释原因。

### 11.4 robustness_validation 阶段

条件：

```text
75 <= completed_formulas <= 100
```

目标：

1. 重复最佳配方。
2. 验证批次稳定性。
3. 验证关键参数上下浮动的稳健性。
4. 形成最终报告。

禁止：

1. 大量引入新材料。
2. 做完全新方向。
3. 忽略重复性验证。

---

## 12. 修改后的提示词设计方向

### 12.1 Audit Agent

职责：

1. 总结上一轮结果。
2. 区分 measured、audit_failed、experimental_failed。
3. 找出最佳、次优、失败但有信息价值的配方。
4. 输出可用于 DOE skeleton 的诊断信号。

不要让 Audit Agent 设计新配方。

### 12.2 DOE Planning Agent

职责：

1. 读取代码生成的 constrained DOE skeleton。
2. 解释每个 skeleton 为什么合理。
3. 给每个候选分配优先级。
4. 写 if_better 和 if_worse。
5. 不允许修改 skeleton 的设计类型和父配方。

不要让 DOE Planning Agent 自由添加新候选。

### 12.3 Formula Generation Agent

职责：

1. 将已批准 skeleton 转换为完整配方。
2. 复制父配方中所有 fixed_variables。
3. 只修改 skeleton 中明确允许修改的变量。
4. 补全实验步骤和安全注意事项。
5. 不得改变设计意图。

对于 baseline_reproduction，建议不调用 LLM，直接用代码复制。

---

## 13. 最小可行修改方案

如果时间有限，建议先完成以下最小版本。

### 13.1 必须完成

1. 新增 parent_candidate_id 强制检查。
2. 新增 baseline_reproduction 完全一致检查。
3. 新增每个候选 changed_variables 自动检测。
4. 限制 local_optimization 最多改变 2 个变量。
5. 限制 single_factor_perturbation 只能改变 1 个变量。
6. 限制每轮最多 1 个 limited_exploration。
7. 强制所有候选含 PVA。
8. 把 audit failure 和 experimental failure 分开记录。
9. 每轮输出配方继承关系表。
10. 每个候选必须有 if_better 和 if_worse。

### 13.2 应该完成

1. 新增 constrained_doe.py。
2. 新增 experiment budget 管理。
3. 新增黑盒跳跃风险评分。
4. 修改 prompts_agents.yaml，让 LLM 解释 skeleton，而不是自由生成配方。
5. 将 baseline_reproduction 改为代码直接复制。

### 13.3 可选完成

1. 新增 judge agent。
2. 新增机制假设库。
3. 新增自动绘制实验迭代树。
4. 新增每轮 markdown 报告导出。
5. 新增 RAG 检索历史最佳配方和失败配方。

---

## 14. Codex 执行建议

请 Codex 按以下顺序修改项目。

### Step 1：先检查现有代码

阅读以下文件：

```text
cycle/src/pva_work_flow/workflow.py
cycle/src/pva_work_flow/generator.py
cycle/src/pva_work_flow/pipeline_agents.py
cycle/src/pva_work_flow/rule_checker.py
cycle/src/pva_work_flow/candidate_critic.py
cycle/src/pva_work_flow/experiment_state.py
cycle/src/pva_work_flow/experiment_rag.py
cycle/src/pva_work_flow/prompts_agents.yaml
cycle/src/pva_work_flow/prompts_en.yaml
cycle/src/pva_work_flow/docs/rules/model_iteration_evaluation.md
```

确认当前是否已经实现：

1. parent_candidate_id 检查。
2. baseline 完全复制。
3. changed_variables 检测。
4. design_type 分布约束。
5. audit failure 和 experimental failure 区分。
6. experiment budget 管理。
7. limited_exploration 数量限制。

### Step 2：实现规则检查优先

优先修改 rule_checker.py 和 candidate_critic.py。先保证错误候选进不了下一轮。

### Step 3：再修改生成逻辑

修改 generator.py 和 pipeline_agents.py，使 LLM 不再从零生成完整配方，而是基于 DOE skeleton 补充解释和细节。

### Step 4：再修改提示词

修改 prompts_agents.yaml 和 prompts_en.yaml，让所有 prompt 都服从“constrained experimental planning”定位。

### Step 5：最后增加报告输出

在每轮输出中增加：

1. 父子关系表。
2. 变量变化表。
3. if_better / if_worse 表。
4. 黑盒跳跃风险表。
5. 实验预算剩余表。

---

## 15. 验收标准

修改完成后，系统应满足以下标准。

### 15.1 功能验收

1. R2 及之后每个候选都有 parent_candidate_id。
2. baseline_reproduction 与父配方完全一致。
3. local_optimization 最多改变 2 个变量。
4. single_factor_perturbation 只改变 1 个变量。
5. 每轮最多 1 个 limited_exploration。
6. 所有候选必须含 PVA。
7. 每个候选都有 if_better 和 if_worse。
8. 每个候选都有 expected_mechanism 和 risks_and_mitigations。
9. 系统能统计已用实验数量和剩余预算。
10. 系统能拒绝黑盒跳跃候选。

### 15.2 行为验收

给定上一轮最佳配方 R1-04 为 PVA + GA + HCl + CMC 且 COF = 0.008 时，系统不应生成以下错误行为：

1. baseline_reproduction 删除 CMC。
2. 第二轮全部改成 PVA + GA + HCl 简化配方。
3. 把 audit failure 当作凝胶失败。
4. 同时改变 PVA、GA、HCl、CMC 和后处理。
5. 引入无关的非 PVA 光固化体系。
6. 不解释为什么删除或保留关键添加剂。
7. 不写 if_better 和 if_worse。

系统应生成类似以下行为：

1. 一个完全复制 R1-04 的 baseline。
2. 多个围绕 R1-04 的局部优化。
3. 一个单因素验证 CMC 浓度的候选。
4. 一个单因素验证 GA 浓度的候选。
5. 如果有探索型候选，也必须保留 PVA 主体并说明具体原因。

---

## 16. 最终目标

完成上述修改后，项目目标应从：

> 用 14B 模型自主推荐下一轮材料配方。

调整为：

> 用 14B 模型在规则约束下辅助完成 PVA 水凝胶实验数据解释、机制假设生成和下一轮小步 DOE 规划。

这会显著降低项目难度，也更符合最多 100 个湿实验配方的实际约束。

最终系统不需要证明 14B 模型能够自主完成开放式材料发现，而应该证明：

1. 14B 模型可以在强约束下参与材料实验规划。
2. 规则系统可以避免黑盒跳跃。
3. 实验日志可以支持连续迭代。
4. 有限实验预算下仍能形成清晰的优化路径。
5. 每一轮建议都有来源、有理由、有变量控制、有判断路径。
