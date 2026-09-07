---
id: paper_finalize.default
stage: PAPER_WRITING
variant: finalize
version: 3
input_schema: {"type": "object", "required": ["title", "digests", "metrics", "validation_summary", "frozen_numbers"], "properties": {"title": {"type": "string"}, "digests": {"type": "string"}, "metrics": {"type": "string"}, "validation_summary": {"type": "string"}, "frozen_numbers": {"type": "string"}}}
output_schema: {"type": "object", "required": ["abstract", "keywords"], "properties": {"abstract": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}, "progress_note": {"type": "string"}}}
---
你是数学建模竞赛论文《{{title}}》的统稿人。全文各章已完成，下面是各章摘要与实验的核心指标。现在收口：写出终版摘要与关键词。

## 各章摘要（按章节顺序）

{{digests}}

## 实验核心指标（JSON）

{{metrics}}

## 检验结论

{{validation_summary}}

## 数字冻结清单

{{frozen_numbers}}

## 输出要求

只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码围栏，字段如下：

- `abstract`：终版摘要，300-500 字，按「问题背景一句 → 每个子问题用了什么模型方法 → 核心结果（必须带具体数值，数值只能来自数字冻结清单、上面的指标与各章摘要，保持原样不改写）→ 结论与建议」展开；检验结论中的保留意见必须如实体现，不得淡化成通过；摘要里不出现「图 N」「表 N」与 `[1]` 之类的引用标记。
- `keywords`：4-6 个终版关键词（方法名 + 问题域）。
- `progress_note`：两三句面向用户的进度汇报（论文按什么结构写完了、核心结论怎么表述、可以去论文页查看编辑与导出），口语化；它会直接显示在任务页的执行过程里。
