import assert from "node:assert/strict";
import test from "node:test";
import { strFromU8, unzipSync } from "fflate";
import katex from "katex";
import { documentUrl, imagesUrl, math, sampleDocument, text, transpiled } from "./paper-export-fixture.mjs";

const mathmlUrl = await transpiled("../text/mathml-omml.ts");
const { DOCX_MEDIA_TYPE, buildDocx, tableColumnWidths, xmlText } = await import(await transpiled("./paper-docx.ts", {
  fflate: import.meta.resolve("fflate"),
  "../text/mathml-omml": mathmlUrl,
  "./paper-document": documentUrl,
  "./paper-export-images": imagesUrl,
}));

const texToMathml = (tex, display) => katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: false });
const unpack = bytes => Object.fromEntries(Object.entries(unzipSync(bytes)).map(([name, data]) => [name, name.startsWith("word/media/") ? data : strFromU8(data)]));
const build = (doc = sampleDocument(), options = {}) => unpack(buildDocx(doc, { texToMathml, now: new Date("2026-10-02T15:00:00Z"), ...options }));
const BREAK_RUN = '<w:r><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>';
const plainRuns = pieces => pieces.map(piece => `<w:r><w:t xml:space="preserve">${piece}</w:t></w:r>`).join(BREAK_RUN);

function assertBalanced(xml, label) {
  const stack = [];
  for (const [, closing, name, selfClosing] of xml.replace(/<\?xml[^>]*\?>/, "").matchAll(/<(\/?)([\w:]+)[^>]*?(\/?)>/g)) {
    if (selfClosing) continue;
    if (closing) assert.equal(stack.pop(), name, `${label}: </${name}> 不匹配`);
    else stack.push(name);
  }
  assert.deepEqual(stack, [], `${label}: 有未闭合的标签`);
}

test("package: all OOXML parts present, well formed, image registered in content types and relationships", () => {
  const files = build();
  assert.equal(DOCX_MEDIA_TYPE, "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  for (const part of ["[Content_Types].xml", "_rels/.rels", "docProps/core.xml", "docProps/app.xml", "word/document.xml", "word/_rels/document.xml.rels", "word/styles.xml", "word/settings.xml", "word/footer1.xml"]) {
    assert.ok(typeof files[part] === "string", `缺少 ${part}`);
    assertBalanced(files[part], part);
  }
  assert.ok(files["word/media/image1.png"] instanceof Uint8Array, "图片部件");
  assert.ok(files["[Content_Types].xml"].includes('<Default Extension="png" ContentType="image/png"/>'));
  const rels = files["word/_rels/document.xml.rels"];
  const imageRel = /<Relationship Id="(rId\d+)" Type="[^"]+\/image" Target="media\/image1\.png"\/>/.exec(rels);
  assert.ok(imageRel, "图片关系");
  assert.ok(files["word/document.xml"].includes(`<a:blip r:embed="${imageRel[1]}"/>`), "正文引用的就是这张图");
  assert.ok(rels.includes('Target="https://example.org/a?b=1#c" TargetMode="External"/>'), "外链");
  assert.ok(files["docProps/core.xml"].includes("<dc:title>从零实现反向传播：手写 MLP 的推导与检验</dc:title>"));
  assert.ok(files["docProps/core.xml"].includes(">2026-10-02T15:00:00Z</dcterms:created>"));
});

