# Code Simplification Tree

本文档用 `zoom-out` 和 `improve-codebase-architecture` 的视角，把当前代码分成"主线必须保留""辅助可保留""旧路径可归档""下一步可拆分"四类。目标不是粗暴删文件，而是让项目维护者一眼看懂：真正跑 100 个实验内受限迭代时，哪些代码是主线。

## 当前精简原则

```text
精简项目
|
+-- 不优先删除实验链路代码
+-- 优先收窄默认入口
+-- 优先把大模块拆成更深的 Module
+-- 优先把旧路径标记为可选/遗留
+-- 删除前先用测试和归档保护
```

---

## 1. 主线代码：默认必须理解

```text
受限 PVA 小步迭代主线
|
+-- cli.py
|   +-- 命令入口
|   +-- --status
|   +-- --sync_results
|   +-- --regenerate_round
|   +-- --tree_initial_roots
|   +-- --target_parent_id
|
+-- artifact_store.py
|   +-- 运行工作区状态
|   +-- 轮次文件路径
|   +-- 下一步建议（含 budget 感知）
|   +-- 旧轮次归档
|
+-- budget_manager.py              ← 新增
|   +-- 实验预算计数
|   +-- 4 阶段推断 (screening→local_optimization→mechanism→robustness)
|   +-- round shape 推荐
|   +-- 预算告警
|
+-- constrained_doe.py
|   +-- R2+ 受限 DOE 骨架
|   +-- 支持单父节点树状优化
|   +-- target_parent_id 可锁定一个父配方节点
|   +-- 默认生成 baseline repeat + 少量局部分支
|   +-- 默认关闭 limited_exploration
|
+-- formula_materializer.py
|   +-- 复制父配方
|   +-- 只应用 variables_changed
|   +-- 防止 LLM 自由改写 R2+ 配方
|
+-- candidate_rules.py
|   +-- parent_candidate_id 检查
|   +-- baseline 完全复现检查
|   +-- 语义 changed_variables
|   +-- tree_id / branch_status 继承表字段
|   +-- limited_exploration 限制
|   +-- black_box_jump_score
|
+-- formula_tree.py
|   +-- 跨轮配方树渲染
|   +-- 显示 tree_id、branch_status、dCOF
|   +-- 用于检查约 10 个 root 的独立优化谱系
|
+-- tree_statistics.py
|   +-- 跨树统计记忆
|   +-- 汇总变量效果、kill_rate、rescue_success_rate 和 root tree 排名
|   +-- 输出 tree_statistics.json/md 和 tree_memory_cards.jsonl
|   +-- 给 Audit Agent / generator 提供统计 RAG 上下文
|
+-- tree_reports.py
|   +-- 每棵 root 树输出 SIMPLE_TREE.md
|   +-- tree 文件夹外输出 GLOBAL_TREE_SUMMARY.md
|   +-- tree 文件夹外输出 EXPERIMENT_FORMULA_SUMMARY.md
|   +-- 面向实验者复盘，不替代统计 RAG
|
+-- formulation_checks.py          ← 从 workflow.py 抽出
|   +-- 材料完整性检查
|   +-- formulation/materials 双向同步
|   +-- 制备时间估算
|
+-- audit.py
|   +-- 化学可行性审计
|   +-- 规则审计
|   +-- audit_status / experimental_status 分离
|
+-- ratio_planner.py
|   +-- 20 g batch 比例换算
|   +-- 材料克数和水补足
|
+-- bruker_parser.py
|   +-- 摩擦 CSV 解析
|   +-- 摩擦模式判别
|   +-- 压缩模量解析
|
+-- io_artifacts.py
    +-- DOE CSV 导出
    +-- results_template 导出
    +-- results_filled 读取
    +-- KPI 计算
```

这部分是项目的"最小可行主线"。后续文档和教学应优先围绕这组文件讲。

---

## 2. 主线编排代码：保留，但需要继续变瘦

```text
仍然重要，但承担太多责任
|
+-- generator.py
|   +-- 当前约 1500 行
|   +-- 负责 prompt、后处理、ratio plan、约束、继承表
|   +-- 下一步应拆分为：
|       +-- prompt_builder.py
|       +-- candidate_normalizer.py
|       +-- candidate_assembly.py
|       +-- candidate_gate.py
|
+-- pipeline_agents.py
|   +-- 当前已从约 820 行降到约 723 行
|   +-- 已抽出 formula_materializer.py
|   +-- 下一步应拆分为：
|       +-- audit_agent_runner.py
|       +-- doe_plan_runner.py
|       +-- legacy_formula_agent.py
|
+-- workflow.py
    +-- 当前约 1100 行
    +-- 同时负责 wetlab、diagnosis、formulation sync、DOE factor
    +-- 下一步应拆分为：
        +-- wetlab_export.py
        +-- formulation_sync.py
        +-- diagnosis_runner.py
        +-- doe_factor_builder.py
```

