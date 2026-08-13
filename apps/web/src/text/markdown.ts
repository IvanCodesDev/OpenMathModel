/**
 * 聊天气泡的 Markdown 渲染：转义优先的受限子集，模型输出按不可信内容处理。
 *
 * 管线：抽出代码块与公式做占位 → 全文 HTML 转义 → 行级块解析（标题/列表/
 * 引用/表格/分隔线/段落）→ 行内标记（粗斜体/行内代码/链接）→ 回填占位。
 * 原始文本从不直接进 innerHTML；链接只放行 http(s)，其余一律当纯文本。
 *
 * 公式输出为带 data-tex 的节点（行内加 data-tex-inline），由页面侧现有的
 * KaTeX 懒加载器排版；加载前节点内保留 LaTeX 源码作为可读回退，与方法库一致。
 * 本文件不做任何导入，方便按仓库惯例用 data URL 转译做单元测试。
 */

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#039;",
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => HTML_ESCAPES[character]);
}

function mathHtml(tex: string, display: boolean): string {
  const escaped = escapeHtml(tex.trim());
  return display
    ? `<div class="md-math-block" data-tex="${escaped}">$$${escaped}$$</div>`
    : `<span class="md-math" data-tex="${escaped}" data-tex-inline="true">$${escaped}$</span>`;
}

/** 占位符用私有区字符 \uE000 包裹序号：正常文本不会出现，也不被转义碰到。 */
const STASH_MARK = "\uE000";
const STASH_TOKEN = /\uE000(\d+)\uE000/g;

interface Stash {
  entries: string[];
}

function put(stash: Stash, html: string): string {
  stash.entries.push(html);
  return `${STASH_MARK}${stash.entries.length - 1}${STASH_MARK}`;
}

function restore(stash: Stash, text: string): string {
  return text.replace(STASH_TOKEN, (_match, index) => stash.entries[Number(index)] ?? "");
}

/** 行内标记；输入已经过 HTML 转义。 */
function renderInline(stash: Stash, text: string): string {
  let result = text;
  // 行内代码优先占位，避免其中的 * _ ~ 被后续规则改写
  result = result.replace(/`([^`\n]+)`/g, (_match, code: string) =>
    put(stash, `<code class="md-inline-code">${code}</code>`));
  // 链接只认显式 http(s)，javascript: 等一律保持纯文本
  result = result.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_match, label: string, href: string) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  result = result.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  result = result.replace(/(?<![*\w])\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
  result = result.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return result;
}

const TABLE_DIVIDER = /^\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/;

function tableCells(line: string): string[] {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(cell => cell.trim());
}

/** 把模型回复渲染成安全的 HTML 片段。 */
export function renderMarkdown(source: string): string {
  const stash: Stash = { entries: [] };
  let text = String(source ?? "").replace(/\r\n?/g, "\n");

  // 1. 代码块（未闭合的按到文末处理，流式渲染时代码块随增量增长）
  text = text.replace(/```([\w+#.-]*)[^\S\n]*\n?([\s\S]*?)(?:```|$)/g, (_match, lang: string, code: string) =>
    put(stash, `<pre class="md-code"><code${lang ? ` data-lang="${escapeHtml(lang)}"` : ""}>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`));

  // 2. 公式：块级（$$…$$、\[…\]）与行内（\(…\)、$…$）
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_match, tex: string) => put(stash, mathHtml(tex, true)));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_match, tex: string) => put(stash, mathHtml(tex, true)));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_match, tex: string) => put(stash, mathHtml(tex, false)));
  // 行内 $…$ 要求两侧紧贴内容，避免把「$5 和 $10」当成公式
  text = text.replace(/\$(?!\s)([^$\n]*?[^\s$])\$/g, (_match, tex: string) => put(stash, mathHtml(tex, false)));

  // 3. 剩余文本统一转义；此后所有替换只针对已转义文本
  text = escapeHtml(text);

  // 4. 行级块解析
  const lines = text.split("\n");
  const blocks: string[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(`<p>${paragraph.map(line => renderInline(stash, line)).join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map(item => `<li>${renderInline(stash, item)}</li>`).join("");
      blocks.push(list.ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`);
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      blocks.push(`<blockquote>${quote.map(line => renderInline(stash, line)).join("<br>")}</blockquote>`);
      quote = [];
    }
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();

    if (!trimmed) {
      flushAll();
      continue;
    }

    // 表格：表头行 + 分隔行
    if (trimmed.startsWith("|") && TABLE_DIVIDER.test(lines[index + 1]?.trim() ?? "")) {
      flushAll();
      const head = tableCells(trimmed).map(cell => `<th>${renderInline(stash, cell)}</th>`).join("");
      const rows: string[] = [];
      let cursor = index + 2;
      while (cursor < lines.length && lines[cursor].trim().startsWith("|")) {
        const cells = tableCells(lines[cursor].trim()).map(cell => `<td>${renderInline(stash, cell)}</td>`).join("");
        rows.push(`<tr>${cells}</tr>`);
        cursor += 1;
      }
      blocks.push(`<table class="md-table"><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
      index = cursor - 1;
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInline(stash, heading[2])}</h${level}>`);
      continue;
    }

    if (/^(?:-{3,}|\*{3,})$/.test(trimmed)) {
      flushAll();
      blocks.push("<hr>");
      continue;
    }

    // 引用：原文 "> " 转义后是 "&gt; "
    const quoted = /^&gt;\s?(.*)$/.exec(trimmed);
    if (quoted) {
      flushParagraph();
      flushList();
      quote.push(quoted[1]);
      continue;
    }

    const unordered = /^[-*]\s+(.*)$/.exec(trimmed);
    const ordered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (unordered || ordered) {
      flushParagraph();
      flushQuote();
      const isOrdered = Boolean(ordered);
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push((unordered ?? ordered)![1]);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(trimmed);
  }
  flushAll();

  return restore(stash, blocks.join(""));
}