test("styles: A4 body text (宋体 / Times New Roman 小四, 1.5 lines, 2-char indent) and outline-level headings", () => {
  const { "word/styles.xml": styles, "word/settings.xml": settings, "word/document.xml": document } = build();
  assert.ok(styles.includes('<w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>'));
  assert.ok(styles.includes('<w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="auto"/><w:ind w:firstLine="480" w:firstLineChars="200"/><w:jc w:val="both"/>'));
  assert.ok(styles.includes('<w:pPr><w:widowControl/><w:snapToGrid w:val="0"/>'), "正文段落属性元素顺序按 schema");
  assert.ok(!styles.includes("w:wordWrap"), "不开「允许西文在单词中间换行」：单词、数字不会在行尾任意字母处被劈开");
  assert.match(styles, /<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"\/>.*?<w:outlineLvl w:val="0"\/>/);
  assert.ok(styles.includes('w:eastAsia="黑体"'), "标题黑体");
  assert.ok(settings.includes('<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'), "不进兼容模式");
  assert.ok(document.includes('<w:pgSz w:w="11906" w:h="16838"/>'), "A4");
  assert.ok(document.includes('<w:footerReference w:type="default" r:id="rId3"/>'));
});

test("body: title, abstract heading, keywords, headings, continued paragraph without indent", () => {
  const document = build()["word/document.xml"];
  assert.ok(document.includes('<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t xml:space="preserve">从零实现反向传播：手写 MLP 的推导与检验</w:t></w:r></w:p>'));
  assert.ok(document.includes('<w:pStyle w:val="AbstractHeading"/></w:pPr><w:r><w:t xml:space="preserve">摘要</w:t>'));
  assert.ok(document.includes('<w:pStyle w:val="BodyNoIndent"/></w:pPr><w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t xml:space="preserve">关键词：</w:t>'));
  assert.ok(document.includes('<w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t xml:space="preserve">1 问题重述</w:t>'));
  assert.ok(document.includes('<w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t xml:space="preserve">1.1 子问题</w:t>'));
  assert.match(document, /<w:pStyle w:val="BodyNoIndent"\/><\/w:pPr><w:r><w:t xml:space="preserve">其中 <\/w:t><\/w:r><m:oMath>.*?<\/m:oMath><w:r><w:t xml:space="preserve"> 为交叉熵，100% 的 R&amp;D_x 成本 #1。<\/w:t><\/w:r><\/w:p>/, "公式后的续段：不缩进、行内公式是 m:oMath");
  assert.ok(document.includes(plainRuns(["梯度检验 metrics.", "grad_", "check_", "max_", "abs_", "err=", "5.9e\u201111，结论可信。"])), "长串在分隔符后插 1 pt 空格断点");
  assert.ok(!document.includes("\u200b"), "不插零宽空格：WPS 把它排成可拉伸的空格，字母会一个个拆开");
  assert.ok(!document.includes("\u0001"), "控制字符不进 XML");
  const pasted = build({ title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [{ kind: "text", text: "data\u200b_source", marks: {} }] }] })["word/document.xml"];
  assert.ok(!pasted.includes("\u200b") && pasted.includes(plainRuns(["data_", "source"])), "正文里原有的零宽空格也去掉，按同一套规则断");
});

