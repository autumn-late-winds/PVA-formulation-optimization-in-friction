# PVA 水凝胶受约束闭环实验规划系统

---

## 1. 项目定位

面向 PVA（聚乙烯醇）水凝胶摩擦学优化的**受约束闭环实验规划系统**。

> 用 14B 大模型在代码规则、材料白名单、父配方继承和 DOE 模板约束下，辅助分析实验结果，并推荐下一轮小步迭代实验。

系统以 Qwen 系列（8B/14B/32B）作为解释、总结和报告生成引擎；以代码层规则作为硬约束引擎。核心关注点是**多轮迭代优化中的逻辑连续性和可解释性**——每轮设计决策必须能从上一轮实验结果推导出来，且能被代码层检查。

相关设计文档：
- 思路演变：[project_feasibility_thinking_tree.md](../design/project_feasibility_thinking_tree.md)
- 代码结构：[code_simplification_tree.md](../architecture/code_simplification_tree.md)
- 工程改动：[project_change_tree.md](../architecture/project_change_tree.md)
- 术语表：[CONTEXT.md](../CONTEXT.md)

---

## 2. 当前实现状态

已完成最小可行闭环，核心功能：

### 工作流
- 用 `MockLLM` 或 `VllmOpenAIChatLLM` 跑通完整闭环（generate → audit → prepare → diagnose）
- R1 生成约 10 个初始 root 配方；R2+ 通过 `--target_parent_id` 每次只展开一个父配方节点
- R2+ 默认由 `constrained_doe.py` 生成受限 DOE 骨架（小步 single_factor/local_opt 分支；默认不再生成 baseline repeat）
- `n_candidates` / `n_select` 根据轮次自动推算，无需手动设置

### 3-Agent 流水线（R2+）
- **Audit Agent** — 分析上一轮数据，输出最佳/失败候选、有效变量、风险变量
- **DOE Planning Agent** — 解释代码生成的受限继承表，不可修改骨架
- **Formula Generation Agent** — 将继承表转换为完整配方，受限模式下代码直接复制父配方

### 约束与审计
- `candidate_rules.py`：父配方检查、baseline 完全复现、语义 changed_variables、变量数量限制、黑盒跳跃评分
- `rule_checker.py`：10 条硬规则独立检查器
- `audit.py`：化学可行性硬审计 + 占位符材料名拦截
- 审计失败（audit_status）与实验失败（experimental_status）严格分离

### 配方优化树
- 约 10 个独立 root 配方各自展开局部优化树
- `formula_tree.md` 显示 tree_id、branch_status、父子 COF 差值
- `formula_branch_decisions.json` 记录机器可读分支判定（continue / rescue_candidate / kill）
- 树内严格单父节点继承；树间通过 `tree_statistics.py` 共享统计 RAG

### 收敛判断（新增）
- 5 项收敛指标：COF ≤ 阈值、模量在目标区间、稳定平台占比达标、粘滑评分达标、COF 趋势连续 N 轮平坦
- 每轮诊断自动输出收敛结论和下一轮建议，写入 `R{N}_diagnosis.json` 的 `convergence` 字段
- 收敛标准可通过 CLI 参数或 shell 环境变量自定义

### 代码层自动修复（Plan A）
- 自动补齐 `expected_mechanism`、`risks_and_mitigations`、`doe_factor_levels`、`material.basis`
- 自动修正不合理数值（soak ≤ 4h、FT ≤ 3、cycle_h ≤ 2）
- 70+ 条材料名规范化映射（中文→英文、简写→全称）

### 运行辅助
- `--status` 查看运行工作区状态和下一步建议
- `--sync_results` 从 `R{N}/` 和 `R{N}_compression/` 自动重建实验结果
- `--regenerate_round N --archive_old` 安全替换旧轮次
- `--list_error_codes` 查看 10 种标准实验错误码

### 测试
- 80 个单元测试覆盖 candidate_rules / constrained_doe / formula_materialization / formula_tree / tree_statistics / tree_reports / workflow parent COF lookup / tree_naming / ratio_planner baseline preservation
- `python -m compileall` 全部通过

---

## 3. 工作流与数据流

```

---

```

### 3.1 成功与失败样品同等重要

本项目的核心目标不是让每一轮湿实验都成功，而是让每一次湿实验都转化为模型可学习的样本。成功样品是正样本，用来定义可行配方区域、有效变量方向和低摩擦/高稳定性的候选机制；失败样品是负样本，用来定义不可行边界、危险变量、材料不兼容关系和后续必须规避或单独验证的条件。

