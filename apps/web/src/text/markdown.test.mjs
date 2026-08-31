/**
 * 聊天 Markdown 渲染的安全与正确性门禁：模型输出是不可信内容，
 * 转义遗漏会直接变成 XSS，这里用断言把常见注入路径挡住。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./markdown.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { renderMarkdown } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

test("escapes html and never passes tags through", () => {
  const html = renderMarkdown('<script>alert(1)</script> 与 <img src=x onerror=alert(1)>');
  assert.ok(!html.includes("<script"), "script 标签必须被转义");
  assert.ok(!html.includes("<img"), "img 标签必须被转义");
  assert.ok(html.includes("&lt;script&gt;"));
});

test("renders basic inline marks", () => {
  const html = renderMarkdown("**粗体** 与 *斜体* 与 ~~删除~~ 与 `代码<b>`");
  assert.ok(html.includes("<strong>粗体</strong>"));
  assert.ok(html.includes("<em>斜体</em>"));
  assert.ok(html.includes("<del>删除</del>"));
  assert.ok(html.includes('<code class="md-inline-code">代码&lt;b&gt;</code>'), "行内代码保持转义");
});

test("renders headings, lists, quote and hr", () => {
  const html = renderMarkdown("## 模型假设\n- 假设一\n- 假设二\n1. 第一步\n2. 第二步\n> 注意事项\n---");
  assert.ok(html.includes("<h2>模型假设</h2>"));
  assert.ok(html.includes("<ul><li>假设一</li><li>假设二</li></ul>"));
  assert.ok(html.includes("<ol><li>第一步</li><li>第二步</li></ol>"));
  assert.ok(html.includes("<blockquote>注意事项</blockquote>"));
  assert.ok(html.includes("<hr>"));
});

test("keeps fenced code verbatim and escaped", () => {
  const html = renderMarkdown("```python\nprint('<hi> & **bold**')\n```");
  assert.ok(html.includes('<pre class="md-code"><code data-lang="python">'));
  assert.ok(html.includes("print(&#039;&lt;hi&gt; &amp; **bold**&#039;)"), "代码块内不做行内标记");
});

test("unclosed fence during streaming still renders as code", () => {
  const html = renderMarkdown("```python\nx = 1\n");
  assert.ok(html.includes('data-lang="python"'));
  assert.ok(html.includes("x = 1"));
});

test("math becomes data-tex nodes with source fallback", () => {
  const html = renderMarkdown("目标函数 $\\min \\sum_i c_i x_i$：\n\n$$x_{i} \\ge 0$$");
  assert.ok(html.includes('data-tex-inline="true"'));
  assert.ok(html.includes('class="md-math-block"'));
  assert.ok(html.includes("$\\min \\sum_i c_i x_i$"), "KaTeX 加载前保留源码回退");
  assert.ok(!html.includes("<span class=\"md-math\" data-tex=\"\""), "tex 不应为空");
});

test("currency amounts are not mistaken for math", () => {
  const html = renderMarkdown("预算 $5 和 $10 都可以");
  assert.ok(!html.includes("md-math"), "$5 和 $10 不是公式");
});

test("links only allow http(s)", () => {
  const good = renderMarkdown("[文档](https://example.org/a?b=1)");
  assert.ok(good.includes('<a href="https://example.org/a?b=1" target="_blank" rel="noopener noreferrer">文档</a>'));
  const bad = renderMarkdown("[点我](javascript:alert(1))");
  assert.ok(!bad.includes("<a "), "javascript: 链接必须保持纯文本");
});

test("renders github-style tables", () => {
  const html = renderMarkdown("| 方案 | 成本 |\n| --- | --- |\n| A | 低 |\n| B | 高 |");
  assert.ok(html.includes('<table class="md-table">'));
  assert.ok(html.includes("<th>方案</th><th>成本</th>"));
  assert.ok(html.includes("<td>A</td><td>低</td>"));
});

test("accepts single-dash divider rows (GFM allows | - | - |)", () => {
  const html = renderMarkdown("| 方案 | 成本 |\n| - | - |\n| A | 低 |");
  assert.ok(html.includes('<table class="md-table">'), "单横线分隔行也应识别为表格");
  assert.ok(html.includes("<th>方案</th><th>成本</th>"));
  assert.ok(html.includes("<td>A</td><td>低</td>"));
  const aligned = renderMarkdown("| 方案 | 成本 |\n|:-|-:|\n| A | 低 |");
  assert.ok(aligned.includes('<table class="md-table">'), "带对齐冒号的单横线分隔行同样识别");
});

test("pipe-separated prose without a divider row stays plain text", () => {
  const html = renderMarkdown("Summary 1 页 | TOC 1 | 正文 ~20 | Memo 1 | 参考 1。");
  assert.ok(!html.includes("<table"), "没有分隔行的竖线句子不是表格");
  assert.ok(html.includes("Summary 1 页 | TOC 1"));
});

test("single newlines become line breaks inside a paragraph", () => {
  const html = renderMarkdown("第一行\n第二行\n\n新段落");
  assert.ok(html.includes("<p>第一行<br>第二行</p>"));
  assert.ok(html.includes("<p>新段落</p>"));
});
