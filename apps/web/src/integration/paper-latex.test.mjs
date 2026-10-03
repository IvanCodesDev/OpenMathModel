import assert from "node:assert/strict";
import test from "node:test";
import { LONG_DESCRIPTION, documentUrl, math, sampleDocument, text, transpiled } from "./paper-export-fixture.mjs";

const { latexEscapeText, latexImageName, latexMath, latexTableLayout, latexText, paperDocumentToLatex } = await import(
  await transpiled("./paper-latex.ts", { "./paper-document": documentUrl })
);

const imagePath = index => (index === 0 ? "figures/fig1-fit-vs-baseline.png" : null);

test("text escaping is single-pass: a backslash does not get its own braces escaped", () => {
  assert.equal(latexEscapeText("a\\b {x} 100% R&D_1 #2 ~ ^"), "a\\textbackslash{}b \\{x\\} 100\\% R\\&D\\_1 \\#2 \\textasciitilde{} \\textasciicircum{}");
  assert.equal(latexEscapeText("x\u200by\u0001"), "x\\allowbreak{}y", "零宽断点 → \\allowbreak，控制字符去掉");
});

test("long latin tokens go into \\ommid with no braces inside; short ones are plain escaped text", () => {
  assert.equal(latexText("误差 5.9e-11、R&D_1"), "误差 5.9e-11、R\\&D\\_1");
  assert.equal(latexText("路径 C:\\Users\\Administrator\\x_y.txt 下"), "路径 \\ommid{C:\\textbackslash Users\\textbackslash Administrator\\textbackslash x\\_y.txt} 下");
  assert.equal(latexText("a\u0001bcdefghijklmnopqrstu"), "\\ommid{abcdefghijklmnopqrstu}", "先去控制字符再认长串");
});

test("math source: unicode symbols become commands, bare CJK goes into \\text", () => {
  assert.equal(latexMath("θ≤1，训练集"), "\\theta \\le 1\\text{，训练集}");
  assert.equal(latexMath("x·y×z → ∞"), "x\\cdot y\\times z \\to  \\infty");
  assert.equal(latexMath("\\text{否则}"), "\\text{\\text{否则}}", "已有 \\text 再包一层也能编译");
});

test("document: preamble, title, abstract, keywords and section commands", () => {
  const tex = paperDocumentToLatex(sampleDocument(), { imagePath });
  assert.ok(tex.startsWith("% !TEX program = xelatex\n"));
  for (const line of [
    "\\documentclass[12pt,a4paper]{article}",
    "\\usepackage[UTF8]{ctex}",
    "\\usepackage{array,booktabs,longtable}",
    "\\usepackage{fvextra}",
    "\\setlength{\\emergencystretch}{3em}",
    "\\newcolumntype{C}[1]{>{\\centering\\arraybackslash}p{\\dimexpr#1\\linewidth-2\\tabcolsep\\relax}}",
    "\\newcolumntype{L}[1]{>{\\raggedright\\arraybackslash}p{\\dimexpr#1\\linewidth-2\\tabcolsep\\relax}}",
    "\\providecommand{\\R}{\\mathbb{R}}",
    "\\providecommand{\\argmin}{\\operatorname*{arg\\,min}}",
  ]) {
    assert.ok(tex.includes(`${line}\n`), line);
  }
  assert.ok(tex.includes("\\IfFontExistsTF{texgyretermes-regular.otf}{\\setmainfont{texgyretermes}[Extension=.otf,"), "Termes 按文件名找：Tectonic 与 TeX Live 都能命中");
  assert.ok(tex.includes("{\\zihao{3}\\bfseries 从零实现反向传播：手写 MLP 的推导与检验\\par}"));
  assert.ok(tex.includes("\\begin{center}\n{\\zihao{4}\\bfseries 摘要\\par}\n\\end{center}"));
  assert.ok(tex.includes("梯度检验 \\ommid{metrics.grad\\_check\\_max\\_abs\\_err=5.9e-11}，结论可信。"), "长串包进 \\ommid，断点交给宏");
  assert.ok(tex.includes("\\DeclareRobustCommand\\ommid[1]{"), "导言区定义 \\ommid");
  assert.ok(tex.includes("\\noindent \\textbf{关键词：}反向传播；梯度检验"));
  assert.ok(tex.includes("\\phantomsection\n\\section*{1 问题重述}\n\\addcontentsline{toc}{section}{1 问题重述}"));
  assert.ok(tex.includes("\\subsection*{1.1 子问题}"));
  assert.ok(tex.trimEnd().endsWith("\\end{document}"));
});

