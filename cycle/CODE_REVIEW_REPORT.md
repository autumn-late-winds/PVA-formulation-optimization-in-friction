# PVA Work Flow 代码审核报告

**审核日期**: 2026-06-06
**审核范围**: `cycle/src/pva_work_flow/` 全部 33 个 Python 文件 + 4 个 YAML 配置 + 3 个 Shell 脚本
**审核方法**: 5 个并行 agent 按子系统分工，逐文件逐行审查

---

## 目录

1. [总体评价](#总体评价)
2. [Critical 严重问题（9 个）](#critical-严重问题)
3. [核心管线问题（cli / generator / workflow）](#核心管线问题)
4. [约束 DOE 系统问题（constrained_doe / formula_materializer / pipeline_agents / candidate_rules / candidate_critic）](#约束-doe-系统问题)
5. [审计与规则问题（audit / rule_checker / experiment_notes / experiment_state / experiment_rag / formulation_rag）](#审计与规则问题)
6. [数据处理问题（bruker_parser / io_artifacts / ratio_planner / utils / formulation_checks）](#数据处理问题)
7. [树与记忆系统问题（formula_tree / tree_naming / tree_reports / chain_search / chain_memory / budget_manager）](#树与记忆系统问题)
8. [架构层面问题](#架构层面问题)
9. [共性问题模式](#共性问题模式)
10. [完整问题索引](#完整问题索引)

---

## 总体评价

**项目的核心思路清晰且正确**：R1 由 LLM 自由生成 → 人工实验 → diagnosis → R2+ 由代码约束 DOE 骨架控制生成（`constrained_doe.py` + `formula_materializer.py`），LLM 只做解释和补充。这条架构路线有效解决了 LLM 自由生成配方时的材料缺失、变量失控、化学错误等问题。

**逻辑完整**：generate → audit → prepare → 人工实验 → diagnose → 下一轮，闭环存在。

**但实现上有两类系统性问题**：

1. **错误静默传播**——数据损坏、文件缺失、候选丢弃时不报警，下游在错误数据上继续跑
2. **跨文件键名/路径不一致**——`parent_round_idx` vs `round_idx-1`、`variables_changed` 的 4 种写法，让多个模块在同一个概念上不同步

---

## Critical 严重问题

> **共 9 个，直接影响正确性和安全性，需优先修复。**

### C1 — tree_visualizer.py — 无限递归（无环检测）

**文件**: [tree_visualizer.py](cycle/src/pva_work_flow/tree_visualizer.py) `_render_node` 函数
**严重度**: Critical

`_render_node` 递归遍历子节点，没有任何 visited-set 或深度限制。如果任何候选的 `parent_candidate_id` 指向自己或后代（形成环），直接导致 `RecursionError`，整棵树构建崩溃。

**修复方向**: 在递归链中传入 `set` 记录已访问的 candidate_id，或加 `max_depth` 参数（默认 20）。

---

### C2 — budget_manager.py — 预算耗尽后仍推荐实验

**文件**: [budget_manager.py:147-195](cycle/src/pva_work_flow/budget_manager.py#L147-L195) `recommend_round_shape` 函数
**严重度**: Critical

当 `remaining_budget == 0` 时，函数仍返回 `round_size=2` 并填充 `required_design_types`。调用方会在预算已耗尽后继续生成实验。100 配方预算形同虚设。

**修复方向**: 函数顶部加早返回：
```python
if remaining_budget <= 0:
    return {"stage": stage, "remaining_budget": 0, "round_size": 0,
            "warnings": ["Budget exhausted. No more experiments."]}
```

---

### C3 — cli.py — 诊断重复执行（双倍 LLM 成本）

**文件**: [cli.py:359-367](cycle/src/pva_work_flow/cli.py#L359-L367) `one_round` 函数
**严重度**: Critical

在 `--mode full` 中：
- `one_round(r-1)` 在第 529 行已执行 `run_diagnose`
- `one_round(r)` 开头第 364 行又执行一次 `run_diagnose`（对 r-1 轮）

同一个 round 的诊断跑了两次，LLM API 成本翻倍，且第二次可能覆盖第一次的诊断文件。

**修复方向**: 删除第 359-367 行的冗余诊断调用，或改为只读检查（确认文件已存在）。

---

### C4 — cli.py — 零候选轮次无断路器

**文件**: [cli.py:509-519](cycle/src/pva_work_flow/cli.py#L509-L519) `one_round` 函数
**严重度**: Critical

当一整轮没有任何候选通过审计时，`one_round()` 调用 `run_text_only_diagnose` 后 `return`。但 `--mode full` 的外层循环继续执行 `one_round(r+1)`，而 `R{r}_results_filled.csv` 不存在、`R{r+1}_candidates.json` 不存在——下一轮必然崩溃。

**修复方向**: 设置一个标志位或抛受控异常，让外层循环检测到并停止整个 pipeline。

---

### C5 — workflow.py — 候选缺键导致 KeyError

**文件**: [workflow.py:710-713](cycle/src/pva_work_flow/workflow.py#L710-L713) `run_diagnose` 函数
**严重度**: Critical

```python
"pva_wt_percent": c["formulation"].get("pva_wt_percent"),
"freeze_thaw_cycles": c["processing"].get("freeze_thaw_cycles"),
```

`c["formulation"]` 和 `c["processing"]` 使用字典索引而非 `.get()`。如果 LLM 生成的 JSON 解析后缺少这两个键，诊断直接抛 `KeyError` 崩溃。

**修复方向**: 使用 `c.get("formulation", {})` 和 `c.get("processing", {})`。

---

### C6 — constrained_doe.py — 死代码导致 Entry 4 冻融回退不可达

**文件**: [constrained_doe.py:346-351](cycle/src/pva_work_flow/constrained_doe.py#L346-L351)
**严重度**: Critical

```python
if not _numeric_equivalent(old, new):     # 346: old ≠ new，确认通过
    # ...
    if _numeric_equivalent(old, new):     # 348: old == new？不可能！
        # fallback to freeze_thaw_cycles  # 349-351: 死代码
```

外层和内层的条件互斥。Entry 4 的 `post_soak_hours` 变化等效于父值时的冻融循环回退永不可达，产出无意义候选，浪费实验槽位。

**修复方向**: 删除内层 `if _numeric_equivalent` 守卫，或重构逻辑使冻融回退无条件激活后重检等效性。

---

### C7 — pipeline_agents.py — 两处 round_idx-1 硬编码读错文件

**文件**: [pipeline_agents.py:301](cycle/src/pva_work_flow/pipeline_agents.py#L301) 和 [:666](cycle/src/pva_work_flow/pipeline_agents.py#L666)
**严重度**: Critical

- 第 301 行：审计完整性检查硬编码 `R{round_idx - 1}_results_filled.csv`，但函数已计算 `source_round`。当 `parent_round_idx ≠ round_idx - 1` 时读错文件。
- 第 666 行：`_validate_and_fix_candidates` 硬编码 `R{round_idx - 1}_candidates.json` 查找父候选。同样的问题。

**修复方向**: 两处都改用 `source_round` 或显式传入的 `parent_round_idx`。

---

### C8 — cli.py — stdout 重定向在 try 外，异常时无法恢复

**文件**: [cli.py:291-295](cycle/src/pva_work_flow/cli.py#L291-L295)
**严重度**: Critical

```python
sys.stdout = Tee(sys.__stdout__, log_f)   # 294: stdout 已替换
sys.stderr = Tee(sys.__stderr__, log_f)   # 295: 如果这行抛异常...
try:                                       # 298: try 从未进入
```

`sys.stdout` 已在 `try` 之前被替换。若 `sys.stderr` 的 Tee 构造失败，`finally` 块不会执行，stdout 永久丢失且 `log_f` 未关闭。

**修复方向**: 将 Tee 初始化移入 `try` 块内。

---

### C9 — generator.py — LLM 输出的 float/int 转换无异常保护

**文件**: [generator.py:1073-1083](cycle/src/pva_work_flow/generator.py#L1073-L1083)
**严重度**: Critical

```python
soak = float(p.get("post_soak_hours") or 0)
ft = int(p.get("freeze_thaw_cycles") or 0)
ch = float(p.get("cycle_hours") or 0)
```

LLM 经常输出 `"1-2"`、`"~3"`、`"2h"`、`"twice"` 等非数字字符串。任何一个抛 `ValueError` 直接崩溃整个候选处理循环，丢弃整轮所有候选。

**修复方向**: 每个转换包裹 `try/except ValueError`，失败时回退到默认值或拒绝该候选。

---

## 核心管线问题

> cli.py / generator.py / workflow.py — 共 19 个问题

| # | 文件:行号 | 严重度 | 问题描述 |
|---|----------|:---:|------|
| M1 | cli.py:479 | Major | `except Exception: pass` 吞噬所有异常类型（MemoryError、KeyboardInterrupt），应只捕获 CSV 相关异常 |
| M2 | cli.py:389-413 | Major | Tree 子目录搜索遗漏 `out_dir` 层的结果 CSV，导致 `last_valid_experimental_round` 为 None |
| M3 | cli.py:403 | Minor | `audits_obj.get("audits", [])` 当 JSON 值为 `null` 时返回 `None`（非 `[]`），应改 `or []` |
| M4 | cli.py:428 | Minor | 不必要的 `getattr(args, "use_external_r1", False)`，`args.use_external_r1` 是 argparse 定义死的属性 |
| M5 | cli.py:542-562 | Minor | 树报告操作裸 `except Exception`，隐藏树构建代码的 bug |
| M6 | generator.py:832-850 | Critical | 3-agent pipeline 无 JSON 解析错误保护（legacy 路径有），原始 LLM 输出丢失 |
| M7 | generator.py:936-938 | Critical | 缺少 parent_candidate_id 时的 RuntimeError 中 `c!r` 输出完整候选字典（可能数千字符） |
| M8 | generator.py:1023-1026 | Major | 材料子串匹配过于宽松——`"ha"` 匹配 `"chitosan"`、`"ethanolamine"` |
| M9 | generator.py:1049-1051 | Major | 添加剂在 allowed_set 和 find_similar 都找不到时静默丢弃，无任何警告 |
| M10 | generator.py:1162 | Major | `network_type` 比较未做 `.lower()`，大小写不一致导致误判 `is_extension` |
| M11 | generator.py:1090-1099 | Minor | 占位符模式过于激进——`"lubricant"` 作为商业产品名会被误删 |
| M12 | generator.py:953-978 | Minor | `read_results_filled` 在循环内为每个候选重读同一 CSV |
| M13 | generator.py:1224-1225 | Minor | `float()` 在 `min()` lambda 中对非数字级别（如 `"high"`）抛异常 |
| M14 | workflow.py:586-608 | Critical | 收敛检查死代码——单轮 delta（行 596）被连续平轮检查（行 607）覆盖，前者从未生效 |
| M15 | workflow.py:618-626 | Major | 趋势数据不足时 `converged=True`（如仅 1 轮），下游可能误以为优化完成 |
| M16 | workflow.py:411 | Major | `_build_structured_doe` 读当前轮候选提取父添加剂——应读 `R{round_idx-1}` |
| M17 | workflow.py:880 | Major | 日志 `"Removed N candidates"` 后列出的实际是幸存候选（`if False` 分支死代码 + `set(best_candidates)`） |
| M18 | workflow.py:4-13 | Minor | YAML 加载只捕获 `FileNotFoundError` 和 `YAMLError`，漏 `PermissionError`、`UnicodeDecodeError` |
| M19 | workflow.py:86-88 | Minor | `pva_wt_percent` 可能含 `"10 wt%"` 字符串，与 float 混用时 `min()`/`max()` 抛 TypeError |

---

## 约束 DOE 系统问题

> constrained_doe.py / formula_materializer.py / pipeline_agents.py / candidate_rules.py / candidate_critic.py — 共 21 个问题

| # | 文件:行号 | 严重度 | 问题描述 |
|---|----------|:---:|------|
| M20 | constrained_doe.py:101-106 | Major | `_numeric_equivalent` 不处理 NaN/Inf——`float('nan')` 比较始终 False，NaN 被静默转 0.0 |
| M21 | constrained_doe.py:278 | Major | 允许生成仅 1 个 entry 的 DOE 计划（下游预期 ≥2），`pipeline_agents.py` 的 ≥2 检查只在 LLM 回退路径 |
| M22 | constrained_doe.py:379 | Major | Entry 5 扰动方向依赖 `len(table) % 2`（非确定性——取决于前面几个 entry 是否有效） |
| M23 | constrained_doe.py:40-45 | Minor | `_audit_failed_ids` 对非 dict 非 str 类型（如 LLM 幻觉输出整数）静默丢弃 |
| M24 | formula_materializer.py:20-22 | Major | `setdefault("crosslinker", {})["wt_percent"] = value` 创建无名 crosslinker，下游 `candidate_rules.py` 读不到 name |
| M25 | formula_materializer.py:113-116 | Major | `baseline_reproduction` 先应用 mutation（行 96-99）再清除 `doe_factor_levels`（行 114）——公式已改变但元数据声称精确复制 |
| M26 | formula_materializer.py:82-83 | Minor | diagnosis 文本硬编码 `round_idx - 1`，但当 `parent_round_idx` 显式传入时写错轮次 |
| M27 | pipeline_agents.py:454-463 | Major | 损坏的 DOE plan（<2 entries）在 `RuntimeError` 抛出前已写入磁盘，后续运行会读到坏文件 |
| M28 | pipeline_agents.py:372-373 | Major | `build_constrained_doe_skeleton` 的 `FileNotFoundError` 被错误回退到 LLM DOE 规划——根因被掩盖 |
| M29 | pipeline_agents.py:863-876 | Major | 实验记录在 ratio_planner/audit 验证前写入 `experiment_records.jsonl`——无效候选污染 RAG 记忆 |
| M30 | pipeline_agents.py:825-831 | Minor | `load_allowed_materials` 失败时 `allowed=[]`（空列表），材料检查静默失效 |
| M31 | pipeline_agents.py:657-659 | Minor | LLM 生成超过 skeleton table 的候选时静默截断，丢弃的候选无日志 |
| M32 | candidate_rules.py:122-135 | Major | 同名异角色添加剂的变量键冲突——第二个 "glycerol"（plasticizer）的键覆盖第一个 "glycerol"（humectant） |
| M33 | candidate_rules.py:322-338 | Major | `new_material_names` 只看 `materials` 数组，忽略 `formulation.additives`、`formulation.crosslinker` |
| M34 | candidate_rules.py:565 | Major | `build_inheritance_table` 只读 `changed_variables`，忽略 `variables_changed` 和 `planned_changed_variables` |
| M35 | candidate_rules.py:241-245 | Major | material relocation 检测过于粗粒度——添加剂新增 metadata 字段时，真实的 wt_percent 变化被抑制 |
| M36 | candidate_rules.py:403 | Minor | `limited_exploration count exceeds 1` 错误消息缺少 candidate_id |
| M37 | candidate_rules.py:380-384 | Minor | `_is_relocated_name` 在循环内每次重建 `candidate_variable_map`（O(n²) 复杂度） |
| M38 | candidate_rules.py:218-220 | Minor | `COUPLED_VARIABLE_PAIRS` 需要手动维护双向排序——新增配对容易遗漏反向 |
| M39 | candidate_critic.py:199 | Major | `n_candidates` 参数名误导——实际控制的是 DOE plan 数量（3 个 plan 各 8 个候选 = 24 个候选） |
| M40 | candidate_critic.py:224-230 | Major | `select_best_candidate` 用 `enumerate(critiques)` 的索引直接索引 `candidates`，长度不一致时 `IndexError` |

---

## 审计与规则问题

> audit.py / rule_checker.py / experiment_notes.py / experiment_state.py / experiment_rag.py / formulation_rag.py — 共 18 个问题

| # | 文件:行号 | 严重度 | 问题描述 |
|---|----------|:---:|------|
| M41 | audit.py:115-116 | Critical | UV 纳米填料检查使用 `m.get("amount")` 而非 `m.get("wt_percent")`——UV 阻挡检测永远不触发 |
| M42 | audit.py:593-607 | Critical | 拒绝原因 if/elif 链只取第一个——化学不可行 + 时间超标时只报前者 |
| M43 | audit.py:798-802 | Critical | FALLBACK 模式生成且通过审计的候选项静默丢弃——R2+ 回退候选无痕迹消失 |
| M44 | audit.py:404 | Major | DI 水警告只 `print` 到 stdout，未进入结构化 `warnings` 列表或持久化 JSON |
| M45 | audit.py:713-714 | Major | 宽泛子串匹配 `"doe_factor_levels"` 将"缺失"和"超出范围"两类不同失败合并 |
| M46 | audit.py:470 | Major | 化学失败消息是自然语言字符串（非结构化），下游无法按错误类型统计/分类 |
| M47 | audit.py:291-292 | Minor | `_infer_crosslinker_from_materials` 搜索 `formulation.additives` 和 `materials`，但未检查 `formulation.crosslinker` |
| M48 | rule_checker.py:178-232 | Critical | `run_all_rule_checks` 缺少 `audit.py` 中的 9 项化学可行性规则——覆盖范围有重大缺口且未文档化 |
| M49 | experiment_notes.py:196-213 | Critical | **中等严重性错误码（ERROR5/7/9）静默丢失**——`has_critical` 和 `has_high` 都不命中时，`experimental_status` 不更新、`failure_mode` 不填充 |
| M50 | experiment_notes.py:261 | Major | `is_candidate_mechanically_failed` 在候选循环中每次重读 notes JSON（N 个候选 = N 次磁盘 I/O） |
| M51 | experiment_notes.py:243-248 | Major | `suggested_action`（如 "increase_crosslink_density_or_pva_wt"）未注入诊断 prompt |
| M52 | experiment_notes.py:217-221 | Major | `suggested_action` 未写入候选的 `_experiment_notes` 子字典 |
| M53 | experiment_state.py:265 | Critical | `failure_notes` 只取原始 `results_filled.csv` 的 `notes` 字段，忽略 `experiment_notes.json` 的错误码 |
| M54 | experiment_state.py:276-278 | Critical | 排名纯按 COF 数值——破裂样品（COF=0.008 的 R1-04）被排为 "excellent" |
| M55 | experiment_rag.py:143-144 | Major | `float(r.get("friction_coefficient", 999))` 对 `"N/A"` 等非数字抛 ValueError |
| M56 | formulation_rag.py:77 | Major | 材料术语循环在第一次迭代就 `break`——仅处理最新一轮，函数名暗示多轮 |
| M57 | formulation_rag.py:156 | Medium | SQLite 连接无超时参数——写锁可能导致管道挂起 |
| M58 | formulation_rag.py:44-53 | Minor | 使用已弃用的 `importlib.util.spec_from_file_location`（Python 3.12+），失败时静默 |

---

## 数据处理问题

> bruker_parser.py / io_artifacts.py / ratio_planner.py / utils.py / formulation_checks.py — 共 22 个问题

| # | 文件:行号 | 严重度 | 问题描述 |
|---|----------|:---:|------|
| M59 | bruker_parser.py:19 | Major | UTF-8 + `errors="ignore"` 静默删除 GBK/GB2312 编码的中文元数据（Bruker 软件常运行在中文 Windows） |
| M60 | bruker_parser.py:70-71 | Minor | 列头正则匹配分号但 split 只用逗号——分号分隔的 CSV 数据行全空 |
| M61 | bruker_parser.py:88-90 | Major | 数据行 float 转换失败静默 `pass`——整列非数字数据被跳过且零诊断 |
| M62 | bruker_parser.py:97-107 | Major | 参数归属逻辑：step 存在时 run 级参数被错误归入 step |
| M63 | bruker_parser.py:534-596 | Minor | `compute_compression_modulus` 种子窗口过小时回退到直接 polyfit（更易受噪音影响），应渐进扩展种子窗口 |
| M64 | bruker_parser.py:583-596 | Minor | 第一次扩展就失败时 `best_end` 停留在 `seed_end`，可能仅用 0-0.015 应变计算模量（过于保守） |
| M65 | bruker_parser.py:428-435 | Major | 元数据/数据边界检测仅凭 `float()` 试探——`"0.5,some description"` 被误当数据 |
| M66 | bruker_parser.py:481 | Major | 厚度/宽度缺失时默认 0——下游应力计算可能除零 |
| M67 | bruker_parser.py:711-713 | Major | `csv_paths` 为空时循环变量 `i` 未定义，`NameError` 崩溃 |
| M68 | bruker_parser.py:662 | Minor | 速度未知时回退 wear proxy 单位是 N·s（动量），与主路径的 mJ（能量）不可比 |
| M69 | io_artifacts.py:79 | Minor | `from .utils import _to_float_or_none` 在函数定义之间，非文件顶部 |
| M70 | io_artifacts.py:98-103 | Minor | 缺失的 COF_std 默认 0.0——把"未测量"等同于"零方差"，系统性低估不确定度 |
| M71 | ratio_planner.py:55-61 | Major | 声称"LHS"但实现是独立 1D 分层采样——非真正多维拉丁超立方，空间填充性差 |
| M72 | ratio_planner.py:234-252 | Major | 总 wt% > 100% 时水被 clamp 到 0，产出物理不可行配方，无警告 |
| M73 | ratio_planner.py:129 | Minor | float 比较无容差——`12.000000000000002` 不在 `[12.0, 14.0]` 范围内 |
| M74 | utils.py:177-179 | Major | `canonicalize_material_name` 子串匹配最小长度 4 排除 `"ha"`、`"ga"`、`"cmc"`、`"peg"` 等常见缩写 |
| M75 | utils.py:381 | Major | `safe_json_loads` 贪婪 `.*` 正则匹配从第一个 `{` 到最后一个 `}`——两个 JSON 对象中间的文本被吞入 |
| M76 | utils.py:395-403 | Major | 正则 JSON 修复可能损坏字符串值内的 `,}` 序列 |
| M77 | utils.py:17-25 | Minor | `_to_float_or_none` 静默返回 None——编程 bug（传入了 list/dict）无法与显式 None 区分 |
| M78 | formulation_checks.py:71-78 | Major | `processing` 为非字典类型时 `p.get()` 抛 `AttributeError` |
| M79 | formulation_checks.py:200-212 | Major | `normalize_materials_and_formulation` 双向同步不传播 `wt_percent`/`amount`——两个表示不一致 |
| M80 | formulation_checks.py:322-350 | Minor | `_require_material` 错误消息不区分"材料不存在"和"角色不匹配" |

---

## 树与记忆系统问题

> formula_tree.py / tree_naming.py / tree_statistics.py / tree_reports.py / tree_visualizer.py / chain_search.py / chain_memory.py / artifact_store.py / budget_manager.py — 共 22 个问题

| # | 文件:行号 | 严重度 | 问题描述 |
|---|----------|:---:|------|
| M81 | formula_tree.py:280-281 | Major | 按 `tree_depth` 拓扑排序不安全——数据损坏时子节点先于父节点，被误判为根 |
| M82 | formula_tree.py:308 | Major | `_render_tree_lines`（渲染函数）执行 `infer_branch_decisions(write=True)`——副作用破坏性 IO |
| M83 | formula_tree.py:182-183 | Minor | 恢复尝试有 COF 改进但有 error 时无条件 kill——可能过于激进 |
| M84 | tree_naming.py:39 | Major | `normalize_tree_label` 对垃圾输入（`"foo-bar"`）原样返回——破坏下游分组 |
| M85 | tree_naming.py:37-39 | Minor | 同一正则匹配执行两次，应存储结果 |
| M86 | tree_reports.py:193-252 | Major | 三个报告文件（SIMPLE_TREE/GLOBAL_TREE/EXPERIMENT_FORMULA）静默覆盖——用户批注丢失 |
| M87 | tree_reports.py:183-187 | Major | 孤立节点静默当根节点处理——应记录警告 |
| M88 | tree_reports.py:205-206 | Minor | `build_global_tree_summary` 级联触发 N 次 `infer_branch_decisions(write=True)` |
| M89 | tree_visualizer.py:429-433 | Major | Tree 无结果时回退到根 workspace 的结果 CSV——**跨树数据污染**（Tree A 的候选可能捡到 Tree B 的 COF） |
| M90 | tree_visualizer.py:193-203 | Minor | `_cof_emoji` 参数 `best_cof` 从未使用（死参数） |
| M91 | tree_visualizer.py:150 | Minor | 中英文混用——`"无添加剂"` 在英文代码库中不一致 |
| M92 | chain_search.py:17-18 | Major | `_read_json` 无错误处理（与其他模块不一致——其他地方都是 try/except 回退 `{}`） |
| M93 | chain_search.py:136-153 | Minor | Trace 不记录未测量子节点数——无法区分"无子节点"和"有子节点但未测量" |
| M94 | chain_memory.py:50 | Major | R1 的 `or not parent_id` 导致所有 R1 候选被当作根——即使有 `parent_candidate_id` |
| M95 | chain_memory.py:82-86 | Minor | `planned_changed_variables` 回退混合意图与实际执行 |
| M96 | artifact_store.py:197 | Major | `exist_ok=False` + 秒精度时间戳 = 同一秒两次归档时 `FileExistsError` |
| M97 | artifact_store.py:148-154 | Major | `budget_manager` 内联导入每次调用都执行（应为模块级惰性导入） |
| M98 | artifact_store.py:263 | Minor | 回退消息 `"inspect missing artifacts"` 不指示哪些文件缺失 |
| M99 | budget_manager.py:30-49 | Major | `infer_stage` 有 2 个死参数（`best_candidate_repeats`, `remaining_budget`） |
| M100 | budget_manager.py:88-105 | Major | `count_completed_by_design_type` 无去重——同候选出现在多个结果文件时多次计数 |
| M101 | budget_manager.py:228 | Minor | `"Budget below 25%%"` 字面渲染为 `25%%`（非 printf，是普通字符串的 typo） |
| M102 | budget_manager.py:108-109 | Minor | `get_remaining_budget` 对 over-budget 静默 clamp 到 0，无警告 |

---

## 架构层面问题

### A1 — 审计失败 ≠ 实验失败：区分正确，但下游传播断裂

`audit.py` 正确分离了 `audit_status`（PASS/WARNING/FAIL）和 `experimental_status`（not_measured/measured/experimental_failed），两者不互相覆盖。

**但断裂点在两处**：

1. **candidate_repairs 在 experiment_notes 过滤之前生成**（[workflow.py:752 vs 862](cycle/src/pva_work_flow/workflow.py#L752)）——对破裂样品（COF 数据不可靠）提出基于 COF 的修复建议
2. **experiment_state.py 排名只看 COF**（[:276-278](cycle/src/pva_work_flow/experiment_state.py#L276-L278)）——破裂的 R1-04（COF=0.008）被排为 "excellent"

### A2 — 错误码传播在两个层次断裂

- `experiment_notes.py` → `experiment_state.py` 的 `failure_notes` 从不查询 notes JSON 文件（M53）
- `experiment_state.py` 排名逻辑不检查 `experimental_status`（M54）
- 中等严重性错误码（ERROR5/7/9）在 `apply_notes_to_candidates` 中静默丢失（M49）
- `suggested_action` 既不注入诊断 prompt（M51）也不写入候选元数据（M52）

### A3 — `parent_round_idx` 线程断裂

5 处 `round_idx - 1` 硬编码分散在多个文件中。当 `parent_round_idx ≠ round_idx - 1`（跳轮、重基线）时出错：

| 位置 | 影响 |
|------|------|
| `pipeline_agents.py:301` | 审计完整性检查读错 CSV |
| `pipeline_agents.py:666` | 父候选查找读错文件 |
| `constrained_doe.py:75` | 上下文正确（读传入的父 dict） |
| `formula_materializer.py:83` | diagnosis 文本写错轮次 |
| `experiment_rag.py` 多处 | RAG 上下文注入错误数据 |

### A4 — `variables_changed` 键名不一致（跨 5 文件 4 种写法）

| 文件 | 使用的键 |
|------|------|
| `constrained_doe.py` `_entry` | `variables_changed`（主）、`changed_variables`（别名） |
| `formula_materializer.py` | `planned_changed_variables` |
| `candidate_rules.py` `build_inheritance_table` | `changed_variables`（仅此一个） |
| `candidate_critic.py` | `changed_variables` → `variables_changed`（回退） |
| `pipeline_agents.py` `_check_inner_rules` | `variables_changed` |

下游静默读取空数据 → 族谱表缺失变量、黑盒风险漏检。

### A5 — audit.py 和 rule_checker.py 覆盖范围不同且未文档化

`rule_checker.run_all_rule_checks`（声称 Layer 1.4）包含 10 条规则，但缺少 `audit._check_chemical_feasibility` 中的 9 条化学可行性规则。调用方以为全检了，实际只覆盖了结构/族谱约束。

### A6 — 每个 round 无错误边界

`one_round()` 内部没有 try/except。generator 中的 `RuntimeError`（格式错误 JSON、缺父候选、killed branch）直接终止整个 `--mode full` pipeline。Round 2 失败 → Round 3+ 永不执行。

---

## 共性问题模式

1. **`except Exception` 裸捕获**：`tree_statistics.py:347`、`chain_memory.py:270`、`cli.py:479`、`cli.py:542`——吞噬 `KeyboardInterrupt`、`MemoryError`
2. **JSON 路径无异常保护**：`chain_search._read_json`、`utils.read_json` 无 try/except——与项目中 `formula_tree._load_json` 的模式不一致
3. **静默数据丢失**：`audit.py` 回退候选丢弃、`generator.py` 添加剂丢弃、`bruker_parser.py` 行跳过——均无计数器或警告
4. **`float()` 无保护**：LLM 输出路径多处假设输入一定是数字（`generator.py`、`experiment_rag.py`、`ratio_planner.py`）
5. **硬编码轮次偏移**：`round_idx - 1` 分散在 5 个文件中——应该统一为显式 `parent_round_idx` 参数
6. **文件静默覆盖**：`tree_reports.py` 三个报告文件、`pipeline_agents.py` DOE plan——均无覆盖前警告
7. **中英文混用**：`tree_visualizer.py` 硬编码中文 `"无添加剂"`，其他位置用英文

---

## 完整问题索引

| 编号 | 文件 | 行号 | 严重度 | 简述 |
|:---:|------|------|:---:|------|
| C1 | tree_visualizer.py | _render_node | Critical | 无限递归（无环检测） |
| C2 | budget_manager.py | 147-195 | Critical | 预算耗尽后仍推荐实验 |
| C3 | cli.py | 359-367 | Critical | 诊断重复执行（双倍 LLM 成本） |
| C4 | cli.py | 509-519 | Critical | 零候选轮次无断路器 |
| C5 | workflow.py | 710-713 | Critical | 候选缺键 KeyError |
| C6 | constrained_doe.py | 346-351 | Critical | Entry 4 冻融回退死代码 |
| C7 | pipeline_agents.py | 301,666 | Critical | round_idx-1 硬编码读错文件 |
| C8 | cli.py | 291-295 | Critical | stdout 重定向异常无法恢复 |
| C9 | generator.py | 1073-1083 | Critical | float/int 转换无保护 |
| M1 | cli.py | 479 | Major | 裸 except Exception |
| M2 | cli.py | 389-413 | Major | Tree 目录遗漏结果 CSV |
| M3 | cli.py | 403 | Minor | audits 可能为 None |
| M4 | cli.py | 428 | Minor | 不必要 getattr |
| M5 | cli.py | 542-562 | Minor | 树报告裸 except |
| M6 | generator.py | 832-850 | Critical | 3-agent 无 JSON 错误保护 |
| M7 | generator.py | 936-938 | Critical | 完整候选 repr 洪流日志 |
| M8 | generator.py | 1023-1026 | Major | 材料子串匹配过松 |
| M9 | generator.py | 1049-1051 | Major | 添加剂静默丢弃 |
| M10 | generator.py | 1162 | Major | network_type 大小写不一致 |
| M11 | generator.py | 1090-1099 | Minor | 占位符模式过激 |
| M12 | generator.py | 953-978 | Minor | 循环内重读 CSV |
| M13 | generator.py | 1224-1225 | Minor | float 在 min lambda 中抛异常 |
| M14 | workflow.py | 586-608 | Critical | 收敛检查死代码 |
| M15 | workflow.py | 618-626 | Major | 数据不足时 converged=True |
| M16 | workflow.py | 411 | Major | 读错轮次的候选 |
| M17 | workflow.py | 880 | Major | 日志幸存/移除反转 |
| M18 | workflow.py | 4-13 | Minor | YAML 加载漏异常类型 |
| M19 | workflow.py | 86-88 | Minor | pva_wt_percent 类型混用 |
| M20 | constrained_doe.py | 101-106 | Major | NaN/Inf 不处理 |
| M21 | constrained_doe.py | 278 | Major | 单候选 DOE 计划 |
| M22 | constrained_doe.py | 379 | Major | Entry 5 方向非确定性 |
| M23 | constrained_doe.py | 40-45 | Minor | 异常类型静默丢弃 |
| M24 | formula_materializer.py | 20-22 | Major | 无名 crosslinker |
| M25 | formula_materializer.py | 113-116 | Major | baseline 先改后清 metadata |
| M26 | formula_materializer.py | 82-83 | Minor | diagnosis 文本轮次硬编码 |
| M27 | pipeline_agents.py | 454-463 | Major | 坏 DOE 先写盘后报错 |
| M28 | pipeline_agents.py | 372-373 | Major | FileNotFound 错误回退到 LLM |
| M29 | pipeline_agents.py | 863-876 | Major | 验证前写入实验记忆 |
| M30 | pipeline_agents.py | 825-831 | Minor | allowed 空列表静默 |
| M31 | pipeline_agents.py | 657-659 | Minor | 候选截断无日志 |
| M32 | candidate_rules.py | 122-135 | Major | 同名添加剂键冲突 |
| M33 | candidate_rules.py | 322-338 | Major | new_materials 忽略 formulation |
| M34 | candidate_rules.py | 565 | Major | inheritance_table 键名不一致 |
| M35 | candidate_rules.py | 241-245 | Major | relocation 检测过粗 |
| M36 | candidate_rules.py | 403 | Minor | 错误消息缺 ID |
| M37 | candidate_rules.py | 380-384 | Minor | 循环内重建变量映射 |
| M38 | candidate_rules.py | 218-220 | Minor | 耦合变量对需手动双向 |
| M39 | candidate_critic.py | 199 | Major | n_candidates 参数名误导 |
| M40 | candidate_critic.py | 224-230 | Major | zip 长度不一致 IndexError |
| M41 | audit.py | 115-116 | Critical | UV 检查用错键 |
| M42 | audit.py | 593-607 | Critical | 拒绝原因掩盖复合失败 |
| M43 | audit.py | 798-802 | Critical | PASS 回退候选静默丢弃 |
| M44 | audit.py | 404 | Major | DI 水警告绕过低结构化日志 |
| M45 | audit.py | 713-714 | Major | 宽泛子串合并两类失败 |
| M46 | audit.py | 470 | Major | 化学失败消息非结构化 |
| M47 | audit.py | 291-292 | Minor | 未检查 crosslinker 字段 |
| M48 | rule_checker.py | 178-232 | Critical | 缺少化学可行性规则 |
| M49 | experiment_notes.py | 196-213 | Critical | 中等错误码静默丢失 |
| M50 | experiment_notes.py | 261 | Major | 循环内重读 notes 文件 |
| M51 | experiment_notes.py | 243-248 | Major | suggested_action 未注入诊断 |
| M52 | experiment_notes.py | 217-221 | Major | suggested_action 未写入元数据 |
| M53 | experiment_state.py | 265 | Critical | failure_notes 忽略 notes JSON |
| M54 | experiment_state.py | 276-278 | Critical | 排名不检查 experimental_status |
| M55 | experiment_rag.py | 143-144 | Major | float 非数字崩溃 |
| M56 | formulation_rag.py | 77 | Major | break 仅处理单轮 |
| M57 | formulation_rag.py | 156 | Medium | SQLite 无超时 |
| M58 | formulation_rag.py | 44-53 | Minor | 弃用 importlib API |
| M59 | bruker_parser.py | 19 | Major | 编码假设 + 静默删除 |
| M60 | bruker_parser.py | 70-71 | Minor | 分号分隔未处理 |
| M61 | bruker_parser.py | 88-90 | Major | 数据行静默跳过 |
| M62 | bruker_parser.py | 97-107 | Major | 参数归属错误 |
| M63 | bruker_parser.py | 534-596 | Minor | 种子窗口过小 |
| M64 | bruker_parser.py | 583-596 | Minor | 首次扩展失败即停 |
| M65 | bruker_parser.py | 428-435 | Major | 元数据/数据边界脆弱 |
| M66 | bruker_parser.py | 481 | Major | 厚度默认 0（除零风险） |
| M67 | bruker_parser.py | 711-713 | Major | 空 csv_paths 致 NameError |
| M68 | bruker_parser.py | 662 | Minor | 回退 wear proxy 单位错误 |
| M69 | io_artifacts.py | 79 | Minor | 文件中部 import |
| M70 | io_artifacts.py | 98-103 | Minor | 缺失 std 默认 0 |
| M71 | ratio_planner.py | 55-61 | Major | 假 LHS 实现 |
| M72 | ratio_planner.py | 234-252 | Major | wt% > 100% 静默 clamp |
| M73 | ratio_planner.py | 129 | Minor | float 比较无容差 |
| M74 | utils.py | 177-179 | Major | 短缩写被排除 |
| M75 | utils.py | 381 | Major | 正则捕获跨 JSON 对象文本 |
| M76 | utils.py | 395-403 | Major | 正则修复损坏字符串值 |
| M77 | utils.py | 17-25 | Minor | _to_float_or_none 信息丢失 |
| M78 | formulation_checks.py | 71-78 | Major | processing 非 dict 时 AttributeError |
| M79 | formulation_checks.py | 200-212 | Major | 双向同步不传播量 |
| M80 | formulation_checks.py | 322-350 | Minor | 错误消息不精确 |
| M81 | formula_tree.py | 280-281 | Major | 拓扑排序不安全 |
| M82 | formula_tree.py | 308 | Major | 渲染函数破坏性 IO |
| M83 | formula_tree.py | 182-183 | Minor | 恢复尝试无条件 kill |
| M84 | tree_naming.py | 39 | Major | 垃圾输入原样返回 |
| M85 | tree_naming.py | 37-39 | Minor | 双重正则匹配 |
| M86 | tree_reports.py | 193-252 | Major | 报告静默覆盖 |
| M87 | tree_reports.py | 183-187 | Major | 孤立节点静默变根 |
| M88 | tree_reports.py | 205-206 | Minor | 级联副作用 |
| M89 | tree_visualizer.py | 429-433 | Major | 跨树数据污染 |
| M90 | tree_visualizer.py | 193-203 | Minor | 死参数 best_cof |
| M91 | tree_visualizer.py | 150 | Minor | 中英文混用 |
| M92 | chain_search.py | 17-18 | Major | _read_json 无错误处理 |
| M93 | chain_search.py | 136-153 | Minor | Trace 缺子节点计数 |
| M94 | chain_memory.py | 50 | Major | R1 全部当根 |
| M95 | chain_memory.py | 82-86 | Minor | planned 回退混合意图 |
| M96 | artifact_store.py | 197 | Major | 竞态条件 |
| M97 | artifact_store.py | 148-154 | Major | 内联导入每次执行 |
| M98 | artifact_store.py | 263 | Minor | 回退消息无信息量 |
| M99 | budget_manager.py | 30-49 | Major | 死参数 |
| M100 | budget_manager.py | 88-105 | Major | 无去重 |
| M101 | budget_manager.py | 228 | Minor | 25%% typo |
| M102 | budget_manager.py | 108-109 | Minor | over-budget 无警告 |

---

## 建议修复优先级

### 第一优先级（Critical — 影响安全性/正确性/成本）

C1 无限递归 → C2 预算失效 → C4 零候选断路器 → C5 KeyError → C6 死代码 → C7 读错文件 → C8 stdout 损坏 → C3 双倍 LLM 成本 → C9 float 崩溃 → M41 UV 检测用错键 → M42 拒绝原因掩盖 → M43 回退候选丢弃 → M48 规则检查缺化学规则 → M49 中等错误码丢失 → M53 failure_notes 忽略 notes → M54 排名忽略 mechanical failure

### 第二优先级（Major — 影响数据可信度）

M14 收敛死代码 → M2/M15/M16/M17 pipeline 逻辑错误 → M25/M34/A4 键名不一致 → M67 空 csv_paths → M71 假 LHS → M72 不可行配方 → M50 O(n²) I/O → M54 跨树数据污染 → A3 parent_round_idx 断裂 → A1 诊断修复过早

### 第三优先级（Minor — 技术债务/可维护性）

M3-M5, M11-M13, M18-M19, M23, M26, M30-M31, M36-M38, M47, M57-M58, M60, M63-M64, M68-M70, M73, M77, M80, M83, M85, M88, M90-M91, M93, M95, M98, M101-M102