test("Latin runs of 11+ characters break after separators via a 1 pt space; words, numbers and a minus sign with its digits stay whole; table cells stay as typed", () => {
  const body = value => {
    const document = build({ title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [text(value)] }] })["word/document.xml"];
    return /<w:pStyle w:val="Normal"\/>(?:<w:jc w:val="left"\/>)?<\/w:pPr>(.*?)<\/w:p>/.exec(document)[1];
  };
  assert.equal(body("基线 F_A_baseline_asap_local=0.665487 改善"), plainRuns(["基线 F_A_baseline_", "asap_", "local=", "0.665487 改善"]),
    "断点前后各留 2 个字符：F_A_ 不拆");
  assert.equal(body("robustness.rolling_domination=0.0021355180250620664"), plainRuns(["robustness.", "rolling_", "domination=", "0.0021355180250620664"]),
    "字母前的 . 可断，长数字内部不断");
  assert.equal(body("收益 5,087,342,520 美元，时刻 2376:2399:2400 不拆"), plainRuns(["收益 5,087,342,520 美元，时刻 2376:2399:2400 不拆"]),
    "后面是数字的逗号、冒号不断");
  assert.equal(body("alpha,beta,gamma,delta"), plainRuns(["alpha,", "beta,", "gamma,", "delta"]));
  assert.equal(body("MILP 与 x=-5、第 2376-2399 小时"), plainRuns(["MILP 与 x=\u20115、第 2376-2399 小时"]),
    "短串不插断点；负号换成不断行连字符，数字区间的连字符照旧");
  const minus = body("增益 renew_util_storage_gain_pct=-1.431863。");
  assert.equal(minus, plainRuns(["增益 renew_", "util_", "storage_", "gain_", "pct=", "\u20111.431863。"]), "负号与数字不在行尾拆开");
  assert.ok(!minus.includes("noBreakHyphen"), "不用 w:noBreakHyphen：WPS 在它后面多排约 1.7 pt");

  const tabled = build({ title: "t", images: [], blocks: [{
    kind: "table",
    caption: [text("表 8 renew_util_storage_gain_pct 等指标")],
    rows: [
      [{ inlines: [text("指标")], header: true }, { inlines: [text("数值")], header: true }],
      [{ inlines: [text("renew_util_storage_gain_pct")] }, { inlines: [text("-1.431863")] }],
    ],
  }] })["word/document.xml"];
  const [caption, cells] = tabled.split("<w:tbl>");
  assert.ok(caption.includes(plainRuns(["表 8 renew_", "util_", "storage_", "gain_", "pct 等指标"])), "表题照正文处理");
  assert.ok(cells.includes(">renew_util_storage_gain_pct</w:t>") && cells.includes(">-1.431863</w:t>") && !cells.includes(BREAK_RUN),
    "表格单元格不插断点、负号照旧：复制出去能原样用");
});

test("paragraphs, list items and quotes holding a Latin run of about 11+ characters are left-aligned instead of justified", () => {
  const paragraph = (inlines, extra = {}) => ({ kind: "paragraph", inlines, ...extra });
  const doc = {
    title: "t",
    images: [],
    blocks: [
      paragraph([text("成本由 total_cost_baseline_asap_yuan=1248491.767975 降至 1072066.31715。")]),
      paragraph([text("MILP、Gurobi、Momentum 与 0.559724 都不到 11 个字符。")]),
      paragraph([text("公式 "), math("x_{i,t}^{\\mathrm{GPU}}+y_{i,t}^{\\mathrm{GPU}}+z_{i,t}^{\\mathrm{GPU}}"), text(" 不算长串。")]),
      paragraph([text("synth_"), text("circles", { bold: true }), text(" 跨 run 也是一整段。")]),
      paragraph([text("F_A_baseline_random_feasible=0.665597")], { align: "center" }),
      { kind: "list", ordered: false, items: [[text("F_A_baseline_random_feasible=0.665597")], [text("随机基线")]] },
      { kind: "quote", inlines: [text("metrics.grad_check_max_abs_err=5.9e-11")] },
    ],
  };
  const paragraphs = [...build(doc)["word/document.xml"].matchAll(/<w:p><w:pPr>(.*?)<\/w:pPr>/g)].map(match => match[1]);
  assert.deepEqual(paragraphs, [
    '<w:pStyle w:val="Title"/>',
    '<w:pStyle w:val="Normal"/><w:jc w:val="left"/>',
    '<w:pStyle w:val="Normal"/>',
    '<w:pStyle w:val="Normal"/>',
    '<w:pStyle w:val="Normal"/><w:jc w:val="left"/>',
    '<w:pStyle w:val="Normal"/><w:ind w:firstLine="0" w:firstLineChars="0"/><w:jc w:val="center"/>',
    '<w:pStyle w:val="ListItem"/><w:jc w:val="left"/>',
    '<w:pStyle w:val="ListItem"/>',
    '<w:pStyle w:val="Quote"/><w:jc w:val="left"/>',
  ]);
});

