# 文献结构化数据 RAG 分层建设方案

## 目标

本项目已经通过微调数据集训练模型，使模型掌握了 PVA 水凝胶闭环优化任务的输出格式、推理风格和实验流程。接下来要增强的不是“格式能力”，而是把**文献中的结构化知识**接入模型，让模型在生成配方、审计方案和诊断机制时，可以检索到外部科学依据。

这里的 RAG 重点不是普通文本段落检索，而是文献结构化数据检索。

文献 RAG 应该回答这类问题：

- 某种材料在 PVA 水凝胶中通常扮演什么角色？
- 某个交联体系需要什么催化剂、pH、温度或时间条件？
- 某个添加剂对摩擦、含水率、模量、溶胀、磨损有什么已报道影响？
- 某个配方窗口是否和文献中的成功区间接近？
- 某个失败现象是否能从文献中找到机制解释？
- 当前模型提出的新材料或新工艺是否有文献支持？

一句话：**微调模型负责会做项目任务，结构化文献 RAG 负责提供可检索的科学先验。**

## 总体架构

建议把知识分成两类，不能混在一起：

1. **项目实验记忆**
   来自本项目自己的实验结果，例如 `R*_results_filled.csv`、`R*_candidates.json`、`formula_branch_decisions.json`。

2. **文献结构化知识**
   来自论文、综述、专利、协议或材料手册，记录材料、体系、工艺、性能和证据来源。

模型使用时应明确区分：

```text
项目已验证证据：
...

外部文献先验：
...

规则：项目实验结果优先级高于文献先验；文献先验只能作为假设来源或约束参考。
```

## 文献结构化数据的最小单元

不要只把论文切成 chunk。对这个项目更有价值的是把文献抽成结构化事实。

推荐最小事实单元叫做 `literature_fact`。

### literature_fact 字段

```json
{
  "fact_id": "litfact_000001",
  "paper_id": "doi_or_local_id",
  "source_title": "paper title",
  "source_year": 2024,
  "source_type": "paper | review | patent | protocol | datasheet",
  "material_system": "PVA hydrogel",
  "base_polymer": ["PVA"],
  "additives": ["sodium hyaluronate"],
  "crosslinking_system": "freeze-thaw | GA-HCl | borax | UV | dual-network",
  "process_conditions": {
    "pva_wt_percent": "8-15",
    "freeze_thaw_cycles": "1-5",
    "temperature_C": null,
    "post_soak_hours": null,
    "pH": null
  },
  "property_target": "friction | wear | modulus | swelling | toughness | hydration",
  "reported_effect": "reduced COF under hydrated sliding",
  "direction": "increase | decrease | optimum | risk | requirement | incompatible",
  "numeric_value": {
    "metric": "COF",
    "value": null,
    "unit": "",
    "range": ""
  },
  "mechanism": "hydrated boundary lubrication from polysaccharide chains",
  "evidence_strength": "high | medium | low",
  "applicability_to_project": "direct | partial | weak",
  "project_relevance_note": "DI-water lubrication is similar, but load/speed may differ.",
  "quote_or_paraphrase": "short evidence statement",
  "source_locator": "page/table/figure/section",
  "tags": ["lubricant", "hydration", "PVA", "low friction"]
}
```

## 推荐的文献结构化表

当进入数据库层后，不建议只建一张大表。可以拆成以下几类。

### 1. papers

记录文献来源。

字段：

- `paper_id`
- `title`
- `authors`
- `year`
- `doi`
- `source_type`
- `journal_or_source`
- `local_file`
- `reliability_note`

### 2. material_roles

记录材料在水凝胶体系中的作用。

字段：

- `material`
- `alias`
- `role`
- `target_property`
- `compatible_system`
- `typical_range`
- `risk`
- `evidence_paper_id`

示例：

```text
Sodium hyaluronate | lubricant/hydration additive | friction | PVA hydrogel | 0.1-2 wt% | viscosity and leaching risk
```

### 3. crosslinking_rules

记录交联体系规则。

字段：

- `system_name`
- `required_components`
- `required_conditions`
- `forbidden_conditions`
- `typical_range`
- `failure_risk`
- `mechanism`
- `evidence_paper_id`

示例：

