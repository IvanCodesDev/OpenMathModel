/**
 * 论文导出的统一文档模型：编辑器正文 DOM → PaperDocument → Word / LaTeX / HTML（PDF）。
 *
 * 四种导出从同一份模型出发，正文结构（公式后的续段、表题、图题、列表、代码）各格式一致；
 * 序列化器（paper-docx / paper-latex / paper-html）只吃模型、不碰 DOM，node --test 可直接断言。
 * 本文件的 readPaperDocument 是唯一读 DOM 的地方，它同时兼容两种正文：
 * - renderMarkdown 渲染的终稿（p / h3 / md-math-block / md-table-wrap / md-code / md-figure …）；
 * - 用户在 contenteditable 里改过的草稿（div 换行、b / i / font、手工插入的 editor-formula /
 *   editor-table / source-chip，以及旧版渲染遗留的游离文本）。
 */

import type { EmbeddedImage } from "./paper-export-images";

export interface TextMarks {
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strike?: boolean;
  code?: boolean;
  sup?: boolean;
  sub?: boolean;
  href?: string;
}

export type Inline =
  | { kind: "text"; text: string; marks: TextMarks }
  | { kind: "math"; tex: string }
  | { kind: "break" };

export type Align = "left" | "center" | "right";

export interface TableCell {
  inlines: Inline[];
  header: boolean;
}

export type Block =
  | { kind: "heading"; level: 1 | 2 | 3 | 4; inlines: Inline[]; role?: "abstract" }
  | {
    kind: "paragraph";
    inlines: Inline[];
    /** abstract 摘要正文 / keywords 关键词行 / note 来源标注等不缩进的小字。 */
    role?: "abstract" | "keywords" | "note";
    /** 块级公式后的续段：不首行缩进（LaTeX 里紧接公式、不空行）。 */
    continued?: boolean;
    align?: Align;
  }
  | { kind: "math"; tex: string }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "quote"; inlines: Inline[] }
  | { kind: "code"; text: string; language?: string }
  | { kind: "table"; rows: TableCell[][]; caption?: Inline[] }
  | { kind: "figure"; image: number | null; caption: Inline[]; alt: string }
  | { kind: "rule" };

export interface PaperDocument {
  title: string;
  blocks: Block[];
  images: EmbeddedImage[];
}

// ── 纯函数工具（序列化器共用） ───────────────────────────────────────────────

/** 零宽空格：HTML / Word 里都是「可在此断行、不占宽度」的断点；LaTeX 序列化时换成 \allowbreak。 */
export const SOFT_BREAK = "\u200b";

/** 去掉 XML 1.0 与 TeX 都不收的字符：除 \t \n \r 外的控制字符、U+FFFE / U+FFFF、孤立代理项。 */
export function stripControlChars(text: string): string {
  let out = "";
  for (const char of String(text)) {
    const code = char.codePointAt(0) ?? 0;
    if (code < 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d) continue;
    if (code === 0xfffe || code === 0xffff || (code >= 0xd800 && code <= 0xdfff)) continue;
    out += char;
  }
  return out;
}

/** 16 个字符以上、中间没有空格的拉丁串（变量名、长数字、网址）：要补断点的对象。 */
export const LONG_TOKEN = /[A-Za-z0-9_.=:/\\,;+-]{16,}/g;

/**
 * 长拉丁串插断点：`metrics.test_acc_iris_mlp_adam=0.99166667` 这类没有空格的串，浏览器 / Word /
 * LaTeX 都只能整串挪到下一行，上一行被两端对齐硬拉开（字母与标点间距变大、行尾大片空白）。
 * 16 个字符以上的串在 `_ = / : , ; \` 之后、以及字母前的 `.` 之后允许断行；字母串内部也允许断
 * （两侧各至少留 2 个字母）——一行几乎全是长串时，只在分隔符后断，行尾仍放不下下一段，
 * 本行仅有的空格就会被拉开。切完仍有 16 个字符以上连续没有断点的片段（长小数、哈希）每 12 个
 * 字符再给一个断点，宁可在行尾断开也不把整行拉散。
 * `letters: false` 不在字母串内部断（LaTeX 估算表格列宽用）。LaTeX 正文不走这里：长串包进
 * `\ommid{…}`，由导言区的宏按同一套规则断（paper-latex.ts）。
 */
