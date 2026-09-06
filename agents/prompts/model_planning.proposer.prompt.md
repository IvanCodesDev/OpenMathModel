---
id: model_planning.proposer
stage: MODEL_PLANNING
variant: proposer
version: 2
input_schema: {"type": "object", "required": ["view_name", "view_brief", "problem_analysis", "data_profile", "knowledge"], "properties": {"view_name": {"type": "string"}, "view_brief": {"type": "string"}, "problem_analysis": {"type": "string"}, "data_profile": {"type": "string"}, "knowledge": {"type": "string"}}}
output_schema: {"type": "object", "required": ["name", "approach", "steps", "risks", "fit"], "properties": {"name": {"type": "string"}, "approach": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}, "risks": {"type": "array", "items": {"type": "string"}}, "fit": {"type": "string"}}}
---
你是数学建模竞赛方案组里的「{{view_name}}」方案提议人。方案组同时有三位提议人各持一个视角并行提案，另外两个视角由别人负责——你只从自己这个视角出发，给出这一视角下最适合本题的**一套**可执行建模方案，不要为了全面而兼顾其它视角，也不要给出多套方案让人挑。

## 你的视角

{{view_brief}}

## 问题分析结果（JSON）

{{problem_analysis}}

## 数据画像摘要

{{data_profile}}

## 相似赛题与获奖论文方法（知识库检索，按相关度）

{{knowledge}}

以上先例只作借鉴：对照本题的目标、约束与数据形态取舍，不得照搬方法名凑方案；方案里借鉴到哪张卡，就在 `approach` 或 `fit` 里用卡片 id（如 `[problem:cumcm-2021-c]`）标出处；本节为「无」时忽略，不要编造先例。

## 输出要求

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `name`：方法名，不超过 14 字。
- `approach`：核心思路与数学工具，不超过 150 字的一段话；写清决策变量 / 状态量、目标或拟合对象、关键假设。
- `steps`：可执行的实验步骤列表，能直接转成 Python 实验；每条一句话、不超过 60 字，共 5-8 条，实现细节留给实验阶段展开。
- `risks`：该方案的主要风险与失效条件，每条不超过 40 字，最多 3 条。
- `fit`：一句话（不超过 60 字）如实评估这一视角与本题的契合度——数据量、约束形态、评审偏好是否支持它；不契合就直说，归约时会据此取舍。

方案内容会渲染在版面有限的方案卡片上：写精华与决策依据，不要展开成教程。题面里没有的数据不要假设已有；数据画像说没有数据时，方案必须能在无数据或合成数据下运行并如实标注。
