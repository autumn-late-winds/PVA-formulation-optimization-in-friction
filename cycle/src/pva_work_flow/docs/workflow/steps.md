#R2+受限工作流步骤

当前R2+流程已经从LLM自由生成完整配方改为代码生成受限继承骨架、LLM负责解释和补充。最新主线进一步支持“配方优化树”：R1 可生成约 10 个初始 root 配方，R2+ 每次通过 `--target_parent_id` 只围绕一个父配方节点生成 baseline repeat 和局部分支。目标是降低14B模型难度、减少黑盒跳跃，并让文章中的 10 条左右配方谱系都可追踪。树状态遵循：变好 `continue`，变差 `rescue_candidate`，rescue 后仍变差或仍有实验错误则 `kill`。

##Step1:AuditAgent

输入父节点所在轮次的候选配方和实验结果，输出最佳候选、失败候选、有效变量、风险变量和待验证问题，并严格区分audit_status与experimental_status。AuditAgent不生成下一轮配方。若使用 `--target_parent_id R1-07`，系统会从 ID 推断父轮次为 R1，而不是强制读取上一轮。

##Step2:ConstrainedDOESkeleton

R2+默认由constrained_doe.py生成受限继承条目。普通模式下围绕代码选出的父配方生成 baseline_reproduction、single_factor_perturbation、local_optimization 等小步候选；树模式下 `--target_parent_id` 会把父配方锁定为一个具体节点，只为这一棵树生成局部分支。默认不生成limited_exploration；如需新材料或新处理方式，应由人工显式打开。

##Step3:CodeMaterializedFormulaBuilder

当R{N}_doe_plan.json包含skeleton_source=code_constrained_doe时，pipeline_agents.py直接复制父配方，只应用variables_changed指定的变量变化，并保留parent_candidate_id、design_type、if_better、if_worse。baseline_reproduction必须保持零变量变化。树模式下还会写入 tree_id、tree_label、node_id、parent_node_id、root_candidate_id、tree_depth、branch_status、branch_intent、parent_branch_status_at_generation 等字段。如果被选中的父节点已经是 `kill`，生成阶段会直接拒绝继续展开。

编号规则必须分层：`tree_id/tree_label` 使用 `root-*` 表示第几棵树；`candidate_id/parent_candidate_id/root_candidate_id` 保持 `R*-*` 表示真实配方节点。树与树之间的统计可以共享，但不能把 `root-*` 当作配方父节点。

##Step4:RatioPlannerAndAudit

ratio_planner.py负责把formulation转换为20gbatch材料用量。candidate_rules.py和audit.py负责自动检测语义changed_variables、检查PVA主体系、父配方有效性、baseline完全复现、变量变化数量和black_box_jump_score。材料清单中的amount/unit/basis属于派生输出，不再作为额外设计变量重复计入changed_variables。

##Step5:LineageOutput

每轮必须输出R{N}_candidates.json、R{N}_audits.json、R{N}_doe.csv、R{N}_results_template.csv和R{N}_inheritance_table.md。其中R{N}_inheritance_table.md是检查本轮是否可追溯、可解释、可执行的首要文件。跨轮树状谱系由formula_tree.md汇总，优先用它查看每棵 root tree 的分叉、continue/rescue_candidate/kill 状态和 dCOF。机器可读状态写入 `formula_branch_decisions.json`，后续 CLI 会用它判断某个 `--target_parent_id` 是否还能继续展开。

##Step5.5:CrossTreeStatisticalRAG

树状优化不等于每棵树彼此失忆。当前原则是：

```text
树内：严格单父节点局部优化
树间：共享统计知识和失败经验
```

