import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import katex from "katex";
import ts from "typescript";

const source = await readFile(new URL("./mathml-omml.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { mathmlToOmml, parseMathml } = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const omml = (tex, display = false) =>
  mathmlToOmml(katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: false }));

/** 标签配平（自闭合除外）：Word 对不配平的 XML 直接拒开。 */
function assertBalanced(xml) {
  const stack = [];
  for (const [, closing, name, selfClosing] of xml.matchAll(/<(\/?)([\w:]+)[^>]*?(\/?)>/g)) {
    if (selfClosing) continue;
    if (closing) assert.equal(stack.pop(), name, `闭合标签 ${name} 不匹配`);
    else stack.push(name);
  }
  assert.deepEqual(stack, [], "有未闭合的标签");
}

test("parser: entities, self-closing tags and the KaTeX span wrapper", () => {
  const math = parseMathml('<span class="katex"><math><mrow><mo>&lt;</mo><mspace width="1em"/><mtext>a&amp;b&#x27;</mtext></mrow></math></span>');
  assert.equal(math.name, "math");
  const row = math.children[0];
  assert.deepEqual(row.children.map(child => child.name), ["mo", "mspace", "mtext"]);
  assert.equal(row.children[0].children[0].text, "<");
  assert.equal(row.children[1].attrs.width, "1em");
  assert.equal(row.children[2].children[0].text, "a&b'");
  assert.equal(parseMathml("<span>没有公式</span>"), null);
});

test("sums take the next term as their operand; display limits sit above/below, inline limits at the side", () => {
  const display = omml("\\frac{1}{N}\\sum_{i=1}^{N}J_i(\\theta)", true);
  assert.match(display, /^<m:oMath><m:f><m:num>.*<\/m:num><m:den>.*<\/m:den><\/m:f><m:nary>/);
  assert.ok(display.includes('<m:chr m:val="∑"/><m:limLoc m:val="undOvr"/>'));
  assert.match(display, /<m:e><m:sSub><m:e><m:r>.*>J<\/m:t><\/m:r><\/m:e><m:sub>.*>i<\/m:t>.*<\/m:sub><\/m:sSub><\/m:e><\/m:nary>/, "J_i 进了求和号的 m:e");
  const inline = omml("\\sum_{i=1}^{N} x_i");
  assert.ok(inline.includes('<m:limLoc m:val="subSup"/>'));
  const nested = omml("\\sum_{i}\\sum_{t} y_{it}", true);
  assert.match(nested, /<m:nary>.*<m:e><m:nary>.*<m:e><m:sSub>/, "嵌套求和：内层求和连同被加项进外层 m:e");
  assert.ok(omml("\\int_0^1 f(x)\\,dx", true).includes('<m:chr m:val="∫"/><m:limLoc m:val="subSup"/>'), "积分上下限在右侧");
  assert.ok(!omml("\\sum = 1").includes("<m:e><m:r><m:rPr><m:sty m:val=\"p\"/></m:rPr>"), "关系符不当被加项");
});

test("styles: italic variables, upright numbers / operators / function names, script / double-struck / bold, \\text as normal text", () => {
  const out = omml("\\min_{\\theta} \\mathcal{D} + \\mathbb{R}^{d} + \\mathbf{W} + \\boldsymbol{\\theta} + 2x + \\text{否则}", true);
  assert.ok(out.includes('<m:limLow><m:e><m:r><m:rPr><m:sty m:val="p"/></m:rPr>'), "min 正体、θ 在下");
  assert.ok(out.includes('<m:rPr><m:scr m:val="script"/><m:sty m:val="p"/></m:rPr>'), "\\mathcal");
  assert.ok(out.includes('<m:scr m:val="double-struck"/>'), "\\mathbb");
  assert.ok(out.includes('<m:sty m:val="b"/>') && out.includes('<m:sty m:val="bi"/>'), "\\mathbf / \\boldsymbol");
  assert.ok(out.includes('<m:rPr><m:sty m:val="p"/></m:rPr><w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/></w:rPr><m:t xml:space="preserve">2</m:t>'), "数字正体");
  assert.ok(out.includes('<m:r><w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/></w:rPr><m:t xml:space="preserve">x</m:t></m:r>'), "单字母变量默认斜体（不写 sty）");
  assert.ok(out.includes('<m:rPr><m:nor/></m:rPr>') && out.includes(">否则</m:t>"), "\\text 走普通文本");
  assert.ok(!out.includes("\u2061"), "函数应用符号不进 Word");
});

test("accents, bars, braces, roots and fences", () => {
  const accents = omml("\\hat{y} + \\bar{x} + \\vec{v} + \\overline{AB} + \\tilde{z}");
  assert.ok(accents.includes('<m:acc><m:accPr><m:chr m:val="\u0302"/></m:accPr>'), "\\hat");
  assert.ok(accents.includes('<m:chr m:val="\u0305"/>'), "\\bar");
  assert.ok(accents.includes('<m:chr m:val="\u20d7"/>'), "\\vec");
  assert.ok(accents.includes('<m:bar><m:barPr><m:pos m:val="top"/></m:barPr>'), "\\overline");
  assert.ok(accents.includes('<m:chr m:val="\u0303"/>'), "\\tilde");
  const brace = omml("\\underbrace{a+b}_{n}", true);
  assert.match(brace, /<m:limLow><m:e><m:groupChr><m:groupChrPr><m:chr m:val="⏟"\/><m:pos m:val="bot"\/>/);
  const roots = omml("\\sqrt{x}+\\sqrt[3]{y}");
  assert.ok(roots.includes('<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg></m:deg><m:e>'));
  assert.match(roots, /<m:rad><m:deg><m:r>.*>3<\/m:t><\/m:r><\/m:deg><m:e>/);
  const fenced = omml("\\left( \\frac{a}{b} \\right)", true);
  assert.match(fenced, /^<m:oMath><m:d><m:dPr><m:begChr m:val="\("\/><m:endChr m:val="\)"\/><\/m:dPr><m:e><m:f>/);
  assert.ok(omml("\\binom{n}{k}").includes('<m:fPr><m:type m:val="noBar"/></m:fPr>'), "二项式无分数线");
});

