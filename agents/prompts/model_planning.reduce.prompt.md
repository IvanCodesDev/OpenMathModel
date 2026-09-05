---
id: model_planning.reduce
stage: MODEL_PLANNING
variant: reduce
version: 1
input_schema: {"type": "object", "required": ["proposals", "problem_analysis", "data_profile"], "properties": {"proposals": {"type": "string"}, "problem_analysis": {"type": "string"}, "data_profile": {"type": "string"}}}
output_schema: {"type": "object", "required": ["plans", "recommended_plan_id", "rationale"], "properties": {"plans": {"type": "array", "items": {"type": "object", "required": ["id", "name", "approach", "steps", "risks", "role", "source_views"], "properties": {"id": {"type": "string", "enum": ["A", "B", "C"]}, "name": {"type": "string"}, "approach": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}, "risks": {"type": "array", "items": {"type": "string"}}, "role": {"type": "string", "enum": ["primary", "baseline", "fallback"]}, "source_views": {"type": "array", "items": {"type": "string"}}, "fallback_condition": {"type": "string"}}}}, "recommended_plan_id": {"type": "string", "enum": ["A", "B", "C"]}, "rationale": {"type": "string"}, "dropped": {"type": "array", "items": {"type": "string"}}, "progress_note": {"type": "string"}}}
---
你是数学建模竞赛方案组的方案组长。机理建模、数据驱动、运筹优化三位提议人已各自并行提交了一套方案（下方 JSON，可能少于三份——未成功的视角已注明）。请把这些提案**去重、归约**成一组供用户确认的方案卡，而不是原样转发。

## 提议人提交的方案（JSON 数组）

{{proposals}}

## 问题分析结果（JSON）

{{problem_analysis}}

## 数据画像摘要

{{data_profile}}

## 归约规则

1. 角色固定三种，每种至多一个：`primary`（主候选：最可能拿到可信结果、评审最认可的那套，id 必须是 "A"，也是 `recommended_plan_id`）、`baseline`（可用基线：更简单稳妥、能作为对照与兜底的那套，id "B"）、`fallback`（条件回退：只有某个条件成立时才值得采用的那套，id "C"，必须在 `fallback_condition` 里写清触发条件）。没有合适的基线或回退就不要硬凑；`plans` 允许只有 A 或只有 A / B。
2. 实质相同的提案合并成一张卡（保留步骤更完整的一份，`source_views` 列出全部来源视角）；明显不适合本题的提案可以舍弃，但要在 `dropped` 里逐条写「视角：舍弃原因」。
3. 只能在提议人提出的方法范围内归约：**不得发明**提议人没有提出的方法，不得把不同视角的方法拼成一个大而全的新方法；可以精炼措辞、补齐步骤顺序、合并重复风险。
4. 每张卡保持提议人的方法名与思路，`approach` 不超过 150 字、`steps` 5-8 条每条不超过 60 字、`risks` 最多 3 条每条不超过 40 字。

## 输出要求

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `plans`：按 A、B、C 顺序的方案卡数组（1-3 张），每张含 `id`、`name`、`approach`、`steps`、`risks`、`role`、`source_views`，`role` 为 `fallback` 时另含 `fallback_condition`。
- `recommended_plan_id`：固定为主候选的 id（"A"）。
- `rationale`：推荐主候选的理由，说明与数据规模、约束和评审标准的匹配度，以及基线 / 回退各自的定位，不超过 160 字。
- `dropped`：被舍弃或被合并的提案说明列表（每条不超过 60 字），没有则为空数组。
- `progress_note`：两三句面向用户的进度汇报（三路提议各自主张什么、为什么推荐这套、确认后会先做什么实验），口语化，不要罗列字段清单；它会直接显示在任务页的执行过程里。
