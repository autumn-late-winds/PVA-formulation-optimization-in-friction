# PVA 项目 Agent 化可行性与分层建设方案

## 1. 结论

这个项目适合做成 Agent，但不建议做成“让大模型自由接管实验设计”的 Agent。更合适的形态是：

```text
外层实验优化 Agent
  负责理解目标、检查工作区状态、选择下一步动作、调用工具、组织人工确认

内层受约束 workflow
  继续使用现有 generate / audit / prepare / diagnose / RAG / tree 代码
  负责可追踪配方生成、硬规则审计、实验数据解析和诊断
```

也就是说，Agent 化的重点不是把现有代码推倒重写，而是把当前已经形成的闭环能力包装成一个“会看状态、会选工具、会停下来问人、会恢复运行”的实验助手。

当前代码已经具备 Agent 化的核心地基：

- `cli.py` 已经是统一命令入口，覆盖生成、审计、准备实验、诊断、同步结果、状态检查、RAG 索引等动作。
- `pipeline_agents.py` 已经实现 R2+ 的 3-Agent 流水线：Audit Agent、DOE Planning Agent、Formula Generation Agent。
- `constrained_doe.py`、`formula_materializer.py`、`candidate_rules.py`、`rule_checker.py` 已经把高风险决策压到代码层。
- `artifact_store.py` 已经有工作区状态识别和 next action 推荐的雏形。
- `formula_tree.py`、`tree_statistics.py`、`tree_reports.py` 已经提供配方树、跨树统计和实验者报告。
- `experiment_rag.py`、`failure_factor_memory.py`、`vector_rag.py`、`formulation_rag.py` 已经有项目记忆和文献 RAG 的接口基础。

因此，Agent 化是可行的，且最好采用渐进式封装。

## 2. Agent 化目标

Agent 化后，系统应该从“命令行工作流”升级为“实验优化协作者”：

```text
用户说：帮我看看这个 run 下一步该做什么
Agent 应该：
1. 读取工作区状态
2. 判断当前处于生成、实验、结果同步、诊断、树扩展或收敛检查的哪一步
3. 给出下一步动作
4. 必要时调用现有工具执行
5. 在高风险动作前要求人工确认
6. 输出人能读懂的实验计划或诊断结论
```

Agent 不应该替代湿实验人员，也不应该绕过现有规则直接生成配方。它的价值在于降低操作复杂度、减少漏步骤、保留可追踪性。

## 3. 可行性分析

### 3.1 高可行部分

这些能力已有代码基础，适合第一阶段 Agent 化：

| 能力 | 现有基础 | Agent 化方式 |
|---|---|---|
| 工作区状态判断 | `RunWorkspace.format_status_report()` / `next_action()` | 封装成 `inspect_workspace` 工具 |
| 生成下一轮候选 | `run_generator()` / `cli --mode generate` | 封装成 `generate_round` 工具 |
| 审计并导出实验模板 | `run_auditor_rulebased()` / `run_prepare_wetlab()` | 封装成 `prepare_wetlab` 工具 |
| Bruker 数据同步 | `--sync_results` / `build_results_from_bruker_csvs` | 封装成 `sync_results` 工具 |
| 诊断实验结果 | `run_diagnose()` | 封装成 `diagnose_round` 工具 |
| 配方树与统计报告 | `formula_tree.py` / `tree_statistics.py` / `tree_reports.py` | 封装成 `refresh_reports` 工具 |
| RAG 记忆索引 | `--build_rag_vector_index` / `--query_rag_vector` | 封装成 `build_memory` / `query_memory` 工具 |

### 3.2 中等可行部分

这些能力能做，但需要补一层状态机和人工确认：

| 能力 | 难点 | 建议 |
|---|---|---|
| 自动选择 root tree 展开 | 需要根据 COF、branch_status、预算、失败因子综合判断 | 先只推荐，不自动执行 |
| 自动判定是否收敛 | 现有 `convergence` 字段可用，但真实实验目标可能变化 | 输出结论后要求人工确认 |
| 自动生成多轮实验计划 | 容易越过湿实验实际进度 | 只规划 1 轮，最多给 2-3 轮路线图 |
| 文献 RAG 驱动新材料建议 | 材料采购和安全性不完全在代码中 | 新材料只作为 proposal，不进入默认候选 |