因此，湿实验失败不应被视为无效数据。失败样品必须被结构化记录为负样本，进入 RAG 记忆，并参与后续 DOE 设计。特别是当样品破碎、未成胶、过软、过度溶胀或相分离时，系统需要做三件事：

1. 记录失败模式：通过 `R{N}_experiment_notes.json`、`failure_type` 和 ERROR1-10 错误码记录真实湿实验现象。
2. 拆分失败因素：由 `failure_factor_memory.py` 将失败样品中的变量变化拆成 suspected failure factors。
3. 设计单因素验证：下一轮 `constrained_doe.py` 优先生成 `failure_factor_verification`，只改变一个 suspected factor。如果仍失败，则将该因素升级为 confirmed failure factor；如果不失败，则将该因素降级为 disproved 或 mixed，转而考虑组合效应。

新增记忆文件：

```text
failure_factor_memory.jsonl      # suspected / confirmed / disproved / mixed 失败因子
experiment_contrast_memory.jsonl # parent-child 正负样本对比
rag_vector_index.json            # 本地向量化 RAG 索引
FAILURE_FACTOR_SUMMARY.md        # 当前失败因子总结
NEXT_VERIFICATION_PLAN.md        # 下一轮单因素验证计划
```

RAG 向量化：

```text
项目现在使用 hybrid RAG：结构化过滤和规则打分仍然作为主要安全层，
rag_vector_index.json 提供额外的语义相似检索层。
当前后端是本地 TF-IDF sparse vector + cosine similarity，
不依赖联网、外部 embedding API 或模型下载。
索引内容包括 failure_factor_memory.jsonl、experiment_contrast_memory.jsonl、
tree_memory_cards.jsonl，以及 SQLite 文献 RAG 数据库中的 formulation_cases。
这样成功样品和失败样品都可以作为正/负样本，被下一轮设计按语义相似度召回。
```

新增统计含义：

```text
positive_sample_count      # 有效、可测、可用于学习成功区域的样品数
negative_sample_count      # 失败、破碎、未成胶等可用于学习不可行边界的样品数
unsafe_factor_count        # suspected + confirmed failure factors 数量
failure_mode_distribution  # 失败模式分布
```

设计原则：

```text
成功样品告诉模型“可以做什么”。
失败样品告诉模型“不要做什么，以及下一轮应该验证什么”。
两者共同定义真实可落地的实验可行域。
```
```text
R1: 生成约10个root → 湿实验 → Bruker分析 → 诊断 → R1_diagnosis.json
                                                         ↓
R2: 选择父节点 → constrained DOE骨架 → 3-Agent → 候选配方 → 审计 → 湿实验
                                                         ↓
R3: ... → 收敛判断 → converged? → 锁定配方 / 继续迭代 / 切换root tree
```

每轮输出文件：
```
R{N}_candidates.json       # 候选配方（含 tree_id、ratio_planner 信息）
R{N}_audits.json            # 审计结果
R{N}_inheritance_table.md   # 配方继承关系表
R{N}_doe.csv                # DOE 导出（含 design_role、recommended_repeats）
R{N}_results_template.csv   # 实验结果模板
R{N}_results_filled.csv     # 实验数据（可从 Bruker CSV 自动构建）
R{N}_diagnosis.json         # LLM 诊断 + 收敛判断
formula_tree.md             # 跨轮配方树
formula_branch_decisions.json  # 机器可读分支状态
tree_statistics.json/md     # 跨树统计
GLOBAL_TREE_SUMMARY.md      # tree 文件夹外的全局简树
EXPERIMENT_FORMULA_SUMMARY.md # tree 文件夹外的材料配方/步骤汇总
tree_memory_cards.jsonl     # 统计 RAG 记忆卡片
kpi_log.json                # 跨轮 KPI 趋势
```

---

## 4. 核心设计原则

### 4.1 配方树优化模型

```text
R1: 10 个独立 root 配方（不同机制假设/配方区域）
R2+: 每次只选一个父节点，生成 2-4 个小步分支；复现/重复性验证单独安排

性能提升 → continue（该分支继续扩展）
性能下降 → rescue_candidate（给一次定向修复机会）
rescue后仍下降 → kill（该分支停止扩展）

多棵树之间：共享统计先验，不共享父子继承关系
```

### 4.2 五个设计类型

