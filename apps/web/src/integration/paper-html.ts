/**
 * PaperDocument → 自足的单文件 HTML：「导出 HTML」直接下载它，「导出 PDF」把它交给服务端
 * 无头浏览器按 A4 打印（打印样式写在 @page / @media print 里，屏幕上打开是居中的纸面）。
 *
 * 自足 = 图片与 KaTeX 字体都以 data URL 内联：服务端打印时网络全部拦截，用户离线打开也不缺图。
 * 排版取赛事论文惯例：宋体 + Times New Roman 小四、1.5 倍左右行距、首行缩进 2 字符、两端对齐、
 * 黑体标题、三线表、图题在下表题在上；长拉丁串插零宽断点，避免两端对齐把整行字距拉开。
 * 两端对齐按字符分摊（text-justify: inter-character）：行尾放不下的是长数字这类断不开的串时，
 * 剩下的空白摊到整行每个字符之间，而不是堆在一行里仅有的几个空格、标点旁边。
 */

import {
  type Block,
  type Inline,
  type PaperDocument,
  allowInlineMathBreaks,
  insertSoftBreaks,
  stripControlChars,
  tableLayout,
} from "./paper-document";
import { dataUrl } from "./paper-export-images";

export interface HtmlOptions {
  /** KaTeX 排版：tex → HTML 片段（失败时调用方自行回退源码）。 */
  renderMath: (tex: string, display: boolean) => string;
  /** KaTeX 样式表全文（字体已内联为 data URL）；空串 = 不带（公式退化为浏览器默认字体）。 */
  katexCss: string;
}

function escapeHtml(text: string): string {
  return stripControlChars(text).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
}