`tree_statistics.py` 会跨所有已完成树读取 `R*_candidates.json`、`R*_results_filled.csv` 和 `formula_branch_decisions.json`，输出 `tree_statistics.json`、`tree_statistics.md` 和 `tree_memory_cards.jsonl`。统计范围包括当前 run 根目录，以及 `trees/root-*` 这种按树拆分的子目录。这样即使每棵树都有局部编号如 `R2-01`，全局统计也会按 artifact_source 分开读取，再合并为跨树先验。这些文件记录变量改动的 improvement_rate、mean_delta_cof、kill_rate、rescue_success_rate，以及每棵 root tree 的 best_cof 和分支状态分布。后续建第 5 棵树时，系统仍只从一个 root/parent 节点出发，但可以把前 4 棵树学到的统计先验注入 Audit Agent 和 generator prompt。

Diagnosis 阶段的父子比较只用于判定当前分支是否 improved / worsened / flat。它会读取 `R1` 到 `R(N-1)` 的全部结果，按当前候选的真实 `parent_candidate_id` 找 parent COF，而不是默认只看 `R(N-1)`。因此，第 N 轮既能参考全部历史统计，又不会破坏单父节点树结构。

## 使用手册

### 目录结构

```
out_dir/
├── R1_candidates.json          # R1 所有 root 配方（备份）
├── R1_results_filled.csv       # R1 实验结果
├── R1_diagnosis.json           # R1 LLM 诊断
├── kpi_log.json                # 跨轮 KPI 趋势
├── experiment_records.jsonl    # RAG 记忆库
├── formula_tree.md             # 跨轮配方树
├── tree_statistics.json/md     # 跨树统计
├── GLOBAL_TREE_SUMMARY.md      # tree 文件夹外的全局简树
├── EXPERIMENT_FORMULA_SUMMARY.md # tree 文件夹外的材料配方/步骤汇总
└── trees/                      # 每棵树的独立工作区
    ├── INDEX.md                # 8 棵树索引
    ├── root-01/                # tree_id=root-01, root_candidate_id=R1-01
    │   ├── root_candidate.json # 该 root 的完整配方
    │   ├── results.json        # 实验结果
    │   ├── audit.json          # 审计结果
    │   ├── R1_R1-01_friction.png
    │   ├── compression.csv
    │   └── bruker_csv/         # 原始摩擦 CSV
    ├── root-02/                # + DMSO
    ├── ...
    └── root-08/                # 光固化 IPN
```

### Shell 脚本 (推荐)

```bash
# ---- 树展开（核心操作） ----
bash scripts/pva_vllm.sh --expand root-04        # 自动: max轮次+1 (如R1→R2, R2→R3)
bash scripts/pva_vllm.sh --expand root-04 --round 3  # 强制展开到指定轮次
bash scripts/pva_vllm.sh --expand_all          # 所有树各自 max+1

# ---- 重新生成 ----
bash scripts/pva_vllm.sh --regenerate 2        # 归档并重新生成 R2（root out_dir，旧版兼容）
bash scripts/pva_vllm.sh --regenerate_all_trees 2    # 归档并重新生成所有 root 树的 R2
bash scripts/pva_vllm.sh --regenerate_tree root-04 --round 2  # 只重生成 root-04 的 R2
bash scripts/pva_vllm.sh --regenerate_tree root-04 2          # 同上，简写

# ---- 诊断 ----
bash scripts/pva_vllm.sh --diagnose_tree root-04             # 诊断树的 R2
bash scripts/pva_vllm.sh --diagnose_tree root-04 --round 3   # 诊断树的 R3
bash scripts/pva_vllm.sh --diagnose_only                   # 诊断所有轮次

# ---- Bruker 数据处理 ----
bash scripts/pva_vllm.sh --bruker_dir <实验数据目录>

# ---- 自定义收敛标准 ----
CONV_COF_MAX=0.03 bash scripts/pva_vllm.sh --expand root-04

# ---- 环境变量 ----
ENGINE=vllm                    # vllm | mock
VLLM_MODEL_NAME=qwen3-14b-sft # 模型名
OUT_DIR=./src/sft_qwen3_14b_out
SEED=7
ROUNDS=3                       # 最大轮次数（--diagnose_only 用）
```

