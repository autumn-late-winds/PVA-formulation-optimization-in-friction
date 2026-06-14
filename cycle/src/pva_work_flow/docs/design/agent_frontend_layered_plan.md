# PVA Workflow Agent 前端化分层计划

## 1. 背景

当前项目已经有两类比较稳定的使用入口：

1. 原有的可复现实验流程入口：
   - `pva_vllm.sh`
   - `pva_vllm_dryrun.sh`
   - `python -m pva_work_flow.cli ...`

2. 新增的外层 agent 入口：
   - `python -m pva_work_flow.cli --agent ...`
   - `python -m pva_work_flow.cli --agent_execute ...`

这说明目前的 agent 化并没有大幅改变主流程的使用方式。它主要增加了状态判断、下一步建议、风险策略和低风险工具执行能力，但这些能力仍然主要通过 CLI 文本输出暴露。

因此，下一步做前端是合理的。前端可以把 agent 的价值显性化，让用户不必记住大量命令，而是通过一个本地控制台理解状态、确认动作、推进流程。

## 2. 核心判断

前端值得做，而且应该先做成“实验流程控制台”，而不是优先做成通用聊天机器人。

推荐的第一形态是：

```text
实验流程控制台
  + Agent 状态与建议
  + Workflow 当前阶段
  + 配方树
  + 候选配方比较
  + 工具执行按钮
  + 运行日志
```

CLI 继续负责可复现执行；前端负责可操作性；agent 负责状态判断、动作推荐和工具编排。

## 3. 目标使用体验

理想情况下，用户打开一个本地网页后，就能直接看到：

1. 当前使用的 `out_dir`。
2. 最新实验轮次。
3. 当前流程是在等待生成、等待 wet-lab prepare、等待实验结果、等待诊断，还是等待报告刷新。
4. agent 推荐的下一步。
5. 哪些动作可以安全直接执行。
6. 哪些动作需要用户确认。
7. 上一次动作生成了哪些文件。
8. 配方树是否在推进，是否出现收敛趋势。

前端的目标不是隐藏实验过程，而是让现有实验流程更容易被操作和理解。

## 4. 分层架构

### 4.1 第 0 层：Artifact 层

这一层就是现有的文件系统状态。

职责：

1. 保存生成的配方。
2. 保存 wet-lab 准备文件。
3. 保存 wet-lab 实验结果。
4. 保存诊断结果。
5. 保存配方树报告。
6. 保存 memory 和 RAG 相关产物。

典型结构：

```text
out_dir/
  R1_*
  R2_*
  trees/
  reports/
  memory/
  archive/
```

设计原则：

浏览器前端不应该直接操作这些文件，而应该通过后端 API 读取和触发操作。

### 4.2 第 1 层：Existing Workflow 层

这一层是项目已有的科学工作流。

职责：

1. 生成候选配方。
2. 准备 wet-lab 文件。
3. 读取或整理 wet-lab 结果。
4. 运行诊断。
5. 刷新报告。
6. 构建配方树。
7. 构建 memory 和 vector index。

相关模块：

```text
pva_work_flow.cli
pva_work_flow.orchestration.workflow
pva_work_flow.planning.*
pva_work_flow.wetlab.*
pva_work_flow.tree.*
pva_work_flow.memory.*
```

设计原则：

不要在前端或 API 层重复实现科学工作流逻辑。后端应该调用已有的 workflow 和 agent 函数。

### 4.3 第 2 层：Agent 层

这一层是已经新增的外层操作 agent。

职责：

1. 检查当前项目状态。
2. 判断当前 workflow 阶段。
3. 推荐下一步动作。
4. 将动作映射到工具。
5. 应用风险策略。
6. 自动执行允许的低风险工具。
7. 生成适合操作者阅读的报告。

已有入口：

```text
python -m pva_work_flow.cli --agent --out_dir ...
python -m pva_work_flow.cli --agent_execute --out_dir ...
```

设计原则：

前端应该把 agent 作为操作判断来源，不应该在前端里另写一套状态机。

### 4.4 第 3 层：Agent API 层

这一层是浏览器和 Python 包之间的本地服务。

推荐实现：

```text
FastAPI
```

职责：

1. 暴露当前 workflow 状态。
2. 暴露 agent 推荐。
3. 暴露可用工具和风险等级。
4. 执行用户确认后的动作。
5. 返回或流式显示命令日志。
6. 读取配方树摘要。
7. 读取候选配方和结果摘要。

建议模块：

```text
pva_work_flow.agent_server
```

建议 API 形态：