test("display math is a native equation paragraph; a formula KaTeX cannot parse falls back to source text", () => {
  const document = build()["word/document.xml"];
  assert.match(document, /<w:p><w:pPr><w:pStyle w:val="Equation"\/><\/w:pPr><m:oMathPara><m:oMathParaPr><m:jc m:val="center"\/><\/m:oMathParaPr><m:oMath><m:limLow>/);
  const broken = build({ title: "t", images: [], blocks: [{ kind: "math", tex: "\\frac{1}{" }] })["word/document.xml"];
  assert.ok(broken.includes('<w:pStyle w:val="Equation"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="Consolas"'), "解析失败的公式退回等宽源码");
  assert.ok(broken.includes("\\frac{1}{</w:t>"));
  const throwing = build({ title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [{ kind: "math", tex: "x" }] }] }, { texToMathml: () => { throw new Error("boom"); } });
  assert.ok(throwing["word/document.xml"].includes(">x</w:t>"), "转换器抛错也不丢内容");
});

test("three-line table with repeating header, caption above; figure scaled into the text block with caption below", () => {
  const document = build()["word/document.xml"];
  assert.ok(document.includes('<w:pStyle w:val="TableCaption"/></w:pPr><w:r><w:t xml:space="preserve">表 1 全文符号说明</w:t></w:r></w:p><w:tbl>'));
  assert.ok(document.includes('<w:tblBorders><w:top w:val="single" w:sz="12" w:space="0" w:color="000000"/><w:left w:val="nil"/><w:bottom w:val="single" w:sz="12" w:space="0" w:color="000000"/>'));
  assert.ok(document.includes("<w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>"), "表头跨页重复");
  assert.ok(document.includes('<w:tcBorders><w:bottom w:val="single" w:sz="6" w:space="0" w:color="000000"/></w:tcBorders>'), "表头下细线");
  assert.ok(document.includes('<w:pStyle w:val="TableText"/><w:jc w:val="left"/>'), "长文本列左对齐");
  const extent = /<wp:extent cx="(\d+)" cy="(\d+)"\/>/.exec(document);
  const [cx, cy] = [Number(extent[1]), Number(extent[2])];
  assert.ok(cx <= Math.round(9070 * 635 * 0.82) && cx > 4_000_000, `图宽缩进版心：${cx}`);
  assert.ok(Math.abs(cy / cx - 800 / 1200) < 0.01, "等比缩放");
  assert.match(document, /<w:pStyle w:val="Figure"\/><\/w:pPr><w:r><w:drawing>.*?<\/w:drawing><\/w:r><\/w:p><w:p><w:pPr><w:pStyle w:val="Caption"\/><\/w:pPr><w:r><w:t xml:space="preserve">图 1 训练收敛曲线<\/w:t>/);
  assert.ok(document.includes("［插图：图 2（图片未能随导出带走）］"), "缺图明说");
});

