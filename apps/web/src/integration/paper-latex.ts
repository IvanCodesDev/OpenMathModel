/**
 * PaperDocument → 可直接用 xelatex 编译的 .tex（Overleaf 选 XeLaTeX 同样可用）。
 *
 * 与编辑器所见一致：块级公式不自动编号（\tag 照留），公式后的续段紧接 \end{equation*}、
 * 不空行（LaTeX 视作同一段、不缩进）；表题「表 N …」进表格浮动体，图题进图浮动体。
 * 兼容处理：KaTeX 认而标准 LaTeX 缺的常用命令（\R、\argmin…）在导言区补齐；公式里直接写的
 * Unicode 希腊字母 / 关系符换成命令，裸中文包进 \text{}；长拉丁串包进 \ommid{…}，断点由导言区的宏给。
 */

import {
  type Block,
  type Inline,
  type PaperDocument,
  type TableCell,
  LONG_TOKEN,
  SOFT_BREAK,
  allowInlineMathBreaks,
  inlineText,
  insertSoftBreaks,
  stripControlChars,
  tableLayout,
} from "./paper-document";

export interface LatexOptions {
  /** 图件在导出包里的相对路径（figures/…）；null = 字节没带上，只留注释占位。 */
  imagePath: (index: number) => string | null;
}

const TEXT_SPECIALS: Record<string, string> = {
  "\\": "\\textbackslash{}",
  "%": "\\%",
  $: "\\$",
  "#": "\\#",
  "&": "\\&",
  _: "\\_",
  "{": "\\{",
  "}": "\\}",
  "~": "\\textasciitilde{}",
  "^": "\\textasciicircum{}",
  "\u00a0": "~",
  [SOFT_BREAK]: "\\allowbreak{}",
};

