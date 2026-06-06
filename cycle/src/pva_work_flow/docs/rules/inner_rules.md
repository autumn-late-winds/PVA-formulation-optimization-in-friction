内部决策规则：

## 当前代码层实现状态（2026-05-28）

当前项目已经把最容易出错的规则从 prompt 约束下沉到代码层：

- R2+ 默认由 `constrained_doe.py` 生成最多 4 个受限继承条目，不再要求 14B 模型自由设计整轮 DOE。
- R1 可以作为文章数据设计的 root 初始化阶段，建议约 10 个初始配方；R2+ 可以用 `--target_parent_id` 每次只展开一个父配方节点。
- `--target_parent_id` 支持引用更早轮次的节点，例如 `--round 3 --target_parent_id R1-07`，代码会从 ID 推断父轮次并只围绕该节点生成局部分支。
- 当 `R{N}_doe_plan.json` 中 `skeleton_source=code_constrained_doe` 时，`pipeline_agents.py` 会直接复制父配方并只应用 `variables_changed` 中允许的变化；LLM 不再自由改写完整配方。
- `candidate_rules.py` 自动检查 `parent_candidate_id`、PVA 主体系、baseline 完全复现、变量变化数量、`limited_exploration` 数量和黑盒跳跃风险。
- `changed_variables` 采用“语义设计变量”口径，只比较 formulation/process 中的实验变量；`ratio_planner` 重新生成的材料克数、单位、basis 不再被误算为额外变量变化。
- 每轮输出 `R{N}_inheritance_table.md`，作为判断本轮是否可追溯、可解释、可执行的首要文件；跨轮树状谱系由 `formula_tree.md` 汇总，机器可读分支判定由 `formula_branch_decisions.json` 保存。
- 多棵树之间可以共享统计知识，但不能共享父节点继承关系。`tree_statistics.json/md` 与 `tree_memory_cards.jsonl` 只能作为统计先验/RAG 上下文，不能替代 `parent_candidate_id`。

---

在生成任何新配方前，先判断它是否满足以下四个条件：

1. 它是否有明确母配方或明确来源结论？
2. 它是否只改变了 1 到 2 个关键变量？
3. 它是否能回答一个明确实验问题？
4. 它是否有助于降低摩擦系数或提高低摩擦体系的稳定性？

只有四个条件全部满足，才能进入候选配方列表。

如果一个配方只是因为"文献中常见""可能有帮助""看起来合理"而被提出，但不能追溯到上一轮实验结果、材料机理假设或用户允许的探索方向，则必须删除。

你必须优先生成"可解释的下一轮 DOE"，而不是"看起来丰富的配方集合"。

---

## 硬性规则（违反即拒绝）

### 规则 1：审计失败 ≠ 实验失败

- **审计失败 (audit failure)**：配方格式不完整（缺少材料用量、单位、basis 等），不意味着实验结果差
- **实验失败 (experimental failure)**：凝胶不成型、摩擦系数过高、磨损严重等
- 严禁将审计不通过等同于"凝胶失败"或"性能不好"
- 在 diagnosis 中引用实验结果时，必须使用实际实验数据（COF、friction_pattern 等），而非审计状态
- 如果审计失败但实验仍获得了数据，实验数据仍然有效，必须作为下一轮设计依据

### 规则 2：expected_mechanism 不得为空

- 每个候选配方必须至少包含 1 个 expected_mechanism，说明期望通过什么机制改善性能
- R1 候选也必须填写，至少说明"该添加剂/交联方式期望通过 X 机制降低摩擦/提高稳定性"
- 空数组 [] 直接导致该候选被拒绝

### 规则 3：risks_and_mitigations 至少 2 条

- 每个候选配方必须至少包含 2 个风险及缓解措施
- 风险应具体到该配方的材料/工艺特点，不允许全是通用风险

### 规则 4：baseline_reproduction 必须精确复制

- baseline_reproduction 类型的候选配方必须与母配方完全一致
- 不允许删除母配方中的添加剂、改变交联剂浓度、更换溶剂等
- 唯一允许的差异是 candidate_id
- 如果母配方的最佳候选使用了 CMC 添加剂，reproduction 配方也必须包含 CMC
- 当前代码会自动比较父/子配方并写入 `changed_variables`；baseline 的 `changed_variables` 必须为空

### 规则 5：下一轮判断规则

- 每个候选配方必须预设结果解释路径：
  - 如果性能改善 → 什么因素可能起作用，下一轮如何继续
  - 如果性能变差 → 什么因素可能导致失败，下一轮如何调整