test("norms and absolute values use the upright glyphs Word's equation editor uses; width-less mspace adds nothing", () => {
  const out = omml("\\|\\theta\\|_2 + \\frac{1}{|\\mathcal{I}|}");
  assert.ok(out.includes(">\u2016</m:t>") && !out.includes("\u2225"), "范数 ‖（U+2016），不是斜的 ∥");
  assert.ok(out.includes(">|</m:t>") && !out.includes("\u2223"), "绝对值 / 基数用 ASCII 竖线");
  assert.ok(!omml("a,\\allowbreak b").includes("\u2009"), "\\allowbreak 的 mspace 不出空格");
});

test("\\mathrm words become one upright run; italic variables stay separate", () => {
  const out = omml("\\mathrm{Loss}(xy)");
  assert.ok(out.includes('<m:rPr><m:sty m:val="p"/></m:rPr><w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/></w:rPr><m:t xml:space="preserve">Loss</m:t>'));
  assert.ok(out.includes(">x</m:t></m:r><m:r>") && out.includes(">y</m:t>"), "乘积 xy 不合并");
});

test("matrices, cases and aligned become m:m with per-column justification; \\tag keeps its number", () => {
  const matrix = omml("\\begin{pmatrix} 1 & 0 \\\\ 0 & 1 \\end{pmatrix}", true);
  assert.match(matrix, /<m:d><m:dPr><m:begChr m:val="\("\/><m:endChr m:val="\)"\/><\/m:dPr><m:e><m:m>/);
  assert.equal((matrix.match(/<m:mr>/g) ?? []).length, 2);
  const cases = omml("f(x)=\\begin{cases} 1 & x>0 \\\\ 0 & \\text{否则} \\end{cases}", true);
  assert.ok(cases.includes('<m:begChr m:val="{"/><m:endChr m:val=""/>'), "cases 只有左花括号");
  assert.ok(cases.includes('<m:mcJc m:val="left"/>'));
  const aligned = omml("\\begin{aligned} a &= b+c \\\\ d &= e \\end{aligned}", true);
  assert.ok(aligned.includes('<m:mcJc m:val="right"/>') && aligned.includes('<m:mcJc m:val="left"/>'), "aligned：右 / 左列");
  assert.ok(!aligned.includes("&amp;"), "不用 & 对齐标记");
  const tagged = omml("\\tag{3} E=mc^2", true);
  assert.ok(!tagged.includes("<m:m>") && tagged.includes(">(3)</m:t>"));
});

test("formulas from a real paper all convert to balanced OMML", () => {
  const formulas = [
    ["\\min_{\\theta} J(\\theta)=\\frac{1}{N}\\sum_{i=1}^{N}J_i(\\theta)+\\lambda\\Omega(\\theta),", true],
    ["J(\\theta)=\\frac{1}{N}\\sum_{i=1}^{N}\\mathrm{Loss}(\\hat y_i,y_i)+\\frac{\\lambda}{2}\\|\\theta\\|_2^2", true],
    ["\\theta=(W^{(1)},b^{(1)},\\ldots,W^{(L)},b^{(L)})", false],
    ["z^{(l)}=W^{(l)}a^{(l-1)}+b^{(l)}", false],
    ["\\hat{y}_i=f(x_i;\\theta),\\quad \\theta=\\{W^{(l)},b^{(l)}\\}_{l=1}^{L}", true],
    ["\\delta^{(L)}=\\hat{y}-y,\\qquad \\delta^{(l)}=\\left(W^{(l+1)}\\right)^{\\top}\\delta^{(l+1)}\\odot\\phi'\\left(z^{(l)}\\right)", true],
    ["\\rho=\\frac{\\|g_{\\mathrm{num}}-g_{\\mathrm{bp}}\\|_2}{\\max(\\|g_{\\mathrm{num}}\\|_2,\\|g_{\\mathrm{bp}}\\|_2)}", true],
    ["m_t=\\beta_1 m_{t-1}+(1-\\beta_1)g_t,\\; \\hat m_t=\\frac{m_t}{1-\\beta_1^t}", true],
    ["\\operatorname{softmax}(z)_k=\\frac{e^{z_k}}{\\sum_{j=1}^{K} e^{z_j}}", false],
    ["\\lim_{n\\to\\infty} \\left(1+\\frac{1}{n}\\right)^n = e", true],
    ["\\mathbb{E}\\left[\\sum_{t=1}^{T}\\gamma^{t} r_t\\right]", true],
    ["\\begin{bmatrix} a_{11} & \\cdots & a_{1n} \\\\ \\vdots & \\ddots & \\vdots \\\\ a_{m1} & \\cdots & a_{mn} \\end{bmatrix}", true],
    ["x \\in \\{1,\\dots,K\\},\\ \\epsilon=10^{-5}", false],
    ["\\boxed{E=mc^2}", true],
    ["\\phantom{x} \\overbrace{a+b+c}^{3}", true],
  ];
  for (const [tex, display] of formulas) {
    const out = omml(tex, display);
    assert.ok(out?.startsWith("<m:oMath>") && out.endsWith("</m:oMath>"), tex);
    assertBalanced(out);
    assert.ok(!out.includes("undefined"), `${tex} 里混进了 undefined`);
  }
});