| 类型 | 变量变化 | 每轮数量 |
|------|:--:|:--:|
| `baseline_reproduction` | 0（代码完全复制父配方） | 1 |
| `single_factor_perturbation` | 恰好 1 | 2 |
| `local_optimization` | 最多 2 | 1 |
| `failure_factor_verification` | 最多 1 | 0-1 |
| `limited_exploration` | ≤1 新材料 | 最多 1（默认关闭） |

### 4.3 硬性规则（代码层强制执行）

- PVA 必须是主聚合物
- R2+ 每个候选必须有有效 `parent_candidate_id`
- `baseline_reproduction` 零变量变化
- 变量变化使用语义设计变量口径（忽略 ratio_planner 派生的 amount/unit/basis）
- `audit_status` 和 `experimental_status` 分开记录
- 每个候选必须有 `if_better` 和 `if_worse`

### 4.4 收敛标准（可配置）

| 指标 | 默认阈值 | CLI 参数 |
|------|----------|----------|
| COF | ≤ 0.02 | `--conv_cof_max` |
| 压缩模量 | 1.5–2.5 MPa | `--conv_modulus_min/max` |
| 稳定平台占比 | > 0.6 | `--conv_stable_proportion` |
| 粘滑评分 | < 0.2 | `--conv_stick_slip_max` |
| COF 趋势 | 连续 2 轮 ΔCOF < 0.005 | `--conv_cof_trend_delta/rounds` |

### 4.5 湿实验评估指标（模型对比用）

湿实验验证阶段使用 `wetlab_metrics.py` 对每个 cycle run 目录做只读评估。该指标组用于比较 bare base 与 SFT+RAG 两组模型在真实实验中的表现，重点不是单次配方是否漂亮，而是连续迭代是否真的推动 COF、稳定性和可制备性改善。

| 指标 | 计算口径 | 默认阈值/说明 |
|------|----------|---------------|
| `endpoint_improvement` | `(root_cof - final_chain_parent_cof) / root_cof` | 评价链式搜索终点相对 root 的 COF 改善 |
| `best_improvement` | `(root_cof - best_cof_under_root) / root_cof` | 评价该 root 下已测样品的最好 COF 改善 |
| `single_step_hit_rate` | 单步候选中满足 `child_cof - parent_cof <= -0.005` 的比例 | 默认有效下降阈值 0.005 |
| `steps_to_best_on_chain` | 接受链路上首次到达最好样品所需步数 | root 为 0，第一轮 accepted child 为 1 |
| `steps_to_10pct_improvement` | 接受链路上首次达到 ≥10% COF 改善的步数 | 未达到则为 `not_available` |
| `steps_to_cof_target` | 接受链路上首次达到目标 COF 的步数 | 默认 `cof_target_max=0.03` |
| `cof_target_rate` | 已测 descendant 中 COF ≤ 目标值的比例 | 默认目标 COF 0.03 |
| `fabrication_success_rate` | 有有效 COF 且未出现关键制备失败的样品比例 | 关键失败包括 no_gel/fracture/rupture/delamination 等 |
| `failure_rate` | `failure_type` 非 none/空/na 的样品比例 | 反映湿实验失败风险 |
| `modulus_in_target_rate` | 压缩模量在目标区间内的样品比例 | 默认 1.5–2.5 MPa |
| `mean_mechanical_retention_vs_parent` | `child_modulus / parent_modulus` 的平均值 | 评价降低摩擦时是否保留力学性能 |
| `strict_success_rate` | 同时满足 COF、模量、稳定平台、粘滑、无失败的样品比例 | 需要 `stable_proportion` 和 `stick_slip_score` 列 |
| `stable_friction_rate` | `stable_proportion >= 0.6` 的样品比例 | 缺列时标记 `not_available` |
| `stick_slip_pass_rate` | `stick_slip_score <= 0.2` 的样品比例 | 缺列时标记 `not_available` |
| `rag_supported_rate` | 候选中存在 per-candidate RAG 证据字段的比例 | 缺少证据字段时标记 `not_available`，不硬算为 0 |
| `inventory_hit_rate` | 候选材料全部命中库存/允许材料 CSV 的比例 | 默认读取 `cycle/materials/materials_en.csv`，也可用 `--inventory-csv` 指定 |
| `new_material_rate` | 相对 parent 引入新非基础材料的候选比例 | 用于监控模型是否频繁跳出库存内小步优化 |
| `purchase_blocked_rate` | 因材料不在库存 CSV 中而可能需要采购的候选比例 | 反映实验排期被采购打断的风险 |
| `inventory_constrained_success_rate` | 库存内候选中制备成功且有有效 COF 的比例 | 评价“只用现有库存”时的真实执行成功率 |

