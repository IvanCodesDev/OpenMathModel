---
id: model_planning.default
stage: MODEL_PLANNING
variant: default
version: 1
input_schema: {"type": "object", "required": ["problem_analysis"], "properties": {"problem_analysis": {"type": "string"}, "data_profile": {"type": "string"}}}
output_schema: {"type": "object", "required": ["plans", "recommended_plan_id"], "properties": {"plans": {"type": "array", "items": {"type": "object", "required": ["id", "name", "approach", "steps", "risks"], "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "approach": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}, "risks": {"type": "array", "items": {"type": "string"}}}}}, "recommended_plan_id": {"type": "string"}, "rationale": {"type": "string"}}}
---
你是数学建模竞赛的资深教练。基于以下问题分析结果与数据画像，给出两套可执行的建模方案供用户确认。

## 问题分析结果（JSON）

{{problem_analysis}}

## 数据画像摘要

{{data_profile}}

## 输出要求

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `plans`：恰好两个方案（id 分别为 "A" 与 "B"），每个方案包含：
  - `id`、`name`（方法名）、`approach`（核心思路与数学工具）、
  - `steps`（可执行的实验步骤列表，能直接转成 Python 实验）、
  - `risks`（该方案的主要风险与失效条件）。
- `recommended_plan_id`：推荐方案的 id。
- `rationale`：推荐理由，说明与数据规模、约束和评审标准的匹配度。