export function insertSoftBreaks(text: string, { letters = true }: { letters?: boolean } = {}): string {
  return text.replace(LONG_TOKEN, token => {
    const segments: string[] = [];
    let start = 0;
    for (let index = 0; index < token.length - 1; index += 1) {
      const char = token[index];
      if ("_=/:,;\\".includes(char) || (char === "." && /[A-Za-z]/.test(token[index + 1]))) {
        segments.push(token.slice(start, index + 1));
        start = index + 1;
      }
    }
    segments.push(token.slice(start));
    return segments
      .flatMap(segment => (letters ? segment.replace(/[A-Za-z]{4,}/g, splitLetters) : segment).split(SOFT_BREAK))
      .map(piece => (piece.length >= 16 ? piece.replace(/(.{12})(?=.)/g, `$1${SOFT_BREAK}`) : piece))
      .join(SOFT_BREAK);
  });
}

/** 字母串内部的断点，两侧各至少留 2 个字母：`circles` → `ci|r|c|l|es`。 */
function splitLetters(run: string): string {
  return run.slice(0, 2) + [...run.slice(2, -1)].map(char => SOFT_BREAK + char).join("") + run.slice(-1);
}

/**
 * 行内公式在顶层逗号后允许断行（KaTeX 与 LaTeX 都认 \allowbreak）：`(W^{(1)},b^{(1)},…)` 这种长串
 * 默认只能在关系符 / 运算符后断，整串挤到下一行，上一行被两端对齐拉开。花括号里的逗号
 * （上下标、\text{}）与 `\,` 这类命令不动。
 */
export function allowInlineMathBreaks(tex: string): string {
  let depth = 0;
  let out = "";
  for (let index = 0; index < tex.length; index += 1) {
    const char = tex[index];
    if (char === "\\") {
      out += char + (tex[index + 1] ?? "");
      index += 1;
      continue;
    }
    if (char === "{") depth += 1;
    else if (char === "}") depth = Math.max(0, depth - 1);
    out += char;
    if (char === "," && depth === 0 && index < tex.length - 1 && !tex.startsWith("\\allowbreak", index + 1)) out += "\\allowbreak ";
  }
  return out;
}

/** 行内序列的纯文本（表题识别、长度估算用）。 */
export function inlineText(inlines: readonly Inline[]): string {
  return inlines.map(inline => (inline.kind === "text" ? inline.text : inline.kind === "math" ? inline.tex : "\n")).join("");
}

/** 「表 1 …」「Table 2 …」：紧挨在表格前的这种段落是表题。 */
export const TABLE_CAPTION = /^\s*(?:续?表|Table)\s*[0-9０-９]+(?:[.\-－–][0-9]+)?(?:\s|[:：.、]|$)/;

/** 粗略版面宽度（中文字符计 1，拉丁计 0.55，公式按源码长度折算）：表格分列宽用。 */
export function displayWidth(inlines: readonly Inline[]): number {
  let width = 0;
  for (const inline of inlines) {
    if (inline.kind === "math") width += Math.min(inline.tex.replace(/\\[a-zA-Z]+|[{}^_\s]/g, "x").length * 0.6, 40);
    else if (inline.kind === "text") {
      for (const char of inline.text) width += /[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]/.test(char) ? 1 : char === SOFT_BREAK ? 0 : 0.55;
    }
  }
  return width;
}

export interface TableLayout {
  columns: number;
  /** 各列内容的粗略版面宽度（displayWidth 口径，单位≈一个汉字）。 */
  widths: number[];
  total: number;
  /** 需要折行的长文本列：左对齐、可换行；其余列居中、不折行。 */
  wrap: boolean[];
  /** 表头行数（开头连续的全 th 行）。 */
  headCount: number;
}

