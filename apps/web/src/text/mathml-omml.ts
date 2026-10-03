/**
 * KaTeX 输出的 MathML → Word 原生公式（OMML），供论文导出 .docx 使用。
 *
 * 公式进 Word 要能继续编辑，只能是 OMML。Word 自己粘贴 MathML 靠随 Office 分发的
 * MML2OMML.XSL，浏览器里拿不到；现成的 JS 转换库是 LGPL。KaTeX 的 MathML 只用到一个
 * 小子集（mrow / mi / mn / mo / mtext / mspace / msub / msup / msubsup / munder / mover /
 * munderover / mfrac / msqrt / mroot / mtable / mstyle / mpadded / mphantom / menclose /
 * semantics），这里逐个映射：求和、积分等大型运算符转 m:nary 并把紧随其后的一项收作被积式，
 * \left…\right 转 m:d，矩阵与 aligned / cases 转 m:m（按 columnalign 设列对齐，与 pandoc
 * 一致——不用 eqArr 的 & 对齐标记，WPS / LibreOffice 有的会把 & 原样显示）。
 *
 * 不碰 DOM：自带一个只为 KaTeX 输出准备的极简 XML 解析器，node --test 可直接断言。
 */

export interface MathNode {
  /** 标签名；文本节点为 "#text"。 */
  name: string;
  attrs: Record<string, string>;
  children: MathNode[];
  /** 仅文本节点有值。 */
  text: string;
}

const NAMED_ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0" };

function decodeEntities(text: string): string {
  return text.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (match, body: string) => {
    if (body.startsWith("#")) {
      const code = body[1] === "x" || body[1] === "X" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : match;
    }
    return NAMED_ENTITIES[body] ?? match;
  });
}

function parseAttributes(raw: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  for (const match of raw.matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
    attrs[match[1]] = decodeEntities(match[2] ?? match[3] ?? "");
  }
  return attrs;
}

/** 取出第一段 <math>…</math> 并解析成树；KaTeX 的整段 HTML（含 span 外壳）可直接传入。 */
export function parseMathml(source: string): MathNode | null {
  const start = source.indexOf("<math");
  if (start < 0) return null;
  const root: MathNode = { name: "#root", attrs: {}, children: [], text: "" };
  const stack: MathNode[] = [root];
  const tag = /<!--[\s\S]*?-->|<(\/?)([A-Za-z][\w:.-]*)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>/g;
  tag.lastIndex = start;
  let cursor = start;
  for (let match = tag.exec(source); match; match = tag.exec(source)) {
    if (match.index > cursor) {
      stack[stack.length - 1].children.push({
        name: "#text", attrs: {}, children: [], text: decodeEntities(source.slice(cursor, match.index)),
      });
    }
    cursor = tag.lastIndex;
    if (match[0].startsWith("<!--")) continue;
    const [, closing, name, rawAttrs, selfClosing] = match;
    if (closing) {
      const open = stack.map(node => node.name).lastIndexOf(name);
      if (open > 0) stack.length = open;
      if (name === "math" && stack.length === 1) break;
      continue;
    }
    const node: MathNode = { name, attrs: parseAttributes(rawAttrs), children: [], text: "" };
    stack[stack.length - 1].children.push(node);
    if (!selfClosing) stack.push(node);
  }
  return root.children.find(child => child.name === "math") ?? null;
}

// ── OMML 片段 ─────────────────────────────────────────────────────────────────

interface RunStyle {
  /** m:sty：p 正体 / b 粗体 / i 斜体 / bi 粗斜体；缺省 = 数学斜体（单字母变量）。 */
  sty?: "p" | "b" | "i" | "bi";
  /** m:scr：double-struck / script / fraktur / sans-serif / monospace。 */
  scr?: string;
  /** m:nor：普通文本（\text{} 里的中文、单词），用正文字体排。 */
  nor?: boolean;
}

interface Context {
  /** mstyle 上继承下来的 mathvariant。 */
  variant?: string;
}

/** 大型运算符：转 m:nary。积分类默认上下标在右侧，其余默认在正上下方。 */
const NARY = new Set([..."∑∏∐∫∬∭∮∯∰∱∲∳⋃⋂⋁⋀⨀⨁⨂⨄⨆"]);
const INTEGRALS = new Set([..."∫∬∭∮∯∰∱∲∳"]);
/** 这些运算符后面不是被求和的项（\sum = …、\int + … 之类），不收进 m:e。 */
const NOT_OPERAND = new Set([..."=<>≤≥≠≈≡∼+−-±∓,;:|)]}"]);
/** 函数应用、不可见乘号 / 分隔符 / 加号：Word 里不需要，留着会出方框。 */
const INVISIBLE = /[\u2061-\u2064]/g;