### 3.3 不建议第一阶段做的部分

这些方向风险高，容易破坏项目当前最重要的可追踪性：

- 让 Agent 自由修改 `R{N}_candidates.json`。
- 让 Agent 自动开启 `limited_exploration` 引入新材料。
- 让 Agent 自动跳过 `target_parent_id`，混合多个父节点生成 R2+。
- 让 Agent 直接根据自然语言生成完整配方，绕过 `constrained_doe.py`。
- 让 Agent 自动删除或覆盖旧轮次产物。
- 把文献先验的优先级放到项目真实实验结果之上。

## 4. 分层架构

推荐分成 6 层。每一层都应有明确输入、输出和责任边界。

```text
L0. 领域边界层
L1. 工具封装层
L2. 工作区状态层
L3. Agent 决策层
L4. 人机协作层
L5. 评估与学习层
```

### L0. 领域边界层

目标：定义 Agent 永远不能越过的领域约束。

这一层主要复用现有规则和文档：

- PVA 必须保持主体系。
- R2+ 必须有 `parent_candidate_id`。
- `target_parent_id` 指定时，只能展开一个父节点。
- `audit_status` 与 `experimental_status` 分离。
- 跨树统计只能作为先验，不能改变父子继承关系。
- 新材料探索默认关闭。
- 高风险分支需要人工确认。

建议新增一个 Agent 可读的策略文件：

```text
docs/agent/agent_policy.md
```

其中只放不可违反的规则，不放实现细节。

### L1. 工具封装层

目标：把现有 Python/CLI 能力包装成稳定工具。

建议每个工具都采用统一结构：

```python
{
  "tool_name": "...",
  "inputs": {...},
  "outputs": {...},
  "artifacts_written": [...],
  "requires_confirmation": true | false,
  "risk_level": "low | medium | high"
}
```

第一批工具建议如下：

| 工具名 | 对应现有能力 | 风险 |
|---|---|---|
| `inspect_workspace` | `RunWorkspace.format_status_report()` | low |
| `sync_results` | `cli --sync_results` | medium |
| `diagnose_round` | `cli --mode diagnose` | medium |
| `generate_round` | `cli --mode generate` | high |
| `prepare_wetlab` | `cli --mode prepare` | medium |
| `refresh_reports` | tree/report/statistics rebuild | low |
| `build_failure_memory` | `--build_failure_memory` | low |
| `build_vector_index` | `--build_rag_vector_index` | low |
| `query_memory` | `--query_rag_vector` | low |

第一阶段可以不用引入复杂 Agent 框架，先写一个 `agent_tools.py` 作为本地函数封装。

### L2. 工作区状态层

目标：让 Agent 先判断“现在在哪里”，再决定“下一步做什么”。

现有 `artifact_store.py` 已经做了一部分，建议扩展为更明确的状态机：

```text
empty_workspace
r1_candidates_ready
wetlab_template_ready
raw_csv_ready
results_synced
diagnosis_ready
ready_for_next_round
tree_branch_blocked
converged_candidate_found
needs_human_review
```

每个状态给出：

- 已有文件
- 缺失文件
- 推荐命令
- 是否需要人工输入
- 是否允许自动执行

例如：

```json
{
  "state": "raw_csv_ready",
  "round": 2,
  "evidence": ["R2/ contains 8 friction CSV files", "R2_results_filled.csv missing"],
  "recommended_action": "sync_results",
  "safe_to_auto_run": true
}
```

### L3. Agent 决策层

目标：把“状态 → 动作”的选择变成可解释的决策。

推荐使用有限状态机，而不是完全开放式规划器。Agent 的决策逻辑应类似：

```text
1. inspect_workspace
2. 如果有 raw CSV 且缺 results_filled，则 sync_results
3. 如果 results_filled 存在但缺 diagnosis，则 diagnose_round
4. 如果 diagnosis 存在且未收敛，则推荐 generate_round
5. 如果目标父节点是 kill，则拒绝生成并推荐换 branch
6. 如果涉及新材料、重生成、归档、覆盖，则要求人工确认
```