/** 表格列布局：总宽排得下（约 42 个汉字）就全部居中；排不下时长列（8 字以上）改为左对齐折行。 */
export function tableLayout(rows: readonly TableCell[][]): TableLayout {
  const columns = Math.max(1, ...rows.map(row => row.length));
  const widths = Array.from({ length: columns }, (_, index) =>
    Math.max(1, ...rows.map(row => (row[index] ? displayWidth(row[index].inlines) : 0))));
  const total = widths.reduce((sum, width) => sum + width, 0);
  const wide = total > 42;
  const wrap = widths.map(width => wide && width > 8);
  if (wide && !wrap.some(Boolean)) wrap[widths.indexOf(Math.max(...widths))] = true;
  const firstBody = rows.findIndex(row => !row.every(cell => cell.header));
  return { columns, widths, total, wrap, headCount: firstBody < 0 ? rows.length : firstBody };
}

/** 首尾空白与断行裁掉、相邻同格式文本合并：序列化器拿到的是干净的行内序列。 */
export function normalizeInlines(inlines: Inline[]): Inline[] {
  const merged: Inline[] = [];
  for (const inline of inlines) {
    const last = merged[merged.length - 1];
    if (inline.kind === "text" && last?.kind === "text" && sameMarks(last.marks, inline.marks)) {
      merged[merged.length - 1] = { ...last, text: last.text + inline.text };
    } else if (inline.kind !== "text" || inline.text) {
      merged.push(inline);
    }
  }
  while (merged.length && isBlank(merged[0])) merged.shift();
  while (merged.length && isBlank(merged[merged.length - 1])) merged.pop();
  const first = merged[0];
  if (first?.kind === "text") merged[0] = { ...first, text: first.text.replace(/^\s+/, "") };
  const last = merged[merged.length - 1];
  if (last?.kind === "text") merged[merged.length - 1] = { ...last, text: last.text.replace(/\s+$/, "") };
  return merged.filter(inline => inline.kind !== "text" || inline.text);
}

function isBlank(inline: Inline): boolean {
  return inline.kind === "break" || (inline.kind === "text" && !inline.text.trim());
}

function sameMarks(a: TextMarks, b: TextMarks): boolean {
  const keys: (keyof TextMarks)[] = ["bold", "italic", "underline", "strike", "code", "sup", "sub", "href"];
  return keys.every(key => (a[key] ?? false) === (b[key] ?? false));
}

// ── DOM → 模型（只在浏览器里跑） ─────────────────────────────────────────────

const INLINE_TAGS = new Set([
  "A", "ABBR", "B", "BDI", "BDO", "BR", "CITE", "CODE", "DATA", "DEL", "DFN", "EM", "FONT", "I", "INS", "KBD",
  "LABEL", "MARK", "Q", "S", "SAMP", "SMALL", "SPAN", "STRIKE", "STRONG", "SUB", "SUP", "TIME", "U", "VAR", "WBR",
]);
const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "svg", "SVG", "IFRAME", "OBJECT", "EMBED"]);

function isElement(node: Node): node is HTMLElement {
  return node.nodeType === 1;
}

function isSkipped(element: HTMLElement): boolean {
  return SKIP_TAGS.has(element.tagName)
    || element.classList.contains("editor-stream-caret")
    || element.classList.contains("md-code-head")
    || element.getAttribute("aria-hidden") === "true" && !element.dataset.tex;
}

/** 行内公式节点：带 data-tex（排版后里面是 KaTeX 标记，必须取原始 LaTeX），或粘贴进来的裸 KaTeX。 */
function mathTex(element: HTMLElement): string | null {
  if (element.dataset.tex !== undefined) return element.dataset.tex.trim();
  if (element.classList.contains("katex")) {
    const annotation = element.querySelector('annotation[encoding="application/x-tex"]');
    if (annotation?.textContent) return annotation.textContent.trim();
  }
  return null;
}