test("display math is unnumbered and sits inside its paragraph: no blank line before it, the continuation follows without indent", () => {
  const tex = paperDocumentToLatex(sampleDocument(), { imagePath });
  assert.ok(tex.includes("学习目标为\n\\begin{equation*}\n\\min_{\\theta} J(\\theta)\n\\end{equation*}\n\\noindent 其中 $J_i$ 为交叉熵，100\\% 的 R\\&D\\_x 成本 \\#1。"));
  const inline = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [math("\\theta=(W^{(1)},b^{(1)})")] }] }, { imagePath });
  assert.ok(inline.includes("$\\theta=(W^{(1)},\\allowbreak b^{(1)})$"), "长行内公式在顶层逗号后可断行");
  const multi = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "math", tex: "a &= b \\\\ c &= d" }, { kind: "math", tex: "x \\\\ y" }, { kind: "math", tex: "\\begin{align}a&=b\\end{align}" }] }, { imagePath });
  assert.ok(multi.includes("\\begin{equation*}\n\\begin{aligned}\na &= b \\\\ c &= d\n\\end{aligned}\n\\end{equation*}"), "多行带 & → aligned");
  assert.ok(multi.includes("\\begin{gathered}\nx \\\\ y\n\\end{gathered}"), "多行无 & → gathered");
  assert.ok(multi.includes("\n\\begin{align}a&=b\\end{align}\n") && !multi.includes("\\begin{equation*}\n\\begin{align}"), "顶层环境原样输出");
});

test("lists, wide tables with captions, figures, code, quotes and rules", () => {
  const tex = paperDocumentToLatex(sampleDocument(), { imagePath });
  assert.ok(tex.includes("\\begin{enumerate}\n\\item 前向传播\n\\item 反向传播 \\textbf{加粗}\n\\end{enumerate}"));
  const symbolTable = /\\begin\{table\}\[htbp\]\n\\centering\n\\caption\*\{表 1 全文符号说明\}\n\\small\n\\begin\{tabular\}\{C\{(0\.\d{3})\}L\{(0\.\d{3})\}C\{(0\.\d{3})\}\}\n\\toprule\n符号 & 含义 & 单位 \\\\\n\\midrule\n\$\\theta\$ & /.exec(tex);
  assert.ok(symbolTable, "长文本列左对齐折行，短列居中");
  const fractions = symbolTable.slice(1).map(Number);
  const total = fractions.reduce((sum, fraction) => sum + fraction, 0);
  assert.ok(fractions[1] > 0.7 && total <= 1 && total > 0.99, `长列分走大部分版心、三列合计排满一行：${fractions}`);
  assert.ok(tex.includes(`${LONG_DESCRIPTION} & 无量纲 \\\\\n\\bottomrule\n\\end{tabular}\n\\end{table}`));
  assert.ok(tex.includes("\\includegraphics[width=0.8\\linewidth,height=0.42\\textheight,keepaspectratio]{figures/fig1-fit-vs-baseline.png}\n\\caption*{图 1 训练收敛曲线}"));
  assert.ok(tex.includes("% 插图：图 2 丢失的图（图片文件未能随导出带走"), "没带上字节的图留注释占位");
  assert.ok(tex.includes("\\begin{Verbatim}\ndef f(x):\n    return x  # 注释\n\\end{Verbatim}"), "代码原样、Tab 换空格");
  assert.ok(tex.includes("\\begin{quote}\n引用一句\n\\end{quote}"));
  assert.ok(tex.includes("\\par\\noindent\\rule{\\linewidth}{0.4pt}\\par"));
  assert.ok(tex.includes("见 \\href{https://example.org/a?b=1\\#c}{文档}。"), "链接里的 # 转义、控制字符去掉");
});