运行示例：

```bash
python -m pva_work_flow.wetlab_metrics \
  --run-dir <cycle_run_dir> \
  --root-id R1-01 \
  --inventory-csv cycle/materials/materials_en.csv
```

默认输出：

```text
<cycle_run_dir>/wetlab_metrics.json
<cycle_run_dir>/wetlab_metrics.md
```

### 4.6 RAG 检索评估指标

RAG 层先用 `evaluate_rag_retrieval.py` 做离线检索评估，再进入模型生成与湿实验验证。该评估回答的问题是：给定真实配方优化问题，RAG 是否能把相关案例排在前面，并提供足够完整、可追溯的证据。

| 指标 | 计算方法 | 含义 |
|------|----------|------|
| `MRR` | 对每个 query 找到第一个 relevant result 的排名 `rank`，计 `1/rank`；所有 query 取平均 | 相关证据是否排得足够靠前 |
| `nDCG` | 对 top-k 结果按相关性折扣累计：`DCG = Σ rel_i / log2(i+1)`；再除以理想排序 `IDCG` | 整体排序质量，越接近 1 越好 |
| `hit@1` | top-1 中是否至少有一个 relevant result；对所有 query 取平均 | 第一条结果是否可用 |
| `hit@5` / `hit@10` | top-5/top-10 中是否至少有一个 relevant result；对所有 query 取平均 | 检索候选池是否覆盖相关证据 |
| `evidence_coverage` | 检查命中结果是否包含来源、baseline、optimized formulation、changed factor、mechanism、locator 等字段；按字段覆盖率平均 | 证据是否足够支撑生成，而不只是“搜到了标题” |
| `zero_results` | 返回空结果的 query 数量 | 检索失败或索引覆盖漏洞 |
| `by_type` | 按 material/problem/property 等 query 类型分别统计 MRR、hit、coverage | 识别 RAG 在哪类问题上薄弱 |

内置 query 可以用相关关键词作为弱监督 gold；正式评估时可提供人工或 agent 审核过的 `relevant_case_ids` / `relevant_terms` gold JSON。默认输出：

```text
数据库/rag_evaluation_runs/rag_retrieval_eval_<timestamp>.json
数据库/rag_evaluation_runs/rag_retrieval_eval_<timestamp>.md
```

---

## 5. 四道防 LLM 幻觉的防线

| 防线 | 阶段 | 机制 |
|------|------|------|
| 结构化数据注入 | Generate | prompt 前端注入实验结果表 + 父配方 + DOE 边界 + 材料白名单 |
| 化学体系连续性校验 | Audit | 子配方与父配方比较网络类型+交联剂体系，切换需 `is_extension=true` |
| 代码层 DOE 因子构建 | Diagnose | LLM 只提优化方向，因子名和范围由代码 `ROLE_RANGES` 决定 |
| 比例规划器 DOE 锚定 | Ratio Plan | `doe_factor_levels` 声明驱动实际克数计算，不是装饰性文本 |

---

## 6. 命令行使用

```bash
# 完整流程（vLLM）
bash scripts/pva_vllm.sh --use_external_r1

# 实验完成后：自动构建结果 + 诊断
bash scripts/pva_vllm.sh --bruker_dir <dir>

# 自定义收敛标准
CONV_COF_MAX=0.03 CONV_MODULUS_MIN=1.0 bash scripts/pva_vllm.sh --bruker_dir <dir>

# 重生成某一轮
bash scripts/pva_vllm.sh --regenerate 2

# 查看运行状态
python -m pva_work_flow.cli --status --out_dir <dir>

# 树状优化
python -m pva_work_flow.cli --mode generate --round 1 --tree_initial_roots 10
python -m pva_work_flow.cli --mode generate --round 2 --target_parent_id R1-04

# 开发测试（mock 引擎）
python -m pva_work_flow.cli --engine mock --mode full --rounds 2 --simulate_results
```