/** mover 的重音字符 → m:acc 用的组合字符。 */
const ACCENTS: Record<string, string> = {
  "^": "\u0302", "\u02c6": "\u0302", "\u0302": "\u0302",
  "\u02c7": "\u030c", "\u030c": "\u030c",
  "~": "\u0303", "\u02dc": "\u0303", "\u0303": "\u0303",
  "\u02c9": "\u0305", "\u00af": "\u0305", "\u0304": "\u0305", "\u0305": "\u0305",
  "\u02d9": "\u0307", "\u0307": "\u0307",
  "\u00a8": "\u0308", "\u0308": "\u0308",
  "\u00b4": "\u0301", "\u02ca": "\u0301", "\u0301": "\u0301",
  "`": "\u0300", "\u02cb": "\u0300", "\u0300": "\u0300",
  "\u02d8": "\u0306", "\u0306": "\u0306",
  "\u02da": "\u030a", "\u030a": "\u030a",
  "\u20d7": "\u20d7", "\u2192": "\u20d7",
};
const OVER_BARS = new Set(["\u203e", "\u00af", "\u02c9", "_", "\u0305"]);
const BRACES_TOP = new Set(["\u23de", "\ufe37"]);
const BRACES_BOTTOM = new Set(["\u23df", "\ufe38"]);

