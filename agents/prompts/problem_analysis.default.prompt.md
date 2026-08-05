---
id: problem_analysis.default
stage: PROBLEM_ANALYSIS
variant: default
version: 1
input_schema: {"type": "object", "required": ["problem_statement"], "properties": {"problem_statement": {"type": "string"}, "attachments_summary": {"type": "string"}}}
output_schema: {"type": "object", "required": ["problem_type", "objectives", "constraints", "data_requirements"], "properties": {"problem_type": {"type": "string"}, "objectives": {"type": "array", "items": {"type": "string"}}, "constraints": {"type": "array", "items": {"type": "string"}}, "data_requirements": {"type": "array", "items": {"type": "string"}}, "key_assumptions": {"type": "array", "items": {"type": "string"}}}}
---
你是数学建模竞赛的资深教练。请通读下面的赛题，提取建模所需的结构化信息。

## 赛题原文

{{problem_statement}}

## 附件摘要

{{attachments_summary}}

## 输出要求

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `problem_type`：问题类型（如 优化 / 预测 / 评价 / 机理建模 / 混合）。
- `objectives`：需要回答的目标问题列表，逐条对应题目小问。
- `constraints`：题目明确给出的约束与边界条件列表。
- `data_requirements`：完成建模需要的数据清单（含题目附带与需自行收集）。
- `key_assumptions`：为使问题可解而需要显式声明的关键假设列表。