决策层可以输出两种模式：

```text
advisory mode：只建议，不执行
operator mode：低风险自动执行，中高风险先确认
```

第一阶段建议默认 advisory mode，避免误操作真实实验数据。

### L4. 人机协作层

目标：明确哪些地方必须停下来问人。

必须人工确认的动作：

- 生成 R2+ 候选，尤其是指定 `target_parent_id` 时。
- 重生成某一轮并归档旧产物。
- 开启 `limited_exploration`。
- 使用新材料或新交联体系。
- 认定某条 branch 为 `kill` 或 rescue 成功。
- 宣布项目收敛并停止实验。
- 修改收敛阈值。

Agent 应该给出简洁问题，而不是把所有责任交给用户：

```text
我建议下一步展开 root-04，因为它当前 best_cof 最低且 branch_status=continue。
这会生成 R3 的 4 个局部分支，不会引入新材料。
是否执行？
```

### L5. 评估与学习层

目标：让 Agent 化后能被评估，而不是只看“能不能跑”。

建议建立 Agent 级指标：

| 指标 | 含义 |
|---|---|
| `action_accuracy` | 推荐动作是否符合工作区真实状态 |
| `unsafe_action_block_rate` | 是否阻止了高风险错误动作 |
| `lineage_preservation_rate` | 生成后候选是否保持父子追踪 |
| `manual_intervention_count` | 每轮需要用户介入多少次 |
| `round_completion_time` | 从 CSV 到诊断完成耗时 |
| `branch_decision_consistency` | continue/rescue/kill 与结果是否一致 |

这些指标可以先写进 Markdown 报告，后续再结构化成 JSON。

## 5. 推荐目录结构

不建议把 Agent 代码散落到现有核心 workflow 中。建议新增轻量目录：

```text
cycle/src/pva_work_flow/
  agent/
    __init__.py
    policy.py              # 读取 agent_policy.md 和硬规则摘要
    state_machine.py       # 工作区状态识别
    tools.py               # 对现有 CLI/函数的工具封装
    planner.py             # 状态 -> 推荐动作
    executor.py            # 执行动作，处理确认与日志
    reports.py             # Agent 决策报告
```

配套文档：

```text
cycle/src/pva_work_flow/docs/agent/
  agent_policy.md
  tools.md
  state_machine.md
  operator_manual.md
```

第一阶段可以只实现 `state_machine.py`、`tools.py`、`planner.py`，不需要复杂框架。

## 6. MVP 路线

### 阶段 1：只读 Agent

目标：不改任何实验产物，只读状态并给建议。

功能：

- 读取 run directory。
- 输出当前状态。
- 列出缺失文件。
- 推荐下一步命令。
- 解释为什么推荐这一步。
- 对高风险状态给出警告。

验收标准：

- 对空目录、已有 R1、已有 CSV、已有 results、已有 diagnosis 的不同状态都能给出正确建议。
- 不写入任何文件。

### 阶段 2：低风险执行 Agent

目标：允许自动执行低风险、可重复的构建动作。

可自动执行：

- `refresh_reports`
- `build_failure_memory`
- `build_vector_index`
- `query_memory`

谨慎自动执行：

- `sync_results`，因为它会写 `R{N}_results_filled.csv`，但来源是原始 CSV，可复现。

仍需确认：

- `generate_round`
- `prepare_wetlab`
- `regenerate_round`

### 阶段 3：受控操作 Agent

目标：Agent 可以在确认后执行完整下一步。

典型流程：

```text
用户：帮我推进这个 run
Agent:
1. inspect_workspace
2. 发现 R2 CSV 已放入，但未同步
3. 执行 sync_results
4. 刷新 tree reports 和 failure memory
5. 发现 R2_results_filled 存在但未诊断
6. 询问是否运行 diagnose
7. 诊断后建议下一步展开哪个 parent
```

### 阶段 4：研究助理 Agent

目标：把文献 RAG、失败因子记忆和树统计结合起来，给出下一轮策略建议。

此阶段 Agent 可以回答：