```text
GA-HCl | requires glutaraldehyde + acid catalyst | acidic condition | missing acid catalyst | covalent acetal crosslinking
```

### 4. property_effects

记录材料或工艺对性能的影响。

字段：

- `factor`
- `factor_type`
- `material_system`
- `property`
- `direction`
- `effect_summary`
- `numeric_metric`
- `numeric_range`
- `conditions`
- `mechanism`
- `evidence_paper_id`

示例：

```text
freeze_thaw_cycles | process | PVA | modulus | increase | more crystalline domains increase stiffness
```

### 5. incompatibilities

记录禁忌组合和风险。

字段：

- `component_a`
- `component_b`
- `condition`
- `risk_type`
- `risk_description`
- `recommended_fix`
- `evidence_paper_id`

### 6. protocols

记录可借鉴的实验制备流程。

字段：

- `protocol_id`
- `material_system`
- `steps`
- `temperature`
- `duration`
- `batch_basis`
- `critical_notes`
- `evidence_paper_id`

### 7. literature_facts

记录无法完全归类但仍有价值的结构化事实。

字段可采用前面 `literature_fact` 的通用 schema。

## 第 0 层：人工整理的小型文献表

### 适合的数据规模

- 5 到 20 篇核心文献
- 50 到 300 条结构化事实
- 初期只服务 PVA 水凝胶和低摩擦设计

### 存储方式

使用 CSV 或 JSONL。

推荐文件：

- `literature_facts.jsonl`
- `material_roles.csv`
- `crosslinking_rules.csv`
- `property_effects.csv`
- `incompatibilities.csv`

### 检索方式

使用精确字段过滤：

- `base_polymer = PVA`
- `additives contains Sodium hyaluronate`
- `property_target = friction`
- `crosslinking_system = GA-HCl`
- `direction = risk`

### 接入方式

在生成候选前，按当前候选或目标父配方检索相关文献事实，注入 3 到 8 条。

适合注入到：

- `run_generator`
- `run_auditor_rulebased`
- `run_diagnose`

### 优点

- 成本最低
- 可人工检查
- 不需要 embedding
- 适合快速验证文献 RAG 是否有用

### 局限

- 人工整理成本高
- 覆盖面有限
- 不能处理大量论文

## 第 1 层：半自动抽取的 JSONL 文献事实库

### 适合的数据规模

- 20 到 100 篇文献
- 300 到 3,000 条结构化事实

### 存储方式

使用 JSONL，每条记录是一条结构化事实。

推荐目录：

```text
literature_rag/
  papers.jsonl
  literature_facts.jsonl
  material_roles.jsonl
  crosslinking_rules.jsonl
  property_effects.jsonl
  incompatibilities.jsonl
  protocols.jsonl
```

### 数据生产流程

1. PDF 或 HTML 文献进入原始文档目录。
2. 抽取摘要、方法、结果、表格、图注。
3. 用模型或脚本抽成结构化事实。
4. 人工抽查高风险字段。
5. 写入 JSONL。

### 必须保留的溯源字段

每条事实必须有：

- `paper_id`
- `source_title`
- `source_year`
- `source_locator`
- `evidence_strength`
- `applicability_to_project`

没有来源的文献事实不应进入 RAG。

### 检索方式

先用字段过滤，再用关键词匹配。

例如：

```text
查询目标：PVA + HA 添加剂 + friction

过滤：
- base_polymer contains PVA
- additives contains HA 或 Sodium hyaluronate
- property_target in friction, hydration, wear

排序：
- applicability_to_project: direct > partial > weak
- evidence_strength: high > medium > low
- source_year: 新文献略优先，但不覆盖高质量旧文献
```

## 第 2 层：SQLite 文献结构化数据库

### 适合的数据规模

- 100 到 1,000 篇文献
- 3,000 到 50,000 条结构化事实
- 需要稳定 join、筛选和统计

### 存储方式

使用 SQLite。

推荐表：

- `papers`
- `literature_facts`
- `material_roles`
- `crosslinking_rules`
- `property_effects`
- `incompatibilities`
- `protocols`
- `aliases`
- `project_material_mapping`

### 为什么文献结构化数据也应先用 SQLite

因为很多文献 RAG 问题是结构化查询：

