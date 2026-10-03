import assert from "node:assert/strict";
import test from "node:test";
import { documentUrl, imagesUrl, math, sampleDocument, text, transpiled } from "./paper-export-fixture.mjs";

const { PAPER_PRINT_CSS, paperDocumentToHtml } = await import(
  await transpiled("./paper-html.ts", { "./paper-document": documentUrl, "./paper-export-images": imagesUrl })
);

const options = {
  katexCss: ".katex{font:normal 1.21em KaTeX_Main}",
  renderMath: (tex, display) => `<span class="${display ? "D" : "I"}">${tex}</span>`,
};

test("standalone document: title, KaTeX css, print css and every block in order", () => {
  const html = paperDocumentToHtml(sampleDocument(), options);
  assert.ok(html.startsWith('<!DOCTYPE html>\n<html lang="zh-CN">'));
  assert.ok(html.includes("<title>从零实现反向传播：手写 MLP 的推导与检验</title>"));
  assert.ok(html.indexOf("<style>.katex{") < html.indexOf("<style>\n@page"), "KaTeX 样式在前，论文样式可覆盖");
  assert.ok(html.includes("<h1>从零实现反向传播：手写 MLP 的推导与检验</h1>"));
  assert.ok(html.includes('<h2 class="abstract-heading">摘要</h2>'));
  assert.ok(html.includes('<p class="keywords"><strong>关键词：</strong>反向传播；梯度检验</p>'));
  assert.ok(html.includes("<p>学习目标为</p>\n<div class=\"math-block\"><span class=\"D\">\\min_{\\theta} J(\\theta)</span></div>\n<p class=\"continue\">其中 <span class=\"math-inline\"><span class=\"I\">J_i</span></span> 为交叉熵，100% 的 R&amp;D_x 成本 #1。</p>"));
  assert.ok(html.includes("<h3>1.1 子问题</h3>"));
  assert.ok(html.includes("<ol><li>前向传播</li><li>反向传播 <strong>加粗</strong></li></ol>"));
  assert.ok(html.includes("<pre><code>def f(x):\n\treturn x  # 注释</code></pre>"));
  assert.ok(html.includes("<blockquote>引用一句</blockquote>") && html.includes("<hr>"));
  assert.ok(html.includes('见 <a href="https://example.org/a?b=1#c">文档</a>。</p>'), "链接保留、控制字符去掉");
});

test("long tokens carry zero-width break points so justified lines are not stretched", () => {
  const html = paperDocumentToHtml(sampleDocument(), options);
  assert.ok(html.includes("me|t|r|i|cs.|gr|ad_|ch|e|ck_|max_|abs_|err=|5.9e-11".replaceAll("|", "\u200b")));
});

test("punctuation sticks to the formula next to it; long inline formulas may break after top-level commas", () => {
  const doc = {
    title: "t",
    images: [],
    blocks: [{ kind: "paragraph", inlines: [text("样本（"), math("x_i,y_i"), text("），其中"), math("\\epsilon"), text("。完")] }],
  };
  const html = paperDocumentToHtml(doc, options);
  assert.ok(html.includes(
    '<p>样本<span class="glue">（<span class="math-inline"><span class="I">x_i,\\allowbreak y_i</span></span>），</span>其中'
    + '<span class="glue"><span class="math-inline"><span class="I">\\epsilon</span></span>。</span>完</p>',
  ));
  assert.ok(PAPER_PRINT_CSS.includes(".glue { white-space: nowrap; }"));
  const long = paperDocumentToHtml({ title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [math("\\theta=(W^{(1)},b^{(1)},\\ldots,W^{(L)})"), text("。")] }] }, options);
  assert.ok(!long.includes('class="glue"') && long.includes("</span></span>。</p>"), "长公式不粘标点，保住公式内部的断行");
});

test("tables: caption on top, header row in thead, long text columns marked for left alignment", () => {
  const html = paperDocumentToHtml(sampleDocument(), options);
  assert.match(html, /<table><caption>表 1 全文符号说明<\/caption><thead><tr><th>符号<\/th><th>含义<\/th><th>单位<\/th><\/tr><\/thead><tbody><tr><td><span class="math-inline"><span class="I">\\theta<\/span><\/span><\/td><td class="wrap">网络待估参数集合/);
});

test("figures inline their bytes as data URLs; missing figures say so instead of leaving a hole", () => {
  const html = paperDocumentToHtml(sampleDocument(), options);
  assert.match(html, /<figure><img src="data:image\/png;base64,iVBORw0KGgo[^"]+" alt="图 1 训练收敛曲线"><figcaption>图 1 训练收敛曲线<\/figcaption><\/figure>/);
  assert.ok(html.includes('<figure><div class="missing-figure">图 2（图片未能随导出带走）</div><figcaption>图 2 丢失的图</figcaption></figure>'));
});

test("print css: A4 pages with page numbers, nothing clipped, figures / formulas / headings kept whole", () => {
  for (const rule of [
    "@page { size: A4;",
    "@bottom-center { content: counter(page);",
    "text-indent: 2em;",
    "p.continue, p.keywords",
    "white-space: pre-wrap;",
    "thead { display: table-header-group; }",
    "figure { margin: 8pt 0 10pt; text-align: center; text-indent: 0; break-inside: avoid;",
    ".math-block { margin: 6pt 0; text-align: center; break-inside: avoid;",
    "break-after: avoid;",
  ]) assert.ok(PAPER_PRINT_CSS.includes(rule), rule);
  assert.ok(!PAPER_PRINT_CSS.includes("overflow-x"), "打印时不能有横向滚动裁切");
});