- DOE Planning Agent 的 inheritance_table 中 if_better/if_worse 字段为必填

### 规则 6：parent_candidate_id 强制有效

- R2 及之后每个候选必须有 `parent_candidate_id`
- `parent_candidate_id` 必须来自已经完成的历史候选，不能引用当前轮候选
- 在树模式下，父节点不必来自上一轮；`--target_parent_id R1-04` 可以在后续任意轮次继续展开 R1-04 这棵树
- limited_exploration 也必须有父配方；它是“从某个父配方出发做有限探索”，不是无父来源的新体系

### 规则 7：变量数量限制

- `local_optimization` 最多改变 2 个变量
- `single_factor_perturbation` 必须正好改变 1 个变量
- `failure_verification` 最多修复 1 个可能失败因素
- `baseline_reproduction` 不允许改变任何变量
- `limited_exploration` 每轮最多 1 个，且最多引入 1 个新材料或 1 个新处理方式

### 规则 8：PVA 主体系约束

- 所有候选必须保留 PVA 作为主体聚合物
- 不允许从 PVA 水凝胶跳到无 PVA 的全新材料体系，除非人工显式覆盖

### 规则 9：R1 探索范围约束

- R1 首轮筛选不得完全跳出 PVA 体系（主体聚合物必须为 PVA）
- R1 中最多允许 1 个非标准交联体系的探索型配方
- R1 建议集中于：交联剂类型变化（2-3种）+ 添加剂类型变化（2-3种）+ 浓度梯度，而非同时改变所有变量

### 规则 10：每轮继承关系表

- 每轮必须输出配方继承关系表
- 表中至少包含：candidate_id、tree_id、branch_status、parent_candidate_id、design_type、changed_variables、if_better、if_worse、black_box_jump_score
- 如果表中显示变量变化与配方正文不一致，以代码自动检测的 `changed_variables` 为准

### 规则 11：树状优化只展开一个父节点

- 当命令包含 `--target_parent_id` 时，本次生成只允许围绕该父节点生成候选
- 生成结果应包含 1 个 baseline_reproduction 和若干局部分支
- 不允许在同一次树状优化中混入另一棵 root tree 的父节点
- `branch_status` 默认是 active；实验结果回填后，`formula_tree.py` 根据 dCOF 和实验备注推断 `continue`、`rescue_candidate`、`kill`、`hold` 或 `pending`
- 如果某节点已被推断或人工标记为 `kill`，后续不得再用 `--target_parent_id` 展开该分支

### 规则 12：树间统计只能作为先验

- 第 5 棵树可以参考前 4 棵树的统计知识，例如变量 improvement_rate、mean_delta_cof、kill_rate 和 rescue_success_rate
- 统计 RAG 不能让当前候选继承另一棵树的 `parent_candidate_id`
- 如果统计先验建议某个变量更有希望，也必须通过当前父节点的小步变化来验证
- 如果统计先验显示某变量 kill_rate 很高，优先避免该变量，或把它放入明确的 rescue/failure_verification 设计中

---

## 配方合理性提升方向

以下基于 R1→R2 实际迭代中暴露的问题，按优先级排列。每条包含**现象**、**根因**、**目标规则**和**实现位置**。

### 方向 1：Diagnosis Agent 必须感知实验失败

**现象**：`R1_diagnosis.json` 将破裂候选 R1-04（ERROR1）列为 `best_candidates[0]`、标记为 `"good_balance"`。COF=0.008 是破裂前短暂采集的数据，不可靠。

**根因**：`diagnosis` prompt 中未注入 `experiment_notes` 的错误码信息。LLM 仅按 COF 数值排序。

**目标规则**：
- `experimental_status` 为 `mechanically_failed`（ERROR1 破裂 / ERROR2 未成胶）的候选，其 COF 数据标记为 `unreliable_due_to_mechanical_failure`
- diagnosis 输出中，机械失败候选必须单独分组，不得进入 `best_candidates` 或 `performance_class: good_balance`
- 诊断必须明确区分"摩擦性能差但凝胶完整"和"凝胶破坏导致数据无效"

**实现位置**：`workflow.py` 的 `build_notes_context_for_diagnosis()` 已实现注入，但需确认 prompt 中有硬性排序规则；`diagnosis_runner` 的输出解析需增加 `experimental_status` 校验。

---

### 方向 2：父配方选择必须交叉校验实验备注

**现象**：R2 父配方选了 R1-01（COF=0.029），而非 R1-03（HA，COF=0.020，未破裂）。`constrained_doe.py` 的 experiment_notes 过滤逻辑在服务器上未生效。

