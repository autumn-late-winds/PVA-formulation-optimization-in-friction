# 项目术语表

这个文件只记录项目里的核心概念，用来统一后续代码评审、文档和架构讨论的语言。它不是实现说明，也不是需求文档。

## 术语

- **受限 DOE 骨架**：由代码生成的下一轮实验设计框架。它先确定父配方、设计类型和允许改变的变量，再交给 LLM 做解释和补充。

- **语义设计变量**：真正代表实验选择的 formulation 或 processing 变量，例如 PVA 浓度、冻融次数、浸泡时间、交联剂浓度。由比例规划器派生出的材料克数、单位和 basis 不属于语义设计变量。

- **继承关系表**：每轮输出的人类可读追踪表，记录 candidate_id、parent_candidate_id、design_type、changed_variables、if_better、if_worse 和 black_box_jump_score。

- **配方优化树**：以一个初始配方为 root，从某个活跃配方节点出发生成少量局部分支；分支根据实验结果被保留、抢救或剪枝。项目当前推荐用约 10 个初始配方启动约 10 棵独立优化树，而不是把所有候选混成一棵大树。

- **单父节点优化**：R2+ 可通过 `--target_parent_id` 指定本次只围绕一个父配方节点生成小步优化分支。父节点可以来自上一轮，也可以来自更早轮次，例如在 R3 继续展开 R1-07。默认不再消耗候选名额做 baseline repeat；复现应作为单独验证任务处理。

- **树节点字段**：候选记录中用于追踪树状谱系的字段，包括 tree_id、tree_label、node_id、parent_node_id、root_candidate_id、tree_depth、branch_status、branch_intent 和 parent_branch_status_at_generation。
- **树编号与配方编号分层**：`tree_id/tree_label` 使用 `root-*` 表示第几棵树；`candidate_id/parent_candidate_id/root_candidate_id` 保持 `R*-*` 表示真实配方节点。`root-*` 可以作为目录名和展示名，但不能替代 `parent_candidate_id`。

- **分支判定文件**：`formula_branch_decisions.json` 是机器可读的树分支状态表。`formula_tree.py` 根据父子 COF、实验备注和 rescue 记录推断 `continue`、`rescue_candidate`、`kill`、`hold`、`pending` 或 `root`。

- **kill 分支**：若某节点已经被推断或人工标记为 `kill`，后续 `--target_parent_id` 不应再指向该节点；代码会阻止继续展开已 kill 的分支。

- **跨树统计记忆**：树内仍然严格单父节点局部优化；树间共享统计知识。`tree_statistics.py` 会从多棵优化树中汇总变量效果、rescue 成功率、kill 频率和 root tree 排名，让第 5 棵树可以参考前 4 棵树的统计先验，但不会混合父节点继承关系。

- **统计 RAG 文件**：`tree_statistics.json` 是机器可读统计，`tree_statistics.md` 是人可读报告，`tree_memory_cards.jsonl` 是可检索记忆卡片。Audit Agent 和普通 generator prompt 都可以注入这段 cross-tree statistical memory。

- **树报告与实验汇总**：`tree_reports.py` 会在每个 `trees/root-*` 下生成 `SIMPLE_TREE.md`，用缩进树列出节点、状态、COF、改动变量和简要配方；同时在 tree 文件夹外生成 `GLOBAL_TREE_SUMMARY.md` 和 `EXPERIMENT_FORMULA_SUMMARY.md`，分别用于全局过程浏览和实验步骤/材料配方汇总。

- **运行工作区**：一次优化运行的目录，包含每轮候选配方、原始摩擦 CSV、压缩模量 CSV、结果文件、诊断文件和日志。

- **审计状态**：候选记录是否通过结构、规则、白名单和完整性检查。审计失败不等于实验失败。

- **实验状态**：湿实验是否成功产生可用材料和可测性能数据。

- **轮次清单**：建议新增或自动推断的轮次状态索引，用来说明某一轮有哪些文件、缺哪些文件、下一步应该运行什么命令。

- **受限轮次规划器**：项目当前推荐的主线流程。它用代码生成受限 DOE 骨架、复制父配方并只应用允许变量变化，再让 LLM 负责解释和报告。

- **主线代码**：运行 100 个以内 PVA 小步迭代时默认需要理解和维护的代码路径。

- **辅助代码**：对报告、记忆、测试或扩展有价值，但不是理解主线工作流的第一阅读路径。

- **遗留路径**：早期为了探索模型能力而保留的自由规划或大 DOE 路径。它可以保留用于对比，但不应作为默认实验规划方式。

- **深模块**：Interface 小、Implementation 多的模块。调用者只需要知道少量概念，就能获得较多行为和可靠性。