Shell 脚本可覆盖的变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ROUNDS` | 3 | 总轮次数 |
| `OUT_DIR` | `sft_qwen3_14b_out` | 输出目录 |
| `SEED` | 7 | 随机种子 |
| `VLLM_MODEL_NAME` | `qwen3-14b-sft` | 模型名 |
| `CONV_COF_MAX` | 0.02 | 收敛 COF 阈值 |
| `CONV_MODULUS_MIN` | 1.5 | 收敛模量下限 |
| … | | 其余 5 个 `CONV_*` 变量 |

---

## 7. 文件结构索引

```
cycle/
├── src/pva_work_flow/
│   ├── cli.py                    # 命令入口
│   ├── generator.py              # 候选生成 + Plan A 自动修复
│   ├── pipeline_agents.py        # 3-Agent 流水线编排
│   ├── constrained_doe.py        # 代码层 DOE 骨架生成
│   ├── formula_materializer.py   # 复制父配方 + 只改允许变量
│   ├── candidate_rules.py        # 继承规则 + 黑盒评分 + 继承表
│   ├── formula_tree.py           # 跨轮配方树 + 分支判定
│   ├── tree_statistics.py        # 跨树统计 RAG
│   ├── tree_reports.py           # SIMPLE_TREE / 全局树总览 / 实验配方步骤汇总
│   ├── audit.py                  # 化学可行性审计
│   ├── rule_checker.py           # 10 条硬规则检查
│   ├── workflow.py               # 诊断 + 收敛判断
│   ├── ratio_planner.py          # 20g batch 比例换算
│   ├── bruker_parser.py          # Bruker CSV 解析 + 摩擦模式判别 + 压缩模量
│   ├── formulation_checks.py     # 配方完整性 + 制备时间
│   ├── budget_manager.py         # 实验预算 + 阶段推断
│   ├── wetlab_metrics.py         # 湿实验指标评估 + bare base / SFT+RAG 对比口径
│   ├── artifact_store.py         # 运行工作区状态
│   ├── experiment_notes.py       # 实验备注 + ERROR1-10 错误码
│   ├── experiment_state.py       # 结构化实验记录
│   ├── experiment_rag.py         # JSONL 实验记忆 + 检索
│   ├── io_artifacts.py           # CSV 导入导出 + KPI 计算
│   ├── utils.py                  # JSON/材料名规范化/相似材料匹配
│   ├── llm_engines.py            # Mock / vLLM / Transformers 后端
│   ├── config.py                 # 约束常量 + 验收标准 + 收敛标准
│   ├── simulation.py             # 湿实验模拟器（测试用）
│   ├── candidate_critic.py       # 多 DOE 评分 + 优选（可选/实验性）
│   ├── prompts/                  # 所有提示词和规则 YAML
│   │   ├── prompts_en.yaml       # R1/Rn/Audit/Diagnosis 模板
│   │   ├── prompts_agents.yaml   # 3-Agent system prompt
│   │   ├── experiment_errors.yaml # ERROR1-10 错误码定义
│   │   └── constrained_planning_policy.yaml # 规则单一来源
│   └── scripts/
│       ├── pva_vllm.sh           # 一键运行（127 行）
│       └── vllm_start.sh         # 启动 vLLM 服务器
├── tests/
│   ├── test_candidate_rules.py   # 35 tests
│   ├── test_constrained_doe.py   # 11 tests
│   └── test_formula_materialization.py # 10 tests
├── materials/
│   └── materials_en.csv          # 336 种化学品清单
└── docs/                         # 项目文档（13 个 md）
    ├── CONTEXT.md                 # 术语表
    ├── overview/
    │   ├── project_description.md # 本文件
    │   └── 工作流概述.md
    ├── design/
    │   ├── hydrogel_agent_optimization_plan.md
    │   ├── pva_project_difficulty_reduction_codex_plan.md
    │   └── project_feasibility_thinking_tree.md
    ├── architecture/
    │   ├── project_change_tree.md
    │   ├── code_simplification_tree.md
    │   └── architecture_feasibility_review.md
    ├── workflow/
    │   └── steps.md
    └── rules/
        ├── inner_rules.md
        ├── friction_patterns.md
        └── model_iteration_evaluation.md
```

---

## 8. 验证命令

```bash
cd cycle
export PYTHONPATH=src

# 编译检查
python -m compileall src/pva_work_flow

# 运行测试
python -m pytest tests/

# 帮助信息
python -m pva_work_flow.cli --help

# Mock 最小闭环
python -m pva_work_flow.cli --engine mock --mode full --rounds 2 \
    --simulate_results --seed 42 --out_dir /tmp/test_run

# 查看工作区状态
python -m pva_work_flow.cli --status --out_dir /tmp/test_run
```