function isBlockMath(element: HTMLElement): boolean {
  if (element.classList.contains("md-math-block") || element.classList.contains("editor-formula")) return true;
  if (element.classList.contains("katex-display")) return true;
  return element.dataset.tex !== undefined && element.dataset.texInline !== "true" && !INLINE_TAGS.has(element.tagName);
}

function styleMarks(element: HTMLElement, marks: TextMarks): TextMarks {
  const style = element.style;
  if (!style) return marks;
  const next = { ...marks };
  const weight = style.fontWeight;
  if (weight === "bold" || weight === "bolder" || Number(weight) >= 600) next.bold = true;
  if (style.fontStyle === "italic" || style.fontStyle === "oblique") next.italic = true;
  const decoration = `${style.textDecoration} ${style.textDecorationLine}`;
  if (decoration.includes("underline")) next.underline = true;
  if (decoration.includes("line-through")) next.strike = true;
  if (style.verticalAlign === "super") next.sup = true;
  if (style.verticalAlign === "sub") next.sub = true;
  return next;
}

function readInlines(node: Node, marks: TextMarks, out: Inline[]): void {
  if (node.nodeType === 3) {
    const text = (node.textContent ?? "").replace(/[\t\n\r ]+/g, " ");
    if (text) out.push({ kind: "text", text, marks });
    return;
  }
  if (!isElement(node) || isSkipped(node)) return;
  const tex = mathTex(node);
  if (tex !== null) {
    if (tex) out.push({ kind: "math", tex });
    return;
  }
  const tag = node.tagName;
  if (tag === "BR") { out.push({ kind: "break" }); return; }
  if (tag === "IMG" || tag === "UL" || tag === "OL" || tag === "TABLE" || tag === "FIGURE") return;
  if (tag === "BUTTON") {
    // 来源标注 chip：只留文字（图标是 <i class="ph …"> 空元素），按斜体小注处理
    const label = (node.textContent ?? "").replace(/\s+/g, " ").trim();
    if (label) out.push({ kind: "text", text: label, marks: { ...marks, italic: true } });
    return;
  }
  let next = styleMarks(node, marks);
  if (tag === "B" || tag === "STRONG") next = { ...next, bold: true };
  else if (tag === "I" || tag === "EM" || tag === "CITE" || tag === "VAR" || tag === "DFN") next = { ...next, italic: true };
  else if (tag === "U" || tag === "INS") next = { ...next, underline: true };
  else if (tag === "S" || tag === "DEL" || tag === "STRIKE") next = { ...next, strike: true };
  else if (tag === "CODE" || tag === "KBD" || tag === "SAMP") next = { ...next, code: true };
  else if (tag === "SUP") next = { ...next, sup: true };
  else if (tag === "SUB") next = { ...next, sub: true };
  else if (tag === "A") {
    const href = node.getAttribute("href") ?? "";
    if (/^https?:\/\//i.test(href)) next = { ...next, href };
  }
  node.childNodes.forEach(child => readInlines(child, next, out));
}

function inlinesOf(element: Node): Inline[] {
  const out: Inline[] = [];
  element.childNodes.forEach(child => readInlines(child, {}, out));
  return normalizeInlines(out);
}

function alignOf(element: HTMLElement): Align | undefined {
  const value = (element.style?.textAlign || element.getAttribute("align") || "").toLowerCase();
  return value === "center" || value === "right" ? value : undefined;
}

interface ReadState {
  blocks: Block[];
  images: EmbeddedImage[];
  title: string | null;
}

function figureFromImage(img: HTMLImageElement, caption: Inline[], state: ReadState): Block {
  const index = img.dataset.exportIndex === undefined ? NaN : Number(img.dataset.exportIndex);
  return {
    kind: "figure",
    image: Number.isInteger(index) && state.images[index] ? index : null,
    caption,
    alt: img.getAttribute("alt") ?? "",
  };
}

function readTable(table: HTMLTableElement, state: ReadState): void {
  const rows: TableCell[][] = [];
  table.querySelectorAll("tr").forEach(row => {
    if (row.closest("table") !== table) return;
    const cells: TableCell[] = [];
    row.querySelectorAll(":scope > th, :scope > td").forEach(cell => {
      cells.push({ inlines: inlinesOf(cell), header: cell.tagName === "TH" || cell.closest("thead") !== null });
    });
    if (cells.length) rows.push(cells);
  });
  if (!rows.length) return;
  // 紧挨在表前的「表 N …」段落收作表题
  const previous = state.blocks[state.blocks.length - 1];
  let caption: Inline[] | undefined;
  if (previous?.kind === "paragraph" && !previous.role && TABLE_CAPTION.test(inlineText(previous.inlines))) {
    caption = previous.inlines;
    state.blocks.pop();
  }
  state.blocks.push({ kind: "table", rows, caption });
}

function readList(list: HTMLElement, state: ReadState): void {
  const items: Inline[][] = [];
  const nested: HTMLElement[] = [];
  list.querySelectorAll(":scope > li").forEach(item => {
    items.push(inlinesOf(item));
    item.querySelectorAll(":scope > ul, :scope > ol").forEach(child => nested.push(child as HTMLElement));
  });
  if (items.some(item => item.length)) state.blocks.push({ kind: "list", ordered: list.tagName === "OL", items: items.filter(item => item.length) });
  nested.forEach(child => readList(child, state));
}

function pushParagraph(inlines: Inline[], state: ReadState, extra: Partial<Extract<Block, { kind: "paragraph" }>> = {}): void {
  const clean = normalizeInlines(inlines);
  if (!clean.length) return;
  state.blocks.push({ kind: "paragraph", inlines: clean, ...extra });
}

/** 容器的子节点：块级元素各自成块，夹在块之间的文字与行内元素收成段落（旧渲染遗留的游离文本也在这里补回）。 */
function readContainer(container: HTMLElement, state: ReadState, paragraphExtra: Partial<Extract<Block, { kind: "paragraph" }>> = {}): void {
  let pending: Inline[] = [];
  let pendingContinues = false;
  const flush = () => {
    pushParagraph(pending, state, pendingContinues ? { ...paragraphExtra, continued: true } : paragraphExtra);
    pending = [];
    pendingContinues = false;
  };
  container.childNodes.forEach(node => {
    if (isElement(node) && !isSkipped(node) && !isInlineContent(node)) {
      flush();
      readBlock(node, state);
      const last = state.blocks[state.blocks.length - 1];
      pendingContinues = last?.kind === "math";
      return;
    }
    readInlines(node, {}, pending);
  });
  flush();
}

function isInlineContent(element: HTMLElement): boolean {
  if (mathTex(element) !== null) return !isBlockMath(element);
  if (element.tagName === "BUTTON") return !element.classList.contains("source-chip") || element.parentElement?.tagName === "P";
  return INLINE_TAGS.has(element.tagName);
}

function readBlock(element: HTMLElement, state: ReadState): void {
  const tag = element.tagName;
  if (isBlockMath(element)) {
    const tex = mathTex(element) ?? (element.textContent ?? "").trim();
    if (tex) state.blocks.push({ kind: "math", tex });
    return;
  }
  if (tag === "H1" && state.title === null) {
    state.title = (element.textContent ?? "").replace(/\s+/g, " ").trim();
    return;
  }
  if (/^H[1-6]$/.test(tag)) {
    const inlines = inlinesOf(element);
    if (!inlines.length) return;
    const level = Math.min(4, Math.max(1, Number(tag[1]) - 1)) as 1 | 2 | 3 | 4;
    const abstract = element.classList.contains("paper-abstract-heading");
    state.blocks.push({ kind: "heading", level: tag === "H1" ? 1 : level, inlines, ...(abstract ? { role: "abstract" as const } : {}) });
    return;
  }
  if (tag === "P" || tag === "DIV" && !element.querySelector(":scope > :is(p, div, h1, h2, h3, h4, h5, h6, ul, ol, table, figure, pre, blockquote, hr, section)")) {
    if (element.classList.contains("md-code")) { readCode(element, state); return; }
    const role = element.classList.contains("paper-abstract") ? "abstract"
      : element.classList.contains("paper-keywords") || element.classList.contains("keywords") ? "keywords" : undefined;
    const extra: Partial<Extract<Block, { kind: "paragraph" }>> = {};
    if (role) extra.role = role;
    if (element.classList.contains("md-continue")) extra.continued = true;
    const align = alignOf(element);
    if (align) extra.align = align;
    // 段落里夹着图片 / 块级公式（用户插入或旧草稿）：拆成段落 + 块
    if (element.querySelector("img, .md-math-block, .editor-formula, .katex-display")) {
      readMixedParagraph(element, state, extra);
      return;
    }
    pushParagraph(inlinesOf(element), state, extra);
    return;
  }
  switch (tag) {
    case "FIGURE": {
      const img = element.querySelector("img");
      const caption = element.querySelector("figcaption");
      if (img) state.blocks.push(figureFromImage(img, caption ? inlinesOf(caption) : [], state));
      return;
    }
    case "IMG":
      state.blocks.push(figureFromImage(element as HTMLImageElement, [], state));
      return;
    case "TABLE":
      readTable(element as HTMLTableElement, state);
      return;
    case "UL":
    case "OL":
      readList(element, state);
      return;
    case "BLOCKQUOTE": {
      const inlines = inlinesOf(element);
      if (inlines.length) state.blocks.push({ kind: "quote", inlines });
      return;
    }
    case "PRE":
      readCode(element, state);
      return;
    case "HR":
      state.blocks.push({ kind: "rule" });
      return;
    case "BUTTON":
      pushParagraph(inlinesOf(element).map(inline => (inline.kind === "text" ? { ...inline, marks: { ...inline.marks, italic: true } } : inline)), state, { role: "note" });
      return;
    default:
      if (element.classList.contains("md-code")) { readCode(element, state); return; }
      readContainer(element, state);
  }
}

function readCode(element: HTMLElement, state: ReadState): void {
  const pre = element.tagName === "PRE" ? element : element.querySelector("pre");
  const text = (pre ?? element).textContent ?? "";
  if (!text.trim()) return;
  const language = element.dataset.label || element.closest<HTMLElement>(".md-code")?.dataset.label;
  state.blocks.push({ kind: "code", text: text.replace(/\n+$/, ""), ...(language ? { language } : {}) });
}

function readMixedParagraph(element: HTMLElement, state: ReadState, extra: Partial<Extract<Block, { kind: "paragraph" }>>): void {
  let pending: Inline[] = [];
  let continues = extra.continued ?? false;
  const flush = () => {
    pushParagraph(pending, state, { ...extra, continued: continues });
    pending = [];
  };
  element.childNodes.forEach(node => {
    if (isElement(node) && (node.tagName === "IMG" || isBlockMath(node))) {
      flush();
      if (node.tagName === "IMG") {
        state.blocks.push(figureFromImage(node as HTMLImageElement, [], state));
        continues = false;
      } else {
        readBlock(node, state);
        continues = true;
      }
      return;
    }
    if (isElement(node) && node.querySelector("img")) {
      flush();
      node.querySelectorAll("img").forEach(img => state.blocks.push(figureFromImage(img, [], state)));
      continues = false;
      return;
    }
    readInlines(node, {}, pending);
  });
  flush();
}

/**
 * 编辑器正文（collectPaperImages 返回的副本，<img> 已带 data-export-index）→ 文档模型。
 * 标题取第一个 h1，缺省用 fallbackTitle。
 */
export function readPaperDocument(root: HTMLElement, images: EmbeddedImage[], fallbackTitle: string): PaperDocument {
  const state: ReadState = { blocks: [], images, title: null };
  readContainer(root, state);
  return { title: state.title || fallbackTitle, blocks: state.blocks, images };
}