test("table column widths: no column starves below 4 characters, identifiers and short headers stay whole when room allows", () => {
  const TEXT_WIDTH = 9070;
  const twips = chars => Math.round(chars * 210 + 360);
  const cell = (value, header = false) => ({ inlines: [{ kind: "text", text: value, marks: {} }], header });
  const sum = values => values.reduce((total, value) => total + value, 0);

  const narrow = tableColumnWidths([[cell("符号", true), cell("含义", true)], [cell("θ"), cell("损失")]], 2, 1);
  assert.deepEqual(narrow, { widths: [twips(2), twips(2)], wrap: [false, false] }, "排得下就用自然宽度");

  const assumptions = tableColumnWidths([
    [cell("假设", true), cell("依据", true), cell("状态与影响", true)],
    [cell("各时段负荷曲线按历史同期均值外推，节假日不单独建模，极端天气不在本模型考虑范围内"), cell("题面附件一给出近三年逐时负荷，均值外推误差在可接受范围内"), cell("已满足；若改用日前预测曲线需重跑第三问")],
  ], 3, 1);
  assert.ok(sum(assumptions.widths) <= TEXT_WIDTH, "不超版心");
  assert.ok(assumptions.widths.every(width => width >= twips(4)), `每列至少 4 个字：${assumptions.widths}`);
  assert.ok(assumptions.widths[2] >= twips(5), "「状态与影响」表头一行放得下");
  assert.deepEqual(assumptions.wrap, [true, true, true]);

  const metrics = tableColumnWidths([
    [cell("指标", true), cell("数值", true), cell("说明", true)],
    [cell("milp_binary_vars_core"), cell("1234"), cell("核心 MILP 模型的二进制变量个数，决定分支定界的规模与求解时间，随时段数线性增长")],
  ], 3, 1);
  assert.deepEqual(metrics.wrap, [false, false, true], "标识符列按自然宽度整串放下、不在格边硬断，只有长说明列折行");

  const scores = tableColumnWidths([
    ["数据配置", "MLP Adam", "逻辑回归", "kNN(k=5)", "多数类", "相对逻辑回归增益(pp)", "相对kNN(k=5)增益(pp)"].map(value => cell(value, true)),
    ["synth_circles", "0.93222222", "0.52555556", "0.61666667", "0.5", "40.666666", "31.555555"].map(value => cell(value)),
  ], 7, 1);
  assert.ok(sum(scores.widths) <= TEXT_WIDTH);
  assert.ok(scores.widths.every((width, column) => column === 0 || width < twips(5.5)), "长表头把数字列压窄");
  assert.deepEqual(scores.wrap, Array(7).fill(false), "数字列表头折行、正文仍居中，不因列被压窄就改左对齐");

  const crowded = tableColumnWidths([
    [cell("milp_binary_vars_core"), cell("milp_binary_vars_aux"), cell("状态与影响"), cell("状态与影响"), cell("状态与影响"), cell("状态与影响"), cell("各时段负荷曲线按历史同期均值外推，节假日不单独建模")],
  ], 7, 0);
  assert.ok(sum(crowded.widths) <= TEXT_WIDTH);
  assert.ok(crowded.widths.every(width => width >= twips(4)), `标识符放不下时也先保证每列 4 个字：${crowded.widths}`);

  const tooMany = tableColumnWidths([Array.from({ length: 10 }, () => cell("milp_binary_vars_core"))], 10, 0);
  assert.ok(sum(tooMany.widths) <= TEXT_WIDTH, "列数多到每列 4 个字都放不下时按比例压，仍不超版心");
});

test("lists, code, quote and hyperlinks", () => {
  const document = build()["word/document.xml"];
  assert.ok(document.includes('<w:pStyle w:val="ListItem"/></w:pPr><w:r><w:t xml:space="preserve">1.</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t xml:space="preserve">前向传播</w:t>'));
  assert.ok(document.includes('<w:pStyle w:val="Code"/></w:pPr><w:r><w:t xml:space="preserve"></w:t><w:tab/><w:t xml:space="preserve">return x  # 注释</w:t></w:r>'), "Tab 保留");
  assert.ok(document.includes('<w:pStyle w:val="Quote"/></w:pPr><w:r><w:t xml:space="preserve">引用一句</w:t>'));
  assert.match(document, /<w:hyperlink r:id="rId\d+" w:history="1"><w:r><w:rPr><w:color w:val="0563C1"\/><w:u w:val="single"\/><\/w:rPr><w:t xml:space="preserve">文档<\/w:t><\/w:r><\/w:hyperlink>/);
});

test("run properties follow the schema order Word insists on", () => {
  const doc = { title: "t", images: [], blocks: [{ kind: "paragraph", inlines: [{ kind: "text", text: "x", marks: { code: true, bold: true, italic: true, strike: true, underline: true, sup: true } }] }] };
  const document = build(doc)["word/document.xml"];
  assert.ok(document.includes('<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:b/><w:bCs/><w:i/><w:iCs/><w:strike/><w:sz w:val="21"/><w:szCs w:val="21"/><w:u w:val="single"/><w:vertAlign w:val="superscript"/></w:rPr>'));
  assert.equal(xmlText('a<b>&"c"\u0002'), "a&lt;b&gt;&amp;&quot;c&quot;");
});