- 哪些文献支持 HA 降低水凝胶摩擦？
- PVA 冻融次数通常在哪个范围？
- GA 交联必须满足哪些条件？
- 哪些材料可能在 DI water 中流失？
- 哪些添加剂提高润滑但可能降低模量？
- 哪些文献条件最接近本项目的 10 N、DI water、低速摩擦？

这些问题用 SQL 比直接向量检索更可靠。

### 检索方式

典型 SQL 检索：

- 根据材料名和 alias 找相关事实
- 根据 property target 找效果方向
- 根据 crosslinking system 找必需条件和禁忌
- 根据当前候选配方找文献支持和风险
- 根据项目目标找候选材料列表

### 与项目数据的关系

建议 SQLite 中同时保留一个映射表：

```text
project_material_mapping
```

用于处理文献名和项目材料白名单之间的映射：

- `HA` -> `Sodium hyaluronate`
- `polyvinyl alcohol` -> `PVA (polyvinyl alcohol)`
- `GA` -> `Glutaraldehyde`
- `DI water` -> `deionized water`

### 输出给模型的上下文格式

不要直接输出数据库行。应该整理成证据块：

```text
外部文献结构化证据：

1. Sodium hyaluronate 在 PVA 或亲水水凝胶中常作为 hydration/lubrication additive。
   - 作用方向：降低 hydrated sliding friction
   - 机制：形成水化边界层
   - 适用性：partial，文献载荷/速度与本项目不同
   - 来源：paper_id=...

2. GA-HCl 交联体系需要酸性催化条件。
   - 风险：缺少酸催化剂时交联不足
   - 对本项目规则影响：若使用 GA，候选必须包含 HCl 或酸催化说明
   - 来源：paper_id=...
```

## 第 3 层：SQLite + 向量检索的混合文献 RAG

### 适合的数据规模

- 1,000 篇以上文献
- 50,000 条以上结构化事实
- 仍保留大量原文 chunk、图注、表格说明

### 存储方式

SQLite 保存结构化事实和溯源。

向量库保存：

- 摘要
- 方法段落
- 结果段落
- 图注
- 表格说明
- 结构化事实的自然语言摘要

可选向量库：

- LanceDB
- Chroma
- Qdrant local
- SQLite vector extension

### 检索方式

采用混合策略：

1. SQL 先筛选材料体系、性能目标、工艺条件。
2. 向量检索找相似机制描述。
3. 用结构化字段重排结果。
4. 输出带来源的文献证据块。

示例：

```text
当前问题：
PVA + PEG 400 添加后，水泡时间增加是否可能影响摩擦稳定性？

SQL 过滤：
- material contains PEG
- property in friction, swelling, hydration, leaching
- medium contains water

向量查询：
"water soluble plasticizer leaching during soaking affects hydrogel friction"
```

### 适合解决的问题

当问题更接近“机制解释”而不是简单字段查询时，才需要向量检索。

例如：

- 摩擦曲线出现 stick-slip，可能与哪些文献机制相关？
- 高交联密度导致低磨损但高摩擦，有哪些类似报道？
- 某个添加剂同时改变水化、模量和表面润滑，如何解释？

## 第 4 层：文献事实图谱

### 适合的数据规模

- 大量材料
- 大量性能关系
- 需要做路径推理或候选材料推荐

### 存储方式

可以继续用 SQLite 表模拟图谱，也可以使用图数据库。

节点：

- material
- polymer
- additive
- crosslinker
- process
- property
- mechanism
- risk
- paper

边：

- improves
- worsens
- requires
- incompatible_with
- reported_in
- mechanistically_explained_by
- applicable_to

### 使用方式

这一层不是初期必须。

只有当你希望系统主动推荐材料组合、解释多跳机制、寻找替代材料时，才需要进入图谱层。

示例查询：

```text
找出：
能降低 friction，
不显著降低 modulus，
与 PVA 和 DI water 相容，
且有至少 2 篇文献支持的 additive。
```

## 文献事实抽取流程

### 步骤 1：文献筛选

优先收集：

- PVA hydrogel
- hydrogel lubrication
- hydrated friction
- artificial cartilage hydrogel
- freeze-thaw PVA
- GA crosslinked PVA
- HA / mucin / PEG / CMC 等添加剂

### 步骤 2：分区抽取

不要整篇一次抽。

按区域抽取：