test("narrow tables keep natural width; very long tables switch to longtable", () => {
  const narrow = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "table", rows: [[{ inlines: [text("a")], header: true }, { inlines: [text("b")], header: true }], [{ inlines: [math("x")], header: false }, { inlines: [text("1")], header: false }]] }] }, { imagePath });
  assert.ok(narrow.includes("\\begin{tabular}{cc}"));
  const rows = [[{ inlines: [text("编号")], header: true }, { inlines: [text("说明")], header: true }]];
  for (let index = 0; index < 40; index += 1) rows.push([{ inlines: [text(String(index))], header: false }, { inlines: [text("说明文字")], header: false }]);
  const long = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "table", caption: [text("表 9 长表")], rows }] }, { imagePath });
  assert.ok(long.includes("{\\small\n\\begin{longtable}{cc}") && long.includes("\\caption*{表 9 长表}\\\\") && long.includes("\\endhead"));
  assert.ok(!long.includes("\\begin{table}"), "长表不放进浮动体");
  const wordy = rows.map((row, index) => (index ? [row[0], { inlines: [text(LONG_DESCRIPTION)], header: false }] : row));
  const longWrapped = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "table", rows: wordy }] }, { imagePath });
  assert.match(longWrapped, /\\begin\{longtable\}\{C\{0\.\d{3}\}L\{0\.\d{3}\}\}/, "长表同样按版心分列宽，长文本列折行");
});

test("wide numeric tables step down to a smaller size instead of overflowing: headers wrap, numbers stay on one line", () => {
  const header = ["数据集", "MLP Adam", "logreg", "kNN(k=5)", "多数类", "MLP Adam 标准差", "gain vs logreg (pp)", "gain vs kNN5 (pp)"];
  const rows = [
    header,
    ["iris", "0.99166667", "1.0", "0.95833333", "0.33333333", "0.01666667", "-0.833333", "3.333334"],
    ["synth_gauss", "0.93888889", "0.94333333", "0.91666667", "0.5", "0.00702728", "-0.444444", "2.222222"],
    ["synth_circles", "0.93222222", "0.52555556", "0.61666667", "0.5", "0.01586984", "40.666666", "31.555555"],
  ].map((row, index) => row.map(value => ({ inlines: [text(value)], header: index === 0 })));
  const layout = latexTableLayout(rows, 8, 1);
  assert.equal(layout.level.size, "\\footnotesize", "\\small 收紧列距也排不下 8 列 8 位小数");
  assert.ok(layout.columns.every(column => column.center), "正文都排得下一行：全部居中");
  const total = layout.columns.reduce((sum, column) => sum + column.fraction, 0);
  assert.ok(total <= 1 && total > 0.99, `合计 ${total}`);
  // 8 位小数在 \footnotesize 下约 4.8em：列宽扣掉两侧 3pt 列距后不能比它窄
  assert.ok(layout.columns.slice(1, 6).every(column => column.fraction * 455.24 - 6 >= 4.78 * 10));
  const tex = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "table", rows }] }, { imagePath });
  assert.ok(tex.includes("\\footnotesize\\setlength{\\tabcolsep}{3pt}\n\\begin{tabular}{C{"));

  const seven = latexTableLayout(rows.map(row => row.slice(0, 7)), 7, 1);
  assert.deepEqual([seven.level.size, seven.level.sep], ["\\small", 4], "差一点排下的先收列距，不降字号");
});

test("a line break inside a table cell stays inside the cell", () => {
  const rows = [
    [{ inlines: [text("项")], header: true }, { inlines: [text("说明")], header: true }],
    [{ inlines: [text("a")], header: false }, { inlines: [text("第一行"), { kind: "break" }, text("第二行")], header: false }],
  ];
  const tex = paperDocumentToLatex({ title: "t", images: [], blocks: [{ kind: "table", rows }] }, { imagePath });
  assert.ok(tex.includes("a & 第一行\\newline 第二行 \\\\\n"), "p 列里用 \\newline；\\\\ 在表格里会结束整行");
  assert.match(tex, /\\begin\{tabular\}\{C\{0\.\d{3}\}C\{0\.\d{3}\}\}/);
});

test("figure file names in the zip are ASCII-safe and numbered", () => {
  assert.equal(latexImageName("fit vs baseline.png", 0, "image/png"), "fig1-fit-vs-baseline.png");
  assert.equal(latexImageName("图 4 灵敏度.png", 3, "image/png"), "fig4-4.png", "中文去掉、只留 ASCII 部分");
  assert.equal(latexImageName("灵敏度.png", 4, "image/png"), "fig5.png");
  assert.equal(latexImageName("curve", 1, "image/jpeg"), "fig2-curve.jpg");
  assert.equal(latexImageName("a.JPEG", 2, "image/jpeg"), "fig3-a.jpg");
});