### Python CLI（高级用法）

```bash
cd cycle && export PYTHONPATH=src

# 查看运行状态
python -m pva_work_flow.cli --status --out_dir <dir>

# 展开一棵树（自动路由到 trees/root-* 目录）
python -m pva_work_flow.cli --engine vllm --mode generate \
    --round 2 --target_parent_id R1-04 --out_dir <dir>

# 完整流程（generate + audit + prepare）
python -m pva_work_flow.cli --engine vllm --mode prepare \
    --round 2 --target_parent_id R1-04 --out_dir <dir>

# 诊断树（需要 results_filled.csv）
python -m pva_work_flow.cli --engine vllm --mode diagnose \
    --round 2 --target_parent_id R1-04 --out_dir <dir>

# 自定义收敛阈值
python -m pva_work_flow.cli --engine vllm --mode full --rounds 3 \
    --conv_cof_max 0.03 --conv_modulus_min 1.0 --out_dir <dir>

# Mock 开发测试
python -m pva_work_flow.cli --engine mock --mode generate \
    --round 2 --target_parent_id R1-04 --out_dir <dir>
```

##Step6:RunWorkspaceStatus

当不确定下一步该做什么时，先运行：

python -m pva_work_flow.cli --status --out_dir <run_dir>

如果已经放入新的摩擦CSV和压缩CSV，优先运行：

python -m pva_work_flow.cli --sync_results <run_dir>

如果某一轮是旧策略生成的结果，先归档再重生成：

python -m pva_work_flow.cli --engine vllm --regenerate_round 2 --archive_old --out_dir <run_dir> --n_candidates 4 --n_select 4

## 黑盒跳跃防护规则

> 来源：`black.md`（已合并）。当前项目使用代码约束而非 LLM 自由生成来控制 R2+ 配方。

### 已实施的防护

- `constrained_doe.py` 构建 R2+ DOE 骨架
- tree mode 使用 `--target_parent_id` 只展开一个配方节点
- `pipeline_agents.py` 在受限模式下复制父配方，只应用 `variables_changed`
- `candidate_rules.py` 检查 parent/baseline/semantic_changed_variables/limited_exploration/PVA/black_box_jump_score
- 候选记录 tree_id, tree_label, node_id, parent_node_id, root_candidate_id, tree_depth, branch_status
- audit_status 与 experimental_status 分离
- `R{N}_inheritance_table.md` 是首要追踪产物
- `formula_tree.md` 是跨轮树审查产物
- `tree_statistics.json/md` 和 `tree_memory_cards.jsonl` 是跨树统计 RAG 产物
- `trees/root-*/SIMPLE_TREE.md`、`GLOBAL_TREE_SUMMARY.md`、`EXPERIMENT_FORMULA_SUMMARY.md` 是实验者审查产物；它们只汇总过程、节点配方、材料和步骤，不改变统计 RAG 或 parent_candidate_id
- 跨树统计可指导后续树，但不得改变 parent_candidate_id 继承关系

### 拒绝规则

1. R2+ 候选缺少 parent_candidate_id
2. baseline_reproduction 改变了父配方的材料/浓度/工艺
3. single_factor_perturbation 改变超过 1 个语义设计变量
4. local_optimization 改变超过 2 个语义设计变量
5. 未经人工批准默认开启 limited_exploration 或引入新材料
6. 将 audit failure 当作凝胶失败或实验失败
7. 为追求新颖性跳出 PVA 主体系
8. 设置了 target_parent_id 时混合多个父节点
9. 对性能下降的分支不做 rescue_candidate 标记也不解释 rescue 逻辑
10. 使用跨树统计来混合父节点或跳过 target_parent_id 继承关系

审查顺序：先看 `formula_tree.md` 和 `R{N}_inheritance_table.md`，再看 `R{N}_candidates.json`。建后续 root tree 前先看 `tree_statistics.md`。