**根因**：`R1_experiment_notes.json` 在服务器上可能不存在或路径不匹配。`is_candidate_mechanically_failed()` 依赖 JSON 文件存在。

**目标规则**：
- 父配方选择排序键：`(is_mechanically_failed ASC, COF ASC)` — 先排除破裂，再按 COF 排序
- 如果 `experiment_notes.json` 不存在，打印告警并回退到仅按 COF 排序（但标注"未校验机械完整性"）
- 机械失败候选的 COF 数据不作为父配方选择依据

**实现位置**：`constrained_doe.py` 的父配方选择逻辑（已有基础实现，需排查服务器端不生效的根因）；`cli.py` 的 `--sync_results` 可自动同步 notes。

---

### 方向 3：受限 DOE 的可扰动变量池需扩展

**现象**：R1-01（PVA+GA+HCl）作为父配方时，可扰动语义变量只有 GA 浓度和 soak 时间两个。导致 R2-03=R2-05（GA↑ 1.25% 出现两次），R2-02（FT 0→0.0）成为无意义变化。

**根因**：`constrained_doe.py` 的降级链只遍历 `initiator→crosslinker→additive→pva→soak`，但 HCl 作为 catalyst 未被充分利用，PVA 浓度未被扰动。

**目标规则**：
- 降级链扩展为：`primary_additive_wt_percent → crosslinker_wt_percent → initiator_wt_percent → pva_wt_percent → post_soak_hours → reaction_temperature → reaction_time`
- entry N（N≥4）生成后，与 entry 1..N-1 做去重检查：如果 `(formulation, processing)` 语义等价，跳过并重试下一个 lever
- `_numeric_equivalent()` 等价检查也用于 new_value 选择：如果 planned new_value 与 old_value 数值等价（如 0 vs 0.0），跳过该 lever

**实现位置**：`constrained_doe.py` 的 entry 5/6 降级链；`candidate_rules.py` 的去重检查可复用到 `constrained_doe.py`。

---

### 方向 4：配方去重与语义等价检测

**现象**：R2-03 和 R2-05 的 formulation（GA=1.25%）和 processing（soak=1h）完全相同。这两个候选在实验上无法区分。

**根因**：`constrained_doe.py` 的 entry 3（SF#2）和 entry 5（SF#3）选择了同一个变量 lever（`primary_additive_wt_percent`），LHS 采样到的 new_value 恰好相同。

**目标规则**：
- `constrained_doe.py` 中，entry N 的 `planned_changed_variables` 确定后，检查是否与 entry 1..N-1 的**实际配方语义等价**（不只是 changed_variables 列表相同，而是实际数值相同）
- 如果等价，自动切换到降级链的下一个 lever 重新采样
- 如果所有 lever 都耗尽，允许生成少于 MAX 个候选，在 DOE plan 中标注 `"deduplicated: N entries reduced to M unique"`

**实现位置**：`constrained_doe.py` 的 `build_constrained_entries()` 末尾，新增 `_deduplicate_entries()` 步骤；`candidate_rules.py` 的 `candidate_variable_map()` 可作为等价比较基础设施。

---

### 方向 5：无效变量变化检测（no-change guard）

**现象**：R2-02 的 `planned_changed_variables` 是 `freeze_thaw_cycles: 0→0.0`，`changed_variable_names` 为空，但被标记为 `single_factor_perturbation`。审计报 `single_factor_changed_variables_invalid`。

**根因**：DOE skeleton 的降级链选择了 FT cycles，但父配方 FT=0（无冻融），子配方 FT=0.0 数值等价。`_numeric_equivalent()` 跳过了 changed_variables 记录，但 skeleton 仍生成了条目。

**目标规则**：
- skeleton 选择 lever 后，检查 `new_value` 是否与 `old_value` 语义等价（`_numeric_equivalent`）。如果等价，立刻跳过该条目，不消耗候选槽位
- `single_factor_perturbation` 如果 `changed_variables` 为空（被等价检查清空），该候选应降级为 `baseline_reproduction` 或直接丢弃
- 不生成 `design_type=single_factor_perturbation` 但 `changed_variables=[]` 的候选

**实现位置**：`constrained_doe.py` 的每个 entry 生成后、写入前增加 `_validate_entry_has_real_change()`；`formula_materializer.py` 在物化后也做二次校验。

---

### 方向 6：诊断结论的质量门槛

**现象**：R2 的诊断全部是代码模板（`"Code-materialized candidate inherits the parent PVA system..."`），没有针对 COF=0.029 + irregular pattern 的具体机制解释。