已经完成的第一步精简：

```text
pipeline_agents.py
|
+-- 原来：内置受限配方物化逻辑
+-- 现在：调用 formula_materializer.py
```

这样 `formula_materializer.py` 成为一个更深的 Module：Interface 很小，但隐藏了"复制父配方并只改允许变量"的实现细节。

---

## 3. 辅助代码：有用，但不是第一阅读路径

```text
辅助能力
|
+-- experiment_state.py
|   +-- 结构化实验记录
|   +-- 后续做数据库/报告时有用
|
+-- experiment_rag.py
|   +-- 实验记忆 JSONL
|   +-- 历史检索
|   +-- 当前主线可用，但不是必须先读
|
+-- rule_checker.py
|   +-- DOE plan 规则检查
|   +-- 与 candidate_rules.py 有部分重叠
|   +-- 后续可考虑合并规则来源
|
+-- simulation.py
|   +-- mock 流程测试
|   +-- 开发时保留
|
+-- utils.py
    +-- JSON/CSV 工具
    +-- 材料名规范化
    +-- Bruker 函数转出
```

这些文件不建议删除，但可以在 README 里标为"辅助层"，避免新读者一开始被所有文件淹没。

---

## 4. 可归档或降级为遗留路径的代码

```text
可选/遗留
|
+-- candidate_critic.py
|   +-- 当前主线没有直接调用
|   +-- 更像旧版"多 DOE + Critic"路线
|   +-- 建议标记为 optional_experimental
|
+-- LLM free-form Formula Agent path in pipeline_agents.py
|   +-- 当前受限模式默认绕过
|   +-- 建议后续迁移到 legacy_formula_agent.py
|
+-- 大 DOE coverage 逻辑
    +-- 对 100 个实验内小步迭代不是默认主线
    +-- 应保留但只在 legacy/expanded DOE 模式启用
```

删除前建议满足两个条件：

1. 有 golden workflow 测试保护受限主线；
2. 至少一个真实 R1 -> R2 -> R3 流程跑通并确认不依赖这些旧路径。

---

## 5. 下一步推荐精简顺序

```text
Phase 1: ✅ 已完成
|
+-- ✅ 抽出 formula_materializer.py
+-- ✅ 新增 RunWorkspace / --status / --sync_results / --regenerate_round
+-- ✅ 新增 budget_manager.py + BUDGET config
+-- ✅ 新增 constrained_planning_policy.yaml 规则单一来源
+-- ✅ 新增 tree-mode CLI 参数和单父节点 DOE 入口
+
Phase 2: ✅ 已完成
|
+-- ✅ test_candidate_rules.py (35 tests)
+-- ✅ test_constrained_doe.py (11 tests)
+-- ✅ test_formula_materialization.py (10 tests)
+-- ✅ conftest.py 共享 fixtures
+-- ⬜ golden R1 -> expected R2 fixture

Phase 3: 部分完成
|
+-- ✅ 从 workflow.py 抽 formulation_checks.py
+-- ⬜ 从 generator.py 抽 candidate_normalizer.py
+-- ⬜ 从 workflow.py 抽 wetlab_export.py
+-- ⬜ 从 workflow.py 抽 diagnosis_runner.py

Phase 4: 归档旧路径
|
+-- candidate_critic.py 标记 optional_experimental
+-- legacy_formula_agent.py
+-- legacy_doe_coverage.py

Phase 5: 规则单一来源
|
+-- constrained_planning_policy.yaml
+-- candidate_rules.py / rule_checker.py 读取同一规则
+-- prompt 和文档引用同一规则摘要
```

## 最小阅读路径

如果只想理解项目如何跑，先读这 8 个文件：

```text
docs/overview/工作流概述.md
docs/design/project_feasibility_thinking_tree.md
artifact_store.py
constrained_doe.py
formula_materializer.py
candidate_rules.py
formula_tree.py
budget_manager.py
cli.py
```

如果只想改实验策略，优先改：

```text
constrained_doe.py
candidate_rules.py
ratio_planner.py
constrained_planning_policy.yaml
formula_tree.py
```

如果只想处理真实实验数据，优先看：

```text
bruker_parser.py
io_artifacts.py
artifact_store.py
```