```text
GET  /api/state?out_dir=...
GET  /api/agent/report?out_dir=...
GET  /api/tree?out_dir=...
GET  /api/candidates?out_dir=...&round=...
GET  /api/logs?out_dir=...
POST /api/tools/execute
POST /api/workflow/generate
POST /api/workflow/prepare
POST /api/workflow/diagnose
POST /api/reports/refresh
```

设计原则：

API 层应该薄而清晰。它只负责把 HTTP 请求转换成已有 Python 调用或 CLI 等价操作。

### 4.5 第 4 层：Frontend UI 层

这一层是本地 Web 控制台。

推荐实现：

```text
Vite + React
```

职责：

1. 展示项目状态。
2. 展示 agent 推荐。
3. 展示 workflow 进度。
4. 展示配方树。
5. 展示候选配方对比。
6. 为允许执行的动作提供按钮。
7. 对中高风险动作弹出确认。
8. 展示运行日志和生成文件。

设计原则：

第一屏应该是实际可用的控制台，而不是介绍页或 landing page。

## 5. 任务层次

### 5.1 第一层任务：只读可视化

目标：

让当前 workflow 状态能在浏览器里被看到。

任务：

1. 增加本地 API server。
2. 增加项目状态接口。
3. 增加 agent 报告接口。
4. 增加配方树摘要接口。
5. 搭建简单前端 dashboard。

完成标准：

用户打开网页后，不运行 CLI 也能理解当前 workflow 状态。

风险：

低。这一阶段只读取 artifact 和 agent 状态。

### 5.2 第二层任务：低风险工具执行

目标：

允许前端执行安全的维护类动作。

任务：

1. 增加低风险工具按钮。
2. 所有执行都经过现有 agent policy。
3. 显示工具执行状态和输出摘要。
4. 执行后刷新 dashboard 状态。

候选工具：

```text
refresh_reports
build_failure_memory
build_vector_index
```

完成标准：

用户可以在浏览器中刷新报告、重建 memory 或重建 vector index。

风险：

低到中。这些动作会写入派生产物，但不会直接改变实验决策。

### 5.3 第三层任务：引导式流程推进

目标：

把生成、prepare、诊断这些动作做成有确认机制的流程按钮。

任务：

1. 增加 generate action 表单。
2. 增加 prepare action 表单。
3. 增加 diagnose action 表单。
4. 执行前展示命令预览。
5. 中风险动作必须确认。
6. 记录动作历史。

候选动作：

```text
生成下一轮候选
准备 wet-lab 文件
运行诊断
归档并重新生成某一轮输出
```

完成标准：

用户可以通过前端推进常规 workflow，同时清楚看到实际会运行什么命令。

风险：

中。这些动作会影响实验规划和输出目录。

### 5.4 第四层任务：候选配方审阅区

目标：

让生成后的候选配方更容易比较，再决定是否进入 wet-lab prepare。

任务：

1. 解析候选配方摘要。
2. 展示组分差异。
3. 展示约束检查结果。
4. 展示模型给出的 rationale 和风险。
5. 支持用户选择或排除候选。
6. 将选中的候选传给 prepare 流程。

完成标准：

用户可以可视化审阅候选配方，而不是手动翻很多生成文件。

风险：

中。界面不能过度简化科学约束，也不能隐藏重要警告。

### 5.5 第五层任务：配方树与收敛视图

目标：

让多轮优化过程可视化。

任务：

1. 渲染配方树节点。
2. 展示父子关系。
3. 展示 COF、modulus、stability、stick-slip 和收敛指标。
4. 高亮较优分支。
5. 标记失败或高风险分支。
6. 节点能链接到对应 artifact。

完成标准：

用户可以一眼理解优化轨迹，而不是只看零散文件。

风险：

中。树可视化必须忠实于 artifact 数据。

### 5.6 第六层任务：交互式 Agent Assistant

目标：

在结构化控制台足够有用之后，再加入对话助手。

任务：

1. 让助手解释当前状态。
2. 让助手回答某个候选配方的问题。
3. 让助手解释为什么推荐某一步。
4. 允许助手提出动作，但不能静默执行高风险动作。
5. 所有执行仍然走同一套 tool policy。

完成标准：

用户可以询问 agent 为什么建议某一步，但真正执行仍通过明确的结构化控制。

风险：

中到高。自由对话不能绕过 policy，也不能破坏可复现性。

## 6. 推荐第一版范围

第一版前端建议只做：

1. 本地 API server。
2. Dashboard。
3. Agent 状态与建议面板。
4. 低风险工具按钮。
5. 配方树摘要。
6. 运行日志面板。

第一版暂时不做：