function xmlEscape(text: string): string {
  return text.replace(/[&<>"]/g, char => (char === "&" ? "&amp;" : char === "<" ? "&lt;" : char === ">" ? "&gt;" : "&quot;"));
}

const MATH_RUN_FONT = '<w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/></w:rPr>';
const TEXT_RUN_FONT = '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="SimSun"/></w:rPr>';

/**
 * KaTeX 用 U+2225（平行）写范数 ‖x‖、用 U+2223（整除）写 |x|，Cambria Math 里这两个字形是斜的，
 * Word 里会显示成「//x//」「/x/」；换成 Word 公式编辑器自己用的 U+2016 与 ASCII 竖线。
 */
const WORD_GLYPHS: Record<string, string> = { "\u2225": "\u2016", "\u2223": "|" };

function run(text: string, style: RunStyle = {}): string {
  const clean = text.replace(INVISIBLE, "").replace(/[\u2223\u2225]/g, char => WORD_GLYPHS[char]);
  if (!clean) return "";
  const props: string[] = [];
  if (style.nor) {
    props.push("<m:nor/>");
  } else {
    if (style.scr) props.push(`<m:scr m:val="${style.scr}"/>`);
    if (style.sty) props.push(`<m:sty m:val="${style.sty}"/>`);
  }
  const mathProps = props.length ? `<m:rPr>${props.join("")}</m:rPr>` : "";
  return `<m:r>${mathProps}${style.nor ? TEXT_RUN_FONT : MATH_RUN_FONT}<m:t xml:space="preserve">${xmlEscape(clean)}</m:t></m:r>`;
}

function variantStyle(variant: string | undefined, fallback: RunStyle): RunStyle {
  switch (variant) {
    case "normal": return { sty: "p" };
    case "bold": return { sty: "b" };
    case "italic": return { sty: "i" };
    case "bold-italic": return { sty: "bi" };
    case "double-struck": return { scr: "double-struck", sty: "p" };
    case "script": return { scr: "script", sty: "p" };
    case "bold-script": return { scr: "script", sty: "b" };
    case "fraktur": return { scr: "fraktur", sty: "p" };
    case "bold-fraktur": return { scr: "fraktur", sty: "b" };
    case "sans-serif": return { scr: "sans-serif", sty: "p" };
    case "bold-sans-serif": return { scr: "sans-serif", sty: "b" };
    case "sans-serif-italic": return { scr: "sans-serif", sty: "i" };
    case "sans-serif-bold-italic": return { scr: "sans-serif", sty: "bi" };
    case "monospace": return { scr: "monospace", sty: "p" };
    default: return fallback;
  }
}

function elements(node: MathNode): MathNode[] {
  return node.children.filter(child => child.name !== "#text");
}

function textOf(node: MathNode | undefined): string {
  if (!node) return "";
  if (node.name === "#text") return node.text;
  return node.children.map(textOf).join("");
}

function isFence(node: MathNode | undefined): boolean {
  return node?.name === "mo" && node.attrs.fence === "true";
}

interface NaryParts {
  chr: string;
  sub: MathNode | null;
  sup: MathNode | null;
  limLoc: "undOvr" | "subSup";
}

/** 节点是否是（带或不带上下限的）大型运算符；是则拆出运算符与上下限。 */
function naryParts(node: MathNode): NaryParts | null {
  if (node.name === "mo") {
    const chr = textOf(node).trim();
    return NARY.has(chr) ? { chr, sub: null, sup: null, limLoc: INTEGRALS.has(chr) ? "subSup" : "undOvr" } : null;
  }
  const scripted: Record<string, ["subSup" | "undOvr", number, number]> = {
    msub: ["subSup", 1, -1], msup: ["subSup", -1, 1], msubsup: ["subSup", 1, 2],
    munder: ["undOvr", 1, -1], mover: ["undOvr", -1, 1], munderover: ["undOvr", 1, 2],
  };
  const shape = scripted[node.name];
  if (!shape) return null;
  const kids = elements(node);
  const base = kids[0];
  if (base?.name !== "mo") return null;
  const chr = textOf(base).trim();
  if (!NARY.has(chr)) return null;
  const [limLoc, subIndex, supIndex] = shape;
  return { chr, sub: subIndex > 0 ? kids[subIndex] ?? null : null, sup: supIndex > 0 ? kids[supIndex] ?? null : null, limLoc };
}

function naryOmml(parts: NaryParts, operand: string, ctx: Context): string {
  const props = [
    `<m:chr m:val="${xmlEscape(parts.chr)}"/>`,
    `<m:limLoc m:val="${parts.limLoc}"/>`,
    parts.sub ? "" : '<m:subHide m:val="1"/>',
    parts.sup ? "" : '<m:supHide m:val="1"/>',
  ].join("");
  const sub = parts.sub ? convert(parts.sub, ctx) : "";
  const sup = parts.sup ? convert(parts.sup, ctx) : "";
  return `<m:nary><m:naryPr>${props}</m:naryPr><m:sub>${sub}</m:sub><m:sup>${sup}</m:sup><m:e>${operand}</m:e></m:nary>`;
}

/**
 * 相邻、同一 mathvariant 的单字 mi 合成一个 run：KaTeX 把 \mathrm{Loss} 拆成四个 mi，逐字成 run
 * 在部分排版器里会被当成四个符号、字距拉开；没写 mathvariant 的斜体变量（xy 是乘积）不合。
 */
function mergeRuns(items: MathNode[]): MathNode[] {
  const merged: MathNode[] = [];
  for (const node of items) {
    const last = merged[merged.length - 1];
    const variant = node.attrs.mathvariant;
    if (node.name === "mi" && last?.name === "mi" && variant && variant === last.attrs.mathvariant) {
      merged[merged.length - 1] = { ...last, children: [{ name: "#text", attrs: {}, children: [], text: textOf(last) + textOf(node) }] };
      continue;
    }
    merged.push(node);
  }
  return merged;
}

/** 一串兄弟节点：大型运算符把紧随其后的一项（或嵌套的大型运算符连同它的项）收作 m:e。 */
function convertSequence(nodes: MathNode[], ctx: Context): string {
  const items = mergeRuns(nodes.filter(node => node.name !== "#text" || node.text.trim()));
  let out = "";
  for (let index = 0; index < items.length;) {
    const [omml, used] = convertAt(items, index, ctx);
    out += omml;
    index += used;
  }
  return out;
}

function convertAt(items: MathNode[], index: number, ctx: Context): [string, number] {
  const node = items[index];
  const parts = naryParts(node);
  if (!parts) return [convert(node, ctx), 1];
  const next = items[index + 1];
  if (!next || (next.name === "mo" && NOT_OPERAND.has(textOf(next).trim()))) return [naryOmml(parts, "", ctx), 1];
  const [operand, used] = convertAt(items, index + 1, ctx);
  return [naryOmml(parts, operand, ctx), 1 + used];
}

function bar(base: string, position: "top" | "bot"): string {
  return `<m:bar><m:barPr><m:pos m:val="${position}"/></m:barPr><m:e>${base}</m:e></m:bar>`;
}

function groupChr(base: string, chr: string, position: "top" | "bot"): string {
  const vertJc = position === "top" ? "bot" : "top";
  return `<m:groupChr><m:groupChrPr><m:chr m:val="${xmlEscape(chr)}"/><m:pos m:val="${position}"/><m:vertJc m:val="${vertJc}"/></m:groupChrPr><m:e>${base}</m:e></m:groupChr>`;
}

function convertOver(node: MathNode, ctx: Context): string {
  const [base, over] = elements(node);
  const baseOmml = base ? convert(base, ctx) : "";
  const mark = textOf(over).trim();
  if (over?.name === "mo") {
    if (BRACES_TOP.has(mark)) return groupChr(baseOmml, mark, "top");
    const accent = node.attrs.accent === "true" || over.attrs.accent === "true";
    if (accent) {
      if (OVER_BARS.has(mark) && over.attrs.stretchy === "true") return bar(baseOmml, "top");
      if (over.attrs.stretchy === "true" && /[\u2190-\u21ff\u27f5-\u27ff]/.test(mark)) return groupChr(baseOmml, mark, "top");
      const chr = ACCENTS[mark] ?? (/^[\u0300-\u036f\u20d0-\u20ff]$/.test(mark) ? mark : "");
      if (chr) return `<m:acc><m:accPr><m:chr m:val="${chr}"/></m:accPr><m:e>${baseOmml}</m:e></m:acc>`;
    }
  }
  return `<m:limUpp><m:e>${baseOmml}</m:e><m:lim>${over ? convert(over, ctx) : ""}</m:lim></m:limUpp>`;
}

function convertUnder(node: MathNode, ctx: Context): string {
  const [base, under] = elements(node);
  const baseOmml = base ? convert(base, ctx) : "";
  const mark = textOf(under).trim();
  if (under?.name === "mo") {
    if (BRACES_BOTTOM.has(mark)) return groupChr(baseOmml, mark, "bot");
    if ((node.attrs.accentunder === "true" || under.attrs.stretchy === "true") && OVER_BARS.has(mark)) return bar(baseOmml, "bot");
  }
  return `<m:limLow><m:e>${baseOmml}</m:e><m:lim>${under ? convert(under, ctx) : ""}</m:lim></m:limLow>`;
}

function columnJustification(value: string | undefined): "left" | "right" | "center" {
  return value === "left" || value === "right" ? value : "center";
}

function convertTable(node: MathNode, ctx: Context): string {
  const rows = elements(node).filter(row => row.name === "mtr" || row.name === "mlabeledtr");
  // KaTeX 的 \tag{n}：一行四格（50% 空格 / 公式 / 50% 空格 / 编号）→ 公式 + 全角空格 + 编号
  if (node.attrs.width === "100%" && rows.length === 1) {
    const cells = elements(rows[0]);
    if (cells.length === 4 && cells[0].attrs.width === "50%" && cells[2].attrs.width === "50%") {
      return `${convertSequence(cells[1].children, ctx)}${run("\u2003\u2003", { sty: "p" })}${run(textOf(cells[3]).trim(), { nor: true })}`;
    }
  }
  const cellRows = rows.map(row => elements(row).filter(cell => cell.name === "mtd"));
  const columns = Math.max(1, ...cellRows.map(cells => cells.length));
  const aligns = (node.attrs.columnalign ?? "center").trim().split(/\s+/);
  const mcs = Array.from({ length: columns }, (_, index) => {
    const justification = columnJustification(aligns[Math.min(index, aligns.length - 1)]);
    return `<m:mc><m:mcPr><m:count m:val="1"/><m:mcJc m:val="${justification}"/></m:mcPr></m:mc>`;
  }).join("");
  const body = cellRows.map(cells => {
    const padded = [...cells.map(cell => `<m:e>${convertSequence(cell.children, ctx)}</m:e>`)];
    while (padded.length < columns) padded.push("<m:e></m:e>");
    return `<m:mr>${padded.join("")}</m:mr>`;
  }).join("");
  return `<m:m><m:mPr><m:mcs>${mcs}</m:mcs></m:mPr>${body}</m:m>`;
}

function spaceFor(width: string | undefined): string {
  const em = /^(-?[\d.]+)em$/.exec(width ?? "")?.[1];
  // 没写宽度的 mspace（\allowbreak 等）是零宽
  const size = em === undefined ? 0 : Number(em);
  if (!(size > 0)) return "";
  if (size >= 1.8) return "\u2003\u2003";
  if (size >= 0.9) return "\u2003";
  if (size >= 0.4) return "\u2002";
  return "\u2009";
}

function convert(node: MathNode, ctx: Context): string {
  switch (node.name) {
    case "#text":
      return node.text.trim() ? run(node.text, { sty: "p" }) : "";
    case "annotation":
    case "annotation-xml":
      return "";
    case "mstyle":
      return convertSequence(node.children, { ...ctx, variant: node.attrs.mathvariant ?? ctx.variant });
    case "mrow": {
      const kids = elements(node);
      if (isFence(kids[0])) {
        const closes = kids.length > 1 && isFence(kids[kids.length - 1]);
        const begin = textOf(kids[0]).trim();
        const end = closes ? textOf(kids[kids.length - 1]).trim() : "";
        const middle = kids.slice(1, closes ? -1 : undefined);
        return `<m:d><m:dPr><m:begChr m:val="${xmlEscape(begin)}"/><m:endChr m:val="${xmlEscape(end)}"/></m:dPr><m:e>${convertSequence(middle, ctx)}</m:e></m:d>`;
      }
      return convertSequence(node.children, ctx);
    }
    case "mi": {
      const text = textOf(node);
      const fallback: RunStyle = [...text.trim()].length > 1 ? { sty: "p" } : {};
      return run(text, variantStyle(node.attrs.mathvariant ?? ctx.variant, fallback));
    }
    case "mn":
      return run(textOf(node), variantStyle(node.attrs.mathvariant ?? ctx.variant, { sty: "p" }));
    case "mo": {
      const parts = naryParts(node);
      if (parts) return naryOmml(parts, "", ctx);
      const variant = node.attrs.mathvariant ?? ctx.variant;
      return run(textOf(node), variant === "bold" ? { sty: "b" } : { sty: "p" });
    }
    case "mtext":
    case "ms": {
      const text = textOf(node);
      return text.trim() ? run(text, { nor: true }) : run(text, { sty: "p" });
    }
    case "mspace":
      return run(spaceFor(node.attrs.width), { sty: "p" });
    case "mfrac": {
      const [numerator, denominator] = elements(node);
      const thickness = (node.attrs.linethickness ?? "").trim();
      const noBar = /^0(?:\.0+)?(?:px|pt|em|ex)?$/.test(thickness);
      const props = noBar ? '<m:fPr><m:type m:val="noBar"/></m:fPr>' : "";
      return `<m:f>${props}<m:num>${numerator ? convert(numerator, ctx) : ""}</m:num><m:den>${denominator ? convert(denominator, ctx) : ""}</m:den></m:f>`;
    }
    case "msup":
    case "msub":
    case "msubsup": {
      const parts = naryParts(node);
      if (parts) return naryOmml(parts, "", ctx);
      const [base, first, second] = elements(node).map(child => convert(child, ctx));
      if (node.name === "msup") return `<m:sSup><m:e>${base ?? ""}</m:e><m:sup>${first ?? ""}</m:sup></m:sSup>`;
      if (node.name === "msub") return `<m:sSub><m:e>${base ?? ""}</m:e><m:sub>${first ?? ""}</m:sub></m:sSub>`;
      return `<m:sSubSup><m:e>${base ?? ""}</m:e><m:sub>${first ?? ""}</m:sub><m:sup>${second ?? ""}</m:sup></m:sSubSup>`;
    }
    case "mover":
    case "munder":
    case "munderover": {
      const parts = naryParts(node);
      if (parts) return naryOmml(parts, "", ctx);
      if (node.name === "mover") return convertOver(node, ctx);
      if (node.name === "munder") return convertUnder(node, ctx);
      const [base, under, over] = elements(node);
      const lower = `<m:limLow><m:e>${base ? convert(base, ctx) : ""}</m:e><m:lim>${under ? convert(under, ctx) : ""}</m:lim></m:limLow>`;
      return `<m:limUpp><m:e>${lower}</m:e><m:lim>${over ? convert(over, ctx) : ""}</m:lim></m:limUpp>`;
    }
    case "msqrt":
      return `<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg></m:deg><m:e>${convertSequence(node.children, ctx)}</m:e></m:rad>`;
    case "mroot": {
      const [base, index] = elements(node);
      return `<m:rad><m:deg>${index ? convert(index, ctx) : ""}</m:deg><m:e>${base ? convert(base, ctx) : ""}</m:e></m:rad>`;
    }
    case "mtable":
      return convertTable(node, ctx);
    case "mphantom":
      return `<m:phant><m:phantPr><m:show m:val="0"/></m:phantPr><m:e>${convertSequence(node.children, ctx)}</m:e></m:phant>`;
    case "menclose":
      return /box|roundedbox/.test(node.attrs.notation ?? "")
        ? `<m:borderBox><m:e>${convertSequence(node.children, ctx)}</m:e></m:borderBox>`
        : convertSequence(node.children, ctx);
    default:
      // math / semantics / mpadded / merror 及未知容器：按顺序展开子节点
      return convertSequence(node.children, ctx);
  }
}

/** MathML（或 KaTeX 的整段输出）→ `<m:oMath>…</m:oMath>`；解析不到 <math> 返回 null。 */
export function mathmlToOmml(mathml: string): string | null {
  const math = parseMathml(mathml);
  if (!math) return null;
  return `<m:oMath>${convert(math, {})}</m:oMath>`;
}