- 哪棵 root tree 最值得继续？
- 哪些失败因子已经确认？
- 哪些变量只是假设，还需要单因素验证？
- 下一轮应该优先 exploitation 还是 rescue？
- 是否需要新材料探索，理由是什么？

仍然不建议它自动开启新材料探索。

## 7. 外层 Agent 与内层 3-Agent 的关系

当前 `pipeline_agents.py` 的 3-Agent 是“生成链内部的角色拆分”：

```text
Audit Agent -> DOE Planning Agent -> Formula Generation Agent
```

本文建议的外层 Agent 是“项目操作层”：

```text
Workspace Agent -> Tool Planner -> Human Confirmation -> Executor -> Report
```

二者不要混淆：

- 内层 3-Agent 负责某一轮候选生成。
- 外层 Agent 负责决定现在是否应该生成、诊断、同步结果、刷新报告、查询记忆。

外层 Agent 不应该绕过内层 3-Agent 直接写候选配方。

## 8. 建议的 Agent 指令骨架

外层 Agent 的系统指令可以非常短，但必须硬：

```text
你是 PVA 水凝胶闭环实验优化项目的操作 Agent。

你只能通过已注册工具读取状态、同步结果、诊断、生成候选、刷新报告和查询记忆。
你不能直接编辑候选配方 JSON。
你不能绕过 constrained DOE 生成 R2+ 配方。
你不能自动引入新材料。
你必须区分 audit failure 和 experimental failure。
你必须保持 parent_candidate_id 继承关系。
任何覆盖、归档、重生成、新材料探索、收敛终止都必须先请求人工确认。
```

## 9. 风险与缓解

| 风险 | 表现 | 缓解 |
|---|---|---|
| Agent 越权生成配方 | 直接写 JSON 或跳过 constrained DOE | 工具层禁止写候选，只允许调用现有生成流程 |
| 误判实验状态 | 缺文件时继续下一轮 | 状态机必须检查必需 artifact |
| 把审计失败当实验失败 | 有 COF 数据却说未成胶 | 复用现有 audit/experimental 分离规则 |
| 自动覆盖历史产物 | 重生成导致旧结果丢失 | 归档必须显式确认 |
| 文献先验压过实验结果 | 明明项目数据失败仍推荐 | prompt 中固定“项目实验优先” |
| 多树继承混乱 | 用 root-* 当 parent_candidate_id | 策略层禁止，状态检查验证 |

## 10. 第一批可写文档

如果先不改代码，建议下一步继续补 4 个文档：

```text
docs/agent/agent_policy.md
docs/agent/state_machine.md
docs/agent/tools.md
docs/agent/operator_manual.md
```

其中：

- `agent_policy.md` 写不可违反的项目规则。
- `state_machine.md` 写每个状态如何识别、下一步是什么。
- `tools.md` 写每个工具的输入输出、风险等级、是否需要确认。
- `operator_manual.md` 写用户如何和 Agent 交互。

## 11. 推荐最小实现顺序

```text
1. 新增只读状态机：根据 artifact 判断当前状态。
2. 把 RunWorkspace.next_action() 升级为结构化 planner 输出。
3. 新增 tools.py，把现有 CLI 能力包装成函数。
4. 新增 advisory agent：只输出建议，不执行。
5. 增加低风险执行：refresh_reports / build_memory。
6. 增加人工确认后的 sync_results / diagnose。
7. 最后再支持确认后的 generate_round。
```

## 12. 最终形态

理想状态下，用户不需要记住全部命令，只需要说：

```text
检查这个 run，告诉我下一步。
```

Agent 输出：

```text
当前状态：R2 原始摩擦 CSV 已放入，压缩 CSV 已放入，但 R2_results_filled.csv 尚未生成。
建议动作：同步实验结果。
原因：诊断依赖 results_filled；当前同步动作可由原始 CSV 可重复生成。
风险：会写入 R2_results_filled.csv，并刷新 tree reports。
```

然后在用户确认后执行同步、刷新报告，再继续判断是否诊断。

这才是本项目最稳妥的 Agent 化路线：Agent 管“操作闭环”，现有代码管“实验约束与配方生成”。