1. 完整聊天界面。
2. 直接管理 vLLM 服务。
3. 自动执行高风险动作。
4. 复杂配方可视化编辑。
5. 多用户认证系统。

这样第一版既有用，又不会把范围铺得太大。

## 7. 建议第一屏

第一屏应该是信息密度较高的操作 dashboard：

```text
------------------------------------------------------------
Project: src/sft_qwen3_14b_out        State: awaiting_results
Latest round: R2                      Agent: prepare complete
------------------------------------------------------------

[Agent Recommendation]
Next action: import wet-lab results, then run diagnosis
Risk: medium
Reason: R2 candidates exist and preparation files were generated

[Actions]
Refresh Reports
Build Failure Memory
Build Vector Index
Run Diagnosis...

[Formula Tree]
R1-02
  R2-01
  R2-02
R1-03
  R2-03

[Recent Artifacts]
R2_wetlab_plan.csv
tree_summary.md
agent_report.md

[Logs]
latest command output...
```

## 8. 前端中的风险策略

前端应该沿用 agent 层的风险分类。

### 8.1 低风险动作

点击后可以直接执行。

例子：

```text
刷新报告
重建 memory
重建 vector index
读取状态
读取配方树摘要
```

### 8.2 中风险动作

必须展示命令预览，并要求用户确认。

例子：

```text
生成候选配方
准备 wet-lab 文件
运行诊断
归档生成结果
```

### 8.3 高风险动作

不应该由前端静默执行。

例子：

```text
删除实验 artifact
覆盖历史轮次
全局修改约束
不预览就启动长时间 vLLM 任务
```

## 9. 与现有脚本的关系

前端不应该替代 `pva_vllm.sh`。

推荐关系：

```text
pva_vllm.sh
  保留为可复现批处理驱动入口

CLI
  保留为标准可编程接口

Agent
  提供状态判断、动作推荐和风险策略

Frontend
  提供可视化和受控操作
```

早期版本中，前端可以显示等价的 CLI 或脚本命令预览。这样可以保持用户对执行过程的信任，也方便复现实验。

## 10. 实施路线

### Phase 1：后端只读 API

交付物：

1. `pva_work_flow.agent_server`
2. `/api/state`
3. `/api/agent/report`
4. `/api/tree`
5. 基础 JSON schema

完成条件：

浏览器或 `curl` 可以读取当前状态和 agent 推荐。

### Phase 2：Dashboard UI

交付物：

1. Vite 前端脚手架。
2. 项目状态面板。
3. Agent 推荐面板。
4. 最近 artifact 面板。
5. 基础日志面板。

完成条件：

用户可以从一个浏览器页面理解当前 workflow 状态。

### Phase 3：低风险动作

交付物：

1. 工具执行接口。
2. 低风险工具按钮。
3. 执行结果展示。
4. 执行后自动刷新状态。

完成条件：

用户可以从前端刷新报告和重建 memory。

### Phase 4：中风险引导动作

交付物：

1. Generate 表单。
2. Prepare 表单。
3. Diagnosis 表单。
4. 带命令预览的确认弹窗。
5. 动作历史。

完成条件：

用户可以从前端推进 workflow，并且清楚知道实际执行命令。

### Phase 5：候选配方与配方树工作区

交付物：

1. 候选配方比较表。
2. 配方组分差异视图。
3. 配方树可视化。
4. 指标趋势面板。
5. 从 UI 节点跳转到 artifact。

完成条件：

用户可以可视化检查优化进展和候选质量。

### Phase 6：对话式助手

交付物：

1. 感知当前状态的 assistant panel。
2. 候选配方解释。
3. 推荐动作解释。
4. 遵守 policy 的 tool proposal flow。

完成条件：

用户可以询问 agent 为什么推荐某一步，但执行仍通过明确的结构化控制。

## 11. 成功标准

前端化工作成功的标志是：

1. 用户不需要记住大量命令，也能推进常规 workflow。
2. 所有生成类动作都有 CLI 等价命令预览，保持可复现。
3. 低风险维护动作可以一键执行。
4. 中风险动作必须确认。
5. 配方树比直接阅读原始文件更容易理解。
6. 前端不重复实现科学 workflow 逻辑。
7. 现有脚本和 CLI 仍然可用。

## 12. 最终建议

建议做前端，但第一步应该做“分层控制台”，而不是直接做完整聊天 agent。

推荐顺序：

```text
只读 dashboard
  -> 低风险工具按钮
  -> 引导式 workflow 动作
  -> 候选配方审阅
  -> 配方树可视化
  -> 对话式 assistant
```

这个顺序可以让 agent 的价值尽快变得可见，同时保持实验流程的可复现性和科学控制边界。
