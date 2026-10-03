import assert from "node:assert/strict";
import test from "node:test";
import { strFromU8, unzipSync } from "fflate";
import katex from "katex";
import { documentUrl, imagesUrl, sampleDocument, transpiled } from "./paper-export-fixture.mjs";

const mathmlUrl = await transpiled("../text/mathml-omml.ts");
const { DOCX_MEDIA_TYPE, buildDocx, xmlText } = await import(await transpiled("./paper-docx.ts", {
  fflate: import.meta.resolve("fflate"),
  "../text/mathml-omml": mathmlUrl,
  "./paper-document": documentUrl,
  "./paper-export-images": imagesUrl,
}));

const texToMathml = (tex, display) => katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: false });
const unpack = bytes => Object.fromEntries(Object.entries(unzipSync(bytes)).map(([name, data]) => [name, name.startsWith("word/media/") ? data : strFromU8(data)]));
const build = (doc = sampleDocument(), options = {}) => unpack(buildDocx(doc, { texToMathml, now: new Date("2026-10-02T15:00:00Z"), ...options }));

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
  assert.ok(styles.includes('<w:pPr><w:widowControl/><w:wordWrap w:val="0"/><w:snapToGrid w:val="0"/>'), "正文允许西文在单词中间换行，元素顺序按 schema");
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
  assert.ok(document.includes("me|t|r|i|cs.|gr|ad_|ch|e|ck_".replaceAll("|", "\u200b")), "长串插零宽断点");
  assert.ok(!document.includes("\u0001"), "控制字符不进 XML");
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