/** 正文文本转义：单遍映射（先换反斜杠再转义花括号会把 \textbackslash{} 自己转坏）。 */
export function latexEscapeText(text: string): string {
  return stripControlChars(text).replace(/[\\%$#&_{}~^\u00a0\u200b]/g, char => TEXT_SPECIALS[char]);
}

const LONG_TOKEN_PART = new RegExp(`(${LONG_TOKEN.source})`);

/**
 * 正文文本：转义，长拉丁串包进 \ommid{…}。断点不逐个写进源码（每个字母后一个断点命令，
 * 源码就没法读了），由导言区的宏逐个记号判断。长串里要转义的只有 `_` 与 `\`；宏的参数里
 * 不能有花括号，所以反斜杠写成 `\textbackslash␣`（空格只是命令名的定界，不会排出来）。
 */
export function latexText(text: string): string {
  return stripControlChars(text)
    .split(LONG_TOKEN_PART)
    .map((part, index) => (index % 2
      ? `\\ommid{${part.replace(/[\\_]/g, char => (char === "_" ? "\\_" : "\\textbackslash "))}}`
      : latexEscapeText(part)))
    .join("");
}

const MATH_UNICODE: Record<string, string> = {
  α: "\\alpha", β: "\\beta", γ: "\\gamma", δ: "\\delta", ε: "\\varepsilon", ϵ: "\\epsilon", ζ: "\\zeta", η: "\\eta",
  θ: "\\theta", ϑ: "\\vartheta", ι: "\\iota", κ: "\\kappa", λ: "\\lambda", μ: "\\mu", ν: "\\nu", ξ: "\\xi", π: "\\pi",
  ρ: "\\rho", σ: "\\sigma", ς: "\\varsigma", τ: "\\tau", υ: "\\upsilon", φ: "\\varphi", ϕ: "\\phi", χ: "\\chi",
  ψ: "\\psi", ω: "\\omega", Γ: "\\Gamma", Δ: "\\Delta", Θ: "\\Theta", Λ: "\\Lambda", Ξ: "\\Xi", Π: "\\Pi",
  Σ: "\\Sigma", Υ: "\\Upsilon", Φ: "\\Phi", Ψ: "\\Psi", Ω: "\\Omega",
  "≤": "\\le", "≥": "\\ge", "≠": "\\ne", "≈": "\\approx", "≡": "\\equiv", "∼": "\\sim", "∝": "\\propto",
  "×": "\\times", "·": "\\cdot", "÷": "\\div", "±": "\\pm", "∓": "\\mp", "∞": "\\infty", "∂": "\\partial",
  "∇": "\\nabla", "∈": "\\in", "∉": "\\notin", "⊂": "\\subset", "⊆": "\\subseteq", "⊃": "\\supset",
  "⊇": "\\supseteq", "∪": "\\cup", "∩": "\\cap", "∅": "\\emptyset", "∀": "\\forall", "∃": "\\exists",
  "¬": "\\neg", "∧": "\\wedge", "∨": "\\vee", "→": "\\to", "←": "\\leftarrow", "⇒": "\\Rightarrow",
  "⇐": "\\Leftarrow", "⇔": "\\Leftrightarrow", "↔": "\\leftrightarrow", "↦": "\\mapsto", "∑": "\\sum",
  "∏": "\\prod", "∫": "\\int", "√": "\\surd", "‖": "\\|", "…": "\\ldots", "⋯": "\\cdots", "°": "^{\\circ}",
  "′": "'", "ℝ": "\\mathbb{R}", "ℕ": "\\mathbb{N}", "ℤ": "\\mathbb{Z}", "ℚ": "\\mathbb{Q}", "ℂ": "\\mathbb{C}",
  "−": "-", "∗": "\\ast", "⊤": "\\top", "⊥": "\\perp", "⟨": "\\langle", "⟩": "\\rangle",
};
const MATH_UNICODE_PATTERN = new RegExp(`[${Object.keys(MATH_UNICODE).join("").replace(/[\\\]^-]/g, "\\$&")}]`, "g");
const CJK_RUN = /[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]+/g;
const TOP_LEVEL_ENVIRONMENTS = /^\\begin\{(align|align\*|gather|gather\*|multline|multline\*|equation|equation\*|eqnarray|eqnarray\*|flalign|flalign\*)\}/;

/** 公式源码兼容：Unicode 符号换命令、裸中文包 \text{}、控制字符去掉。 */
export function latexMath(tex: string): string {
  return stripControlChars(tex)
    .replace(/\u200b/g, "")
    .replace(MATH_UNICODE_PATTERN, char => {
      const command = MATH_UNICODE[char];
      return /^\\[a-zA-Z]+$/.test(command) ? `${command} ` : command;
    })
    .replace(CJK_RUN, run => `\\text{${run}}`)
    .trim();
}

function displayMath(tex: string): string {
  const source = latexMath(tex);
  if (TOP_LEVEL_ENVIRONMENTS.test(source)) return source;
  // 多行却没套环境（模型常把 \\ 直接写进 $$…$$）：有 & 用 aligned，没有用 gathered
  const multiline = /\\\\/.test(source) && !/\\begin\{/.test(source);
  const body = multiline
    ? `\\begin{${source.includes("&") ? "aligned" : "gathered"}}\n${source}\n\\end{${source.includes("&") ? "aligned" : "gathered"}}`
    : source;
  return `\\begin{equation*}\n${body}\n\\end{equation*}`;
}

function latexUrl(href: string): string {
  return href.replace(/\\/g, "%5C").replace(/\{/g, "%7B").replace(/\}/g, "%7D").replace(/([%#])/g, "\\$1");
}

export function latexInlines(inlines: readonly Inline[]): string {
  return inlines.map(inline => {
    if (inline.kind === "break") return "\\\\\n";
    if (inline.kind === "math") return `$${allowInlineMathBreaks(latexMath(inline.tex))}$`;
    const marks = inline.marks;
    let text = latexText(inline.text);
    if (marks.code) text = `\\texttt{${text}}`;
    if (marks.sup) text = `\\textsuperscript{${text}}`;
    if (marks.sub) text = `\\textsubscript{${text}}`;
    if (marks.bold) text = `\\textbf{${text}}`;
    if (marks.italic) text = `\\textit{${text}}`;
    if (marks.underline) text = `\\underline{${text}}`;
    if (marks.href) text = `\\href{${latexUrl(marks.href)}}{${text}}`;
    return text;
  }).join("");
}

/** 书签用的纯文本（hyperref 的 PDF 书签里不能放公式与格式命令）。 */
function bookmarkText(inlines: readonly Inline[]): string {
  return latexEscapeText(inlineText(inlines).replace(/\s+/g, " ").trim());
}

/** 图件浮动体：图宽不超过版心 80%、图高不超过版面 42%，等比缩放。 */
export function latexFigure(path: string, caption: string): string {
  const captionLine = caption ? `\n\\caption*{${caption}}` : "";
  return `\\begin{figure}[htbp]\n\\centering\n\\includegraphics[width=0.8\\linewidth,height=0.42\\textheight,keepaspectratio]{${path}}${captionLine}\n\\end{figure}`;
}

/** 版心宽（pt）：A4 宽 210mm 减两侧 2.5cm 页边距，与导言区 geometry 一致。 */
const TEXT_WIDTH_PT = 455.24;

/** 表格字号档：12pt 文档里 \small / \footnotesize / \scriptsize 的 1em（pt）与列间距 \tabcolsep（pt）。 */
const TABLE_LEVELS = [
  { size: "\\small", em: 10.95, sep: 6 },
  { size: "\\small", em: 10.95, sep: 4 },
  { size: "\\footnotesize", em: 10, sep: 3 },
  { size: "\\scriptsize", em: 8, sep: 2 },
] as const;
type TableLevel = (typeof TABLE_LEVELS)[number];

const CJK_CHAR = /[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]/;

/** 字宽（em）：西文按 Latin Modern 分档、宁宽勿窄（Termes 更窄），汉字与全角符号 1em。 */
function glyphWidth(char: string): number {
  if (CJK_CHAR.test(char)) return 1;
  if (/[0-9_/$#]/.test(char)) return 0.5;
  if (/[ijl.,:;'!|]/.test(char)) return 0.28;
  if (/[frtI()[\]-]/.test(char)) return 0.4;
  if (/[MW]/.test(char)) return 0.95;
  if (/[mw]/.test(char)) return 0.8;
  if (/[a-z]/.test(char)) return 0.53;
  if (/[A-Z]/.test(char)) return 0.75;
  if (/[=+<>%&]/.test(char)) return 0.8;
  return char === " " ? 0.33 : 0.6;
}

function mathWidth(tex: string): number {
  return tex.replace(/\\[a-zA-Z]+/g, "x").replace(/[{}^_\s]/g, "").length * 0.55;
}

/**
 * 单元格估宽（em）：natural 是不折行时最宽的一行；longest 是最长的不可断片段——西文词、数字串、
 * 公式（按 8em 封顶，TeX 能在运算符处断），汉字之间、空格与软断点处都能断。
 */
function measureCell(inlines: readonly Inline[]): { natural: number; longest: number } {
  let natural = 0;
  let line = 0;
  let longest = 0;
  let run = 0;
  const endRun = () => {
    longest = Math.max(longest, run);
    run = 0;
  };
  for (const inline of inlines) {
    if (inline.kind === "break") {
      natural = Math.max(natural, line);
      line = 0;
      endRun();
    } else if (inline.kind === "math") {
      const width = mathWidth(inline.tex);
      line += width;
      run += Math.min(width, 8);
    } else {
      for (const char of insertSoftBreaks(inline.text, { letters: false })) {
        if (char === SOFT_BREAK || char === " ") {
          line += char === " " ? glyphWidth(char) : 0;
          endRun();
          continue;
        }
        const width = glyphWidth(char);
        line += width;
        if (CJK_CHAR.test(char)) {
          endRun();
          longest = Math.max(longest, width);
        } else {
          run += width;
        }
      }
    }
  }
  endRun();
  return { natural: Math.max(natural, line), longest };
}

/** 正文短于这个宽度（em）的列不折行：数字、单位、短标签折成两行比挤一点更难看。 */
const SHORT_BODY = 8;
/** 表头短于这个宽度（em）也不折行，免得「符号」「单位」被拆成一字一行。 */
const SHORT_HEAD = 4;
/** 每列多留的余量（em）：汉字估宽是准的，列宽比例向下取整后正好排满的一行会被挤出最后一个字。 */
const CELL_SLACK = 0.15;

export interface LatexTableLayout {
  level: TableLevel;
  /** null = 自然宽度（c 列、不折行）；否则每列占版心的比例与对齐（正文排得下一行就居中）。 */
  columns: { fraction: number; center: boolean }[] | null;
}

/**
 * 三线表列宽：自然宽度排得下就用 c 列（先试默认列距，再把列距收到 4pt）。排不下时每列有一个
 * 下限——最长的不可断片段、短正文、短表头——下限之和排得下就按「下限 + 剩余空间按（自然宽 − 下限）
 * 比例分」给宽度；排不下依次降到 \footnotesize、\scriptsize 并收紧列距再试，实在排不下按下限比例硬分。
 */
export function latexTableLayout(rows: readonly TableCell[][], columns: number, headCount: number): LatexTableLayout {
  const measures = rows.map(row => Array.from({ length: columns }, (_, index) => measureCell(row[index]?.inlines ?? [])));
  const widest = (from: number, to: number, column: number) =>
    Math.max(0, ...measures.slice(from, to).map(row => row[column].natural));
  const natural = Array.from({ length: columns }, (_, column) => Math.max(0.5, widest(0, rows.length, column)) + CELL_SLACK);
  const body = Array.from({ length: columns }, (_, column) => widest(headCount, rows.length, column));
  const floor = Array.from({ length: columns }, (_, column) => {
    const head = widest(0, headCount, column);
    return CELL_SLACK + Math.max(
      ...measures.map(row => row[column].longest),
      body[column] <= SHORT_BODY ? body[column] : 0,
      head <= SHORT_HEAD ? head : 0,
    );
  });
  const sum = (values: readonly number[]) => values.reduce((total, value) => total + value, 0);
  const room = (level: TableLevel) => (TEXT_WIDTH_PT - columns * 2 * level.sep) / level.em;
  const hasBreak = rows.some(row => row.some(cell => cell.inlines.some(inline => inline.kind === "break")));
  if (!hasBreak) {
    const fits = TABLE_LEVELS.slice(0, 2).find(level => sum(natural) <= room(level));
    if (fits) return { level: fits, columns: null };
  }
  const level = TABLE_LEVELS.find(candidate => sum(floor) <= room(candidate)) ?? TABLE_LEVELS[TABLE_LEVELS.length - 1];
  const available = room(level);
  const spare = sum(natural) - sum(floor);
  const share = spare > 0 ? Math.min(1, (available - sum(floor)) / spare) : 0;
  const widths = sum(floor) > available
    ? floor.map(width => (width * available) / sum(floor))
    : floor.map((width, column) => width + (natural[column] - width) * share);
  return {
    level,
    columns: widths.map((width, column) => ({
      fraction: Math.floor(((width * level.em + 2 * level.sep) / TEXT_WIDTH_PT) * 1000) / 1000,
      center: body[column] <= width + 1e-6,
    })),
  };
}

/** 单元格里的换行：p 列用 \newline；c 列不能换行，换成空格（\\ 在表格里是换行而不是单元格内断行）。 */
function cellLatex(inlines: readonly Inline[], wrapping: boolean): string {
  const lines: Inline[][] = [[]];
  for (const inline of inlines) {
    if (inline.kind === "break") lines.push([]);
    else lines[lines.length - 1].push(inline);
  }
  return lines.map(latexInlines).join(wrapping ? "\\newline " : " ");
}

function cellLine(row: readonly TableCell[], columns: number, wrapping: boolean): string {
  return `${Array.from({ length: columns }, (_, index) => (row[index] ? cellLatex(row[index].inlines, wrapping) : "")).join(" & ")} \\\\`;
}

/**
 * 三线表：列宽见 latexTableLayout，折行列用导言区定义的 C{比例} / L{比例}（居中 / 左对齐的 p 列，
 * 宽度随 \linewidth）；行数多到一页放不下的用 longtable，允许跨页、表头每页重复。
 */
function latexTable(block: Extract<Block, { kind: "table" }>): string {
  const rows = block.rows;
  const { columns, headCount } = tableLayout(rows);
  const layout = latexTableLayout(rows, columns, headCount);
  const wrapping = layout.columns !== null;
  const head = rows.slice(0, headCount).map(row => cellLine(row, columns, wrapping));
  const body = rows.slice(headCount).map(row => cellLine(row, columns, wrapping));
  const caption = block.caption?.length ? latexInlines(block.caption) : "";
  const spec = layout.columns
    ? layout.columns.map(column => `${column.center ? "C" : "L"}{${column.fraction.toFixed(3)}}`).join("")
    : "c".repeat(columns);
  const setup = layout.level.size + (layout.level.sep === 6 ? "" : `\\setlength{\\tabcolsep}{${layout.level.sep}pt}`);

  if (rows.length > 28) {
    const headBlock = head.length ? `${head.join("\n")}\n\\midrule\n` : "";
    return [
      `{${setup}`,
      `\\begin{longtable}{${spec}}`,
      caption ? `\\caption*{${caption}}\\\\` : "",
      "\\toprule",
      `${headBlock}\\endfirsthead`,
      "\\toprule",
      `${headBlock}\\endhead`,
      "\\bottomrule",
      "\\endlastfoot",
      body.join("\n"),
      "\\end{longtable}}",
    ].filter(Boolean).join("\n");
  }

  return [
    "\\begin{table}[htbp]",
    "\\centering",
    caption ? `\\caption*{${caption}}` : "",
    setup,
    `\\begin{tabular}{${spec}}`,
    "\\toprule",
    head.length ? `${head.join("\n")}\n\\midrule` : "",
    body.join("\n"),
    "\\bottomrule",
    "\\end{tabular}",
    "\\end{table}",
  ].filter(Boolean).join("\n");
}

function verbatim(text: string): string {
  const safe = text.replace(/\t/g, "    ").replace(/\\end\{Verbatim\}/g, "\\end {Verbatim}");
  return `\\begin{Verbatim}\n${safe}\n\\end{Verbatim}`;
}

function latexBlock(block: Block, options: LatexOptions): string {
  switch (block.kind) {
    case "heading": {
      const text = latexInlines(block.inlines);
      if (block.role === "abstract") return `\\begin{center}\n{\\zihao{4}\\bfseries ${text}\\par}\n\\end{center}`;
      const command = block.level === 1 ? "section" : block.level === 2 ? "subsection" : "subsubsection";
      return `\\phantomsection\n\\${command}*{${text}}\n\\addcontentsline{toc}{${command}}{${bookmarkText(block.inlines)}}`;
    }
    case "paragraph": {
      const text = latexInlines(block.inlines);
      if (block.role === "note") return `{\\noindent\\small ${text}\\par}`;
      if (block.align === "center") return `\\begin{center}\n${text}\n\\end{center}`;
      if (block.align === "right") return `\\begin{flushright}\n${text}\n\\end{flushright}`;
      return block.role === "keywords" || block.continued ? `\\noindent ${text}` : text;
    }
    case "math":
      return displayMath(block.tex);
    case "list": {
      const environment = block.ordered ? "enumerate" : "itemize";
      return `\\begin{${environment}}\n${block.items.map(item => `\\item ${latexInlines(item)}`).join("\n")}\n\\end{${environment}}`;
    }
    case "quote":
      return `\\begin{quote}\n${latexInlines(block.inlines)}\n\\end{quote}`;
    case "code":
      return verbatim(block.text);
    case "table":
      return latexTable(block);
    case "figure": {
      const caption = latexInlines(block.caption);
      const path = block.image === null ? null : options.imagePath(block.image);
      if (path) return latexFigure(path, caption);
      const label = latexEscapeText(inlineText(block.caption) || block.alt || "未命名").replace(/\n/g, " ");
      return `% 插图：${label}（图片文件未能随导出带走：另存到 figures/ 后取消下一行注释并改文件名）\n% \\includegraphics[width=0.8\\linewidth]{figures/figure.png}`;
    }
    case "rule":
      return "\\par\\noindent\\rule{\\linewidth}{0.4pt}\\par";
  }
}

const PREAMBLE = [
  "% !TEX program = xelatex",
  "% 由 OpenMathModel 论文编辑器导出：xelatex 编译（Overleaf 选 XeLaTeX）；插图在同目录的 figures/ 下。",
  "\\documentclass[12pt,a4paper]{article}",
  "\\usepackage[UTF8]{ctex}",
  "\\usepackage[a4paper,margin=2.5cm]{geometry}",
  "\\usepackage{amsmath,amssymb,mathtools,bm}",
  "\\usepackage{graphicx}",
  "\\usepackage{array,booktabs,longtable}",
  "\\usepackage[font=small]{caption}",
  "\\usepackage{enumitem}",
  "\\usepackage{fvextra}",
  "\\usepackage{titlesec}",
  "\\usepackage[hidelinks]{hyperref}",
  "% 西文用 Times 风格：TeX Gyre Termes 随 TeX Live / Tectonic 发行，按文件名找（字体名查不到系统没登记的字体）；没有就保持默认字体",
  "\\IfFontExistsTF{texgyretermes-regular.otf}{\\setmainfont{texgyretermes}[Extension=.otf,UprightFont=*-regular,BoldFont=*-bold,ItalicFont=*-italic,BoldItalicFont=*-bolditalic]}{}",
  "\\setlength{\\parindent}{2em}",
  "% 长标识符、长数字串排不进一行时放松这一行的字距，不冲出版心",
  "\\setlength{\\emergencystretch}{3em}",
  "% \\ommid{长拉丁串}：_ = / : , ; \\ 之后、字母前的 . 之后可断；字母之间也可断，但罚分同连字符断词，",
  "% 断开后有一侧不足 2 个字母时罚分更高；连续 12 个记号没有断点（长小数、哈希）时补一个带罚分的断点",
  "\\makeatletter",
  "\\newcount\\omm@run",
  "\\newcount\\omm@lead",
  "\\newif\\ifomm@a \\newif\\ifomm@b \\newif\\ifomm@c \\newif\\ifomm@sep",
  "\\def\\omm@end{\\omm@end}",
  "\\def\\omm@letter#1#2#3{\\ifcat a\\noexpand#3#1\\else#2\\fi}",
  "\\DeclareRobustCommand\\ommid[1]{\\omm@run\\z@\\omm@lead\\z@\\omm@id#1\\omm@end\\omm@end\\omm@end}",
  "\\def\\omm@id#1#2#3{\\ifx\\omm@end#1\\expandafter\\@gobble\\else\\expandafter\\@firstofone\\fi{#1\\omm@gap#1#2#3\\omm@id#2#3}}",
  "\\def\\omm@gap#1#2#3{\\omm@letter\\omm@atrue\\omm@afalse#1\\omm@letter\\omm@btrue\\omm@bfalse#2\\omm@letter\\omm@ctrue\\omm@cfalse#3%",
  "  \\ifomm@a\\advance\\omm@lead\\@ne\\else\\omm@lead\\z@\\fi\\omm@testsep#1%",
  "  \\ifx\\omm@end#2\\else\\ifomm@sep\\omm@strong\\else\\ifomm@b\\if.\\noexpand#1\\omm@strong\\else",
  "  \\ifomm@a\\ifomm@c\\ifnum\\omm@lead>\\@ne\\omm@weak\\else\\omm@weaker\\fi\\else\\omm@weaker\\fi\\else\\omm@count\\fi",
  "  \\fi\\else\\omm@count\\fi\\fi\\fi}",
  "\\def\\omm@testsep#1{\\omm@septrue\\ifx\\_#1\\else\\ifx\\textbackslash#1\\else\\if=\\noexpand#1\\else\\if/\\noexpand#1\\else",
  "  \\if:\\noexpand#1\\else\\if,\\noexpand#1\\else\\if;\\noexpand#1\\else\\omm@sepfalse\\fi\\fi\\fi\\fi\\fi\\fi\\fi}",
  "\\def\\omm@strong{\\allowbreak\\omm@run\\z@}",
  "\\def\\omm@weak{\\penalty\\hyphenpenalty\\omm@run\\z@}",
  "\\def\\omm@weaker{\\penalty\\@medpenalty\\omm@run\\z@}",
  "\\def\\omm@count{\\advance\\omm@run\\@ne\\ifnum\\omm@run>11 \\omm@weak\\fi}",
  "\\makeatother",
  "% 表格折行列：C 居中、L 左对齐，参数是占版心的比例（已扣两侧列距）",
  "\\newcolumntype{C}[1]{>{\\centering\\arraybackslash}p{\\dimexpr#1\\linewidth-2\\tabcolsep\\relax}}",
  "\\newcolumntype{L}[1]{>{\\raggedright\\arraybackslash}p{\\dimexpr#1\\linewidth-2\\tabcolsep\\relax}}",
  "\\titleformat*{\\section}{\\zihao{4}\\bfseries}",
  "\\titleformat*{\\subsection}{\\zihao{-4}\\bfseries}",
  "\\titleformat*{\\subsubsection}{\\zihao{-4}\\bfseries}",
  "\\titlespacing*{\\section}{0pt}{2ex plus .5ex minus .2ex}{1ex plus .2ex}",
  "\\titlespacing*{\\subsection}{0pt}{1.5ex plus .5ex minus .2ex}{.8ex plus .2ex}",
  "\\titlespacing*{\\subsubsection}{0pt}{1.2ex plus .5ex minus .2ex}{.6ex plus .2ex}",
  "\\setlist{leftmargin=2.5em,itemsep=0pt,topsep=3pt}",
  "\\fvset{breaklines=true,breakanywhere=true,fontsize=\\small,frame=single,framesep=2mm}",
  "% KaTeX 认、标准 LaTeX 没有的常用命令",
  "\\providecommand{\\R}{\\mathbb{R}}",
  "\\providecommand{\\N}{\\mathbb{N}}",
  "\\providecommand{\\Z}{\\mathbb{Z}}",
  "\\providecommand{\\Q}{\\mathbb{Q}}",
  "\\providecommand{\\C}{\\mathbb{C}}",
  "\\providecommand{\\Reals}{\\mathbb{R}}",
  "\\providecommand{\\argmin}{\\operatorname*{arg\\,min}}",
  "\\providecommand{\\argmax}{\\operatorname*{arg\\,max}}",
  "\\providecommand{\\Bbb}[1]{\\mathbb{#1}}",
  "\\providecommand{\\bold}[1]{\\mathbf{#1}}",
  "\\providecommand{\\mathscr}[1]{\\mathcal{#1}}",
];

/** 整篇 → .tex 源码。 */
export function paperDocumentToLatex(doc: PaperDocument, options: LatexOptions): string {
  const chunks: string[] = [];
  let previous: Block | null = null;
  for (const block of doc.blocks) {
    const latex = latexBlock(block, options);
    // 公式与它所在的段落之间只换一行：公式前空行会多出一个空段落的间距；公式后的续段
    // 紧接 \end{equation*}，LaTeX 视作同一段、不缩进
    const joinsParagraph = (block.kind === "math" && (previous?.kind === "paragraph" || previous?.kind === "math"))
      || (block.kind === "paragraph" && block.continued && previous?.kind === "math");
    const joiner = joinsParagraph ? "\n" : "\n\n";
    chunks.push(chunks.length ? joiner + latex : latex);
    previous = block;
  }
  return [
    ...PREAMBLE,
    "",
    "\\begin{document}",
    "",
    "\\begin{center}",
    `{\\zihao{3}\\bfseries ${latexText(doc.title)}\\par}`,
    "\\end{center}",
    "\\vspace{1ex}",
    "",
    chunks.join(""),
    "",
    "\\end{document}",
    "",
  ].join("\n");
}

/** 导出包里图件的文件名：ASCII 安全（graphicx 对空格、中文与特殊字符敏感），按序号前缀防重名。 */
export function latexImageName(rawName: string, index: number, mediaType: string): string {
  const extension = /\.([A-Za-z0-9]{1,5})$/.exec(rawName)?.[1]?.toLowerCase()
    ?? ({ "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg", "image/webp": "webp", "image/bmp": "bmp" } as Record<string, string>)[mediaType.toLowerCase()]
    ?? "png";
  const stem = rawName.replace(/\.[A-Za-z0-9]{1,5}$/, "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return `fig${index + 1}${stem ? `-${stem}` : ""}.${extension === "jpeg" ? "jpg" : extension}`;
}