**根因**：`formula_materializer.py` 的 auto-fill 只填了通用文本，未调用 LLM 做针对性解释。当前架构下 LLM 只做 audit 和 diagnosis，不做 per-candidate mechanism 解释。

**目标规则**：
- `local_optimization` 和 `single_factor_perturbation` 候选的 `expected_mechanism` 不能只有通用模板
- 至少包含一个基于父配方实验数据的具体机制假设（如 "Increasing GA from 1.0 to 1.25 wt% is expected to increase crosslink density, which may reduce the irregular friction pattern observed in R1-01 by stabilizing the surface layer"）
- 如果代码无法生成具体假设（没有 LLM 调用），标记为 `"mechanism_auto_filled: needs_human_review"` 而非伪装成完整分析

**实现位置**：`generator.py` 的 Plan A auto-fix 逻辑；可选方案是在 diagnosis 阶段让 LLM 为每个 planned change 生成一句话的机制假设，存入 `diagnosis.json`，然后在物化时注入。

---

### 方向 7：审计失败与配方有效性的分离

**现象**：R2 全部 6 个候选 audit_status=FAIL，但 failure 原因全是 `iteration_metadata_incomplete: doe_factor_levels_missing` — 这是审计模板过于严格，不影响配方在实验台上的可执行性。

**根因**：审计规则要求 `doe_factor_levels` 包含所有 `diagnosis.next_round_doe` 中列出的因子，但代码物化的候选不需要这些元数据。

**目标规则**：
- `audit_status=FAIL` 分为两类：
  - `BLOCKER`（配方不可执行）：materials 不完整、PVA 缺失、变量超限、if_better/if_worse 缺失
  - `WARNING`（元数据不完整但不影响执行）：doe_factor_levels 缺失、diagnosis_levers_used 为空、iteration_metadata 缺字段
- `rejection_reason` 从统一的 `materials_incomplete_or_inconsistent` 改为区分 `formulation_invalid` vs `metadata_incomplete`
- CLI `--status` 和 `artifact_store.py` 的 `next_action()` 应区分这两类

**实现位置**：`audit.py` 的 `hard_constraint_failures` 分类逻辑；`artifact_store.py` 的 `format_status_report()`。

---

### 方向 8：实验失败模式的细分

**现象**：当前 `experimental_status` 只有 `not_measured` / `measured` / `failed`，无法区分"凝胶完整但摩擦高" vs "凝胶破裂数据无效" vs "未成胶无数据"。

**根因**：`experiment_notes.py` 引入了 error_codes，但 `experimental_status` 字段未与 error_codes 联动。

**目标规则**：
- `experimental_status` 扩展为：`not_measured` | `measured_valid` | `measured_high_friction` | `measured_unstable` | `mechanically_failed_rupture` | `mechanically_failed_no_gelation` | `data_unusable`
- 从 `experiment_notes.json` 的 `error_codes` 自动推导 `experimental_status`：
  - ERROR1 → `mechanically_failed_rupture`
  - ERROR2 → `mechanically_failed_no_gelation`
  - ERROR3/6 → `data_unusable`
- diagnosis 和 parent selection 只信任 `measured_*` 和 `data_unusable` 状态的 COF 数据

**实现位置**：`experiment_notes.py` 的 `apply_notes_to_candidates()` 已设置 experimental_status，需确保该函数在 diagnosis 和 parent selection 之前被调用；`experiment_errors.yaml` 中增加 `affects_data_validity: true/false` 字段。

---

### 实施优先级建议

| 优先级 | 方向 | 影响 | 工作量 |
|:---:|------|------|:---:|
| P0 | 方向 1: diagnosis 感知实验失败 | 修复"破裂候选被推荐"的致命错误 | 小 (prompt + 解析调整) |
| P0 | 方向 2: 父配方交叉校验 notes | 确保 R3 选 R1-03 而非 R1-01 | 小 (排查服务器路径) |
| P1 | 方向 5: 无效变量变化检测 | 消除 R2-02 类无意义候选 | 小 (skeleton 加校验) |
| P1 | 方向 4: 配方去重 | 消除 R2-03=R2-05 类重复 | 中 (新增去重逻辑) |
| P1 | 方向 3: 可扰动变量池扩展 | 减少未来轮次重复 | 中 (降级链扩展) |
| P2 | 方向 7: 审计分级 | 改善用户体验, 避免误判 | 中 (audit.py 重构) |
| P2 | 方向 6: 诊断质量门槛 | 提升 LLM 解释深度 | 中 (LLM 调用 + 模板改造) |
| P3 | 方向 8: 失败模式细分 | 为长期数据利用打基础 | 中 (experiment_notes 联动)