/** 不能出现在行首 / 行尾的标点：与相邻的行内公式粘成一组，不许从中间断行。 */
const CLOSING_RUN = /^[，。、；：！？）」』”’】〕》〉…%,.;:!?)\]]+/;
const OPENING_RUN = /[（「『“‘【〔《〈([]+$/;

function textHtml(inline: Extract<Inline, { kind: "text" }>): string {
  const marks = inline.marks;
  let html = escapeHtml(insertSoftBreaks(inline.text));
  if (marks.code) html = `<code>${html}</code>`;
  if (marks.sup) html = `<sup>${html}</sup>`;
  if (marks.sub) html = `<sub>${html}</sub>`;
  if (marks.bold) html = `<strong>${html}</strong>`;
  if (marks.italic) html = `<em>${html}</em>`;
  if (marks.underline) html = `<u>${html}</u>`;
  if (marks.strike) html = `<s>${html}</s>`;
  if (marks.href) html = `<a href="${escapeHtml(marks.href)}">${html}</a>`;
  return html;
}

/**
 * 行内序列 → HTML。浏览器在行内公式（KaTeX 的行内块）边界上总允许断行、不看标点禁则，
 * 「(x_i,y_i)，其中」会把「，」甩到下一行行首；所以短公式连同紧贴的标点包进 .glue 整组不断行。
 * 断行判定看的是公式内部元素的 white-space，没法只禁「公式 | 标点」这一处：长公式不粘，
 * 保住它在逗号、运算符后断行的能力（否则整串挪到下一行，上一行又被拉开）。
 */
const GLUE_MAX_TEX = 24;
function inlineHtml(inlines: readonly Inline[], options: HtmlOptions): string {
  const items: Inline[] = [...inlines];
  let html = "";
  let lead = "";
  for (let index = 0; index < items.length; index += 1) {
    const inline = items[index];
    if (inline.kind === "break") {
      html += "<br>";
    } else if (inline.kind === "text") {
      let text = inline.text;
      const following = items[index + 1];
      const opening = following?.kind === "math" && following.tex.length <= GLUE_MAX_TEX ? OPENING_RUN.exec(text) : null;
      if (opening) {
        lead = textHtml({ ...inline, text: opening[0] });
        text = text.slice(0, opening.index);
      }
      if (text) html += textHtml({ ...inline, text });
    } else {
      const math = `<span class="math-inline">${options.renderMath(allowInlineMathBreaks(inline.tex), false)}</span>`;
      let trail = "";
      const next = items[index + 1];
      const closing = next?.kind === "text" && inline.tex.length <= GLUE_MAX_TEX ? CLOSING_RUN.exec(next.text) : null;
      if (next?.kind === "text" && closing) {
        trail = textHtml({ ...next, text: closing[0] });
        items[index + 1] = { ...next, text: next.text.slice(closing[0].length) };
      }
      html += lead || trail ? `<span class="glue">${lead}${math}${trail}</span>` : math;
      lead = "";
    }
  }
  return html;
}

function blockHtml(block: Block, doc: PaperDocument, options: HtmlOptions): string {
  switch (block.kind) {
    case "heading": {
      const tag = `h${block.level + 1}`;
      const role = block.role === "abstract" ? ' class="abstract-heading"' : "";
      return `<${tag}${role}>${inlineHtml(block.inlines, options)}</${tag}>`;
    }
    case "paragraph": {
      const classes = [block.role, block.continued ? "continue" : "", block.align ? `align-${block.align}` : ""].filter(Boolean);
      return `<p${classes.length ? ` class="${classes.join(" ")}"` : ""}>${inlineHtml(block.inlines, options)}</p>`;
    }
    case "math":
      return `<div class="math-block">${options.renderMath(block.tex, true)}</div>`;
    case "list": {
      const tag = block.ordered ? "ol" : "ul";
      return `<${tag}>${block.items.map(item => `<li>${inlineHtml(item, options)}</li>`).join("")}</${tag}>`;
    }
    case "quote":
      return `<blockquote>${inlineHtml(block.inlines, options)}</blockquote>`;
    case "code":
      return `<pre><code>${escapeHtml(block.text)}</code></pre>`;
    case "table": {
      const layout = tableLayout(block.rows);
      const row = (cells: typeof block.rows[number], tag: "th" | "td") => `<tr>${Array.from({ length: layout.columns }, (_, index) => {
        const wrap = tag === "td" && layout.wrap[index] ? ' class="wrap"' : "";
        return `<${tag}${wrap}>${cells[index] ? inlineHtml(cells[index].inlines, options) : ""}</${tag}>`;
      }).join("")}</tr>`;
      const head = block.rows.slice(0, layout.headCount).map(cells => row(cells, "th")).join("");
      const body = block.rows.slice(layout.headCount).map(cells => row(cells, "td")).join("");
      const caption = block.caption?.length ? `<caption>${inlineHtml(block.caption, options)}</caption>` : "";
      return `<table>${caption}${head ? `<thead>${head}</thead>` : ""}<tbody>${body}</tbody></table>`;
    }
    case "figure": {
      const image = block.image === null ? null : doc.images[block.image];
      const caption = block.caption.length ? `<figcaption>${inlineHtml(block.caption, options)}</figcaption>` : "";
      const picture = image
        ? `<img src="${dataUrl(image)}" alt="${escapeHtml(block.alt)}">`
        : `<div class="missing-figure">${escapeHtml(block.alt || "插图")}（图片未能随导出带走）</div>`;
      return `<figure>${picture}${caption}</figure>`;
    }
    case "rule":
      return "<hr>";
  }
}

/** 正文字体链：西文 Times New Roman，中文落到各平台的宋体；标题中文落到黑体。 */
const BODY_FONTS = '"Times New Roman", Times, "SimSun", "Songti SC", "Noto Serif CJK SC", "Source Han Serif SC", "Noto Serif SC", serif';
const HEADING_FONTS = '"Times New Roman", Times, "SimHei", "Heiti SC", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif';
const CODE_FONTS = 'Consolas, "Cascadia Mono", Menlo, "DejaVu Sans Mono", "SimSun", monospace';

export const PAPER_PRINT_CSS = `
@page { size: A4; margin: 25mm 25mm 24mm; @bottom-center { content: counter(page); font: 10pt ${BODY_FONTS}; color: #333; } }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; color: #000; font: 12pt/1.8 ${BODY_FONTS}; text-align: justify; text-justify: inter-character; text-autospace: normal; line-break: strict; overflow-wrap: break-word; orphans: 2; widows: 2; }
@media screen {
  html { background: #eceae6; }
  body { box-sizing: border-box; max-width: 210mm; margin: 24px auto; padding: 25mm; background: #fff; box-shadow: 0 2px 16px rgba(0, 0, 0, .12); }
}
h1 { margin: 0 0 14pt; font: bold 16pt/1.5 ${HEADING_FONTS}; text-align: center; }
h2, h3, h4, h5 { font-family: ${HEADING_FONTS}; font-weight: bold; text-align: left; break-after: avoid; page-break-after: avoid; }
h2 { margin: 16pt 0 6pt; font-size: 14pt; line-height: 1.5; }
h3 { margin: 12pt 0 4pt; font-size: 12pt; line-height: 1.5; }
h4, h5 { margin: 10pt 0 4pt; font-size: 12pt; line-height: 1.5; }
h2.abstract-heading { margin-top: 6pt; text-align: center; letter-spacing: .5em; text-indent: .5em; }
p { margin: 0 0 3pt; text-indent: 2em; }
p.continue, p.keywords, p.note, p.align-center, p.align-right { text-indent: 0; }
p.keywords { margin-top: 6pt; }
p.note { font-size: 10.5pt; color: #444; }
p.align-center { text-align: center; }
p.align-right { text-align: right; }
.math-inline .katex { font-size: 1.05em; }
.glue { white-space: nowrap; }
.math-block { margin: 6pt 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }
.math-block .katex-display { margin: 0; }
ul, ol { margin: 0 0 4pt; padding-left: 2.5em; }
li { margin: 0; }
blockquote { margin: 4pt 0 6pt 2em; padding-left: 10pt; border-left: 2pt solid #999; color: #333; }
pre { margin: 4pt 0 8pt; padding: 6pt 8pt; border: .5pt solid #bbb; background: #f6f6f6; font: 9pt/1.45 ${CODE_FONTS}; white-space: pre-wrap; word-break: break-all; text-align: left; }
code { font-family: ${CODE_FONTS}; font-size: .9em; }
pre code { font-size: inherit; }
table { width: 100%; margin: 4pt auto 10pt; border-collapse: collapse; border-top: 1.5pt solid #000; border-bottom: 1.5pt solid #000; font-size: 10.5pt; line-height: 1.5; text-align: center; break-inside: auto; }
caption { caption-side: top; padding-bottom: 4pt; font-size: 10.5pt; font-weight: bold; text-align: center; break-after: avoid; }
thead { display: table-header-group; }
thead tr:last-child th { border-bottom: .75pt solid #000; }
th, td { padding: 3pt 6pt; vertical-align: middle; text-indent: 0; }
td.wrap { text-align: left; }
th { font-weight: bold; }
tr { break-inside: avoid; page-break-inside: avoid; }
figure { margin: 8pt 0 10pt; text-align: center; text-indent: 0; break-inside: avoid; page-break-inside: avoid; }
figure img { display: block; max-width: 82%; max-height: 10cm; margin: 0 auto 4pt; }
figcaption { font-size: 10.5pt; line-height: 1.5; text-align: center; }
.missing-figure { padding: 18pt; border: .75pt dashed #999; color: #666; font-size: 10.5pt; }
hr { margin: 8pt 0; border: 0; border-top: .75pt solid #999; }
a { color: inherit; text-decoration: none; }
`;

/** 整篇 → 自足 HTML 文档。 */
export function paperDocumentToHtml(doc: PaperDocument, options: HtmlOptions): string {
  const body = doc.blocks.map(block => blockHtml(block, doc, options)).join("\n");
  return [
    "<!DOCTYPE html>",
    '<html lang="zh-CN">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${escapeHtml(doc.title)}</title>`,
    options.katexCss ? `<style>${options.katexCss}</style>` : "",
    `<style>${PAPER_PRINT_CSS}</style>`,
    "</head>",
    "<body>",
    `<h1>${escapeHtml(insertSoftBreaks(doc.title))}</h1>`,
    body,
    "</body>",
    "</html>",
    "",
  ].filter(line => line !== "").join("\n");
}