- abstract
- methods
- results
- discussion
- tables
- figure captions

### 步骤 3：结构化抽取

针对不同区域使用不同 schema：

- methods -> `protocols`
- results -> `property_effects`
- discussion -> `mechanism`
- tables -> `numeric_value`
- materials section -> `material_roles`

### 步骤 4：人工校验

至少校验这些高风险字段：

- 数值范围
- 单位
- 材料名称
- 交联条件
- 是否适用于本项目
- 是否只是作者假设，而不是实验结果

### 步骤 5：别名归一化

建立 alias 表。

示例：

```text
HA -> Sodium hyaluronate
hyaluronic acid sodium salt -> Sodium hyaluronate
PVA -> PVA (polyvinyl alcohol)
GA -> Glutaraldehyde
DI water -> deionized water
```

### 步骤 6：项目适用性标注

每条文献事实都要标注对本项目的适用性：

- `direct`：材料体系、介质、性能目标接近
- `partial`：机制相关，但实验条件不同
- `weak`：只是远距离启发

## 小批量到大批量的推荐路线

| 阶段 | 数据规模 | 存储 | 检索 | 重点 |
| --- | --- | --- | --- | --- |
| 第 0 层 | 5-20 篇文献，50-300 条事实 | CSV/JSONL | 字段过滤 | 验证文献结构化 RAG 是否有用 |
| 第 1 层 | 20-100 篇文献，300-3,000 条事实 | JSONL 事实库 | 字段 + 关键词 | 半自动抽取和人工抽查 |
| 第 2 层 | 100-1,000 篇文献，3,000-50,000 条事实 | SQLite | SQL | 稳定结构化检索 |
| 第 3 层 | 1,000+ 文献，50,000+ 事实 | SQLite + 向量库 | SQL + 语义检索 | 机制相似案例检索 |
| 第 4 层 | 大规模多材料体系 | SQLite/图数据库 | 图查询 | 材料推荐和多跳机制推理 |

## 对当前代码的推荐接入点

### 1. 生成候选前

接入 `run_generator`。

用途：

- 给出材料选择依据
- 给出禁忌条件
- 给出推荐工艺窗口
- 给出当前候选变量的文献先验

### 2. 审计候选时

接入 `audit_candidate` 或 `run_auditor_rulebased`。

用途：

- 检查文献中的硬条件
- 检查材料相容性
- 检查交联体系是否缺组分
- 检查工艺是否超出合理范围

### 3. 诊断实验结果时

接入 `run_diagnose`。

用途：

- 用文献机制解释摩擦曲线和失效现象
- 找相似文献案例
- 生成下一轮可解释的优化方向

## 检索优先级

构造 prompt 时建议按以下顺序：

1. 当前候选或父配方的项目实验事实
2. 项目材料白名单和硬规则
3. 同材料体系的文献结构化事实
4. 同性能目标的文献结构化事实
5. 机制相似的文献事实
6. 原文 chunk 或长段文献解释

文献不能覆盖项目已测数据。

## 注入上下文预算

建议每次注入：

- 生成候选：5 到 8 条文献事实
- 审计：8 到 12 条规则/风险事实
- 诊断：8 到 15 条机制/性能事实

每条事实必须短，并带来源。

## 反模式

- 不要只做 PDF chunk 向量检索，而不做结构化字段。
- 不要把文献事实和项目实验事实混为一谈。
- 不要让模型直接相信无来源事实。
- 不要忽略单位和实验条件。
- 不要把综述中的泛化结论当成具体实验数值。
- 不要用文献支持模型违反项目硬约束。
- 不要在小数据阶段上复杂图数据库。

## 最小可行第一步

最小实现不需要数据库。

先做一个小型文献结构化表：

```text
literature_rag/literature_facts.jsonl
literature_rag/material_roles.csv
literature_rag/crosslinking_rules.csv
literature_rag/property_effects.csv
literature_rag/incompatibilities.csv
```

然后实现一个统一检索对象：

```text
LiteratureRagContext
```

它只需要返回：

- 与当前材料相关的事实
- 与当前交联体系相关的规则
- 与当前目标性能相关的影响方向
- 与当前风险相关的文献提醒

先把它注入 generator 和 audit，再观察模型输出是否更稳定、更有依据。
