---
id: data_preparation.default
stage: DATA_PREPARATION
variant: default
version: 4
input_schema: {"type": "object", "required": ["problem_analysis"], "properties": {"problem_analysis": {"type": "string"}, "attachments_summary": {"type": "string"}}}
output_schema: {"type": "object", "required": ["profile_summary", "datasets", "preparation_steps"], "properties": {"profile_summary": {"type": "string"}, "datasets": {"type": "array", "items": {"type": "object", "required": ["name", "source", "fields"], "properties": {"name": {"type": "string"}, "source": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}}, "quality_risks": {"type": "array", "items": {"type": "string"}}}}}, "preparation_steps": {"type": "array", "items": {"type": "string"}}, "missing_value_strategy": {"type": "string"}, "outlier_strategy": {"type": "string"}, "derived_features": {"type": "array", "items": {"type": "string"}}, "progress_note": {"type": "string"}}}
---
你是数学建模竞赛团队的数据工程师。基于问题分析结果与附件摘要，产出数据准备方案与数据画像。

## 问题分析结果（JSON）

{{problem_analysis}}

## 附件摘要

{{attachments_summary}}

## 输出要求

题目附带数据时以附件摘要为准描述真实字段；没有附带数据时，按题意列出需要收集或构造的数据（source 注明「需构造」或「需收集」），不得虚构不存在的真实数据来源。

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `profile_summary`：一段话的数据画像摘要（数据构成、规模量级、质量状况与可用性结论），不超过 120 字，将直接供建模方案阶段引用；规模用可核查的数字表述（条数/字段数/时间跨度，构造数据则给出建议构造规模）。
- `datasets`：数据清单，每项包含：
  - `name`（数据集名）、`source`（来源：题目附件 / 需收集 / 需构造）、
  - `fields`（字段清单，每项格式「字段名：一句含义（单位）」，单项不超过 24 字；字段很多时把同构字段合并成一项，如「x1…x9：九项评分（分）」）、
  - `quality_risks`（该数据集的质量风险，每条不超过 40 字，最多 3 条，只写最关键的）。
- `preparation_steps`：可执行的数据准备步骤列表（清洗、对齐、归一化、划分等），按执行顺序排列；每条一句话、不超过 45 字，共 5-10 条。
- `missing_value_strategy`：缺失值处理策略与一句话理由，不超过 100 字。
- `outlier_strategy`：异常值识别与处理策略，不超过 100 字。
- `derived_features`：建议构造的衍生变量列表，每项「名称：构造式」不超过 24 字，最多 8 项，只列建模真正会用到的。
- `progress_note`：两三句面向用户的进度汇报（数据侧的核心结论、最需要注意的质量风险、接下来建模方案阶段会怎么用这些数据），口语化，不要罗列字段清单；它会直接显示在任务页的执行过程里。

这些内容会渲染在版面有限的报告页上：宁可少而精，不要把细节塞进单条文本；超出上限的细节留给后续阶段，不写。
