import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import test from "node:test";
import { fileURLToPath, URL } from "node:url";
import { build } from "esbuild";
import { strToU8, zipSync } from "fflate";

// 抽取器跨多个 TS 模块并依赖 fflate，先打成一份 ESM 再动态导入；
// 这样测的就是浏览器里真正会跑的那份代码。
async function load(entry) {
  const bundle = await build({
    entryPoints: [fileURLToPath(new URL(entry, import.meta.url))],
    bundle: true,
    format: "esm",
    platform: "neutral",
    target: "es2022",
    write: false,
  });
  const source = Buffer.from(bundle.outputFiles[0].text).toString("base64");
  return import(`data:text/javascript;base64,${source}`);
}

const {
  extractDocxText,
  extractOpenDocumentText,
  extractPptxText,
  extractXlsxText,
  listArchiveEntries,
} = await load("./zip-documents.ts");
const { decodeEntities, parseAttributes, scanXml } = await load("./xml-scan.ts");

function pack(files) {
  return zipSync(Object.fromEntries(
    Object.entries(files).map(([name, content]) => [name, strToU8(content)]),
  ));
}

test("decodes named, decimal and hexadecimal XML entities", () => {
  assert.equal(decodeEntities("a &amp; b &lt;c&gt; &#65;&#x4e2d;"), "a & b <c> A中");
  assert.equal(decodeEntities("&unknown; &#x110000;"), "&unknown; &#x110000;");
});

test("parses attributes containing angle brackets and single quotes", () => {
  const attributes = parseAttributes(`r="B2" t='s' note="1 &gt; 0"`);
  assert.equal(attributes.get("r"), "B2");
  assert.equal(attributes.get("t"), "s");
  assert.equal(attributes.get("note"), "1 > 0");
});

test("skips comments, declarations and CDATA payloads", () => {
  const seen = [];
  scanXml(`<?xml version="1.0"?><!-- x --><a><![CDATA[<raw>]]>tail</a>`, {
    onOpen: name => seen.push(`open:${name}`),
    onText: text => seen.push(`text:${text}`),
    onClose: name => seen.push(`close:${name}`),
  });
  assert.deepEqual(seen, ["open:a", "text:<raw>", "text:tail", "close:a"]);
});

test("extracts Word paragraphs, tabs and table cells in document order", () => {
  const document = `<?xml version="1.0"?>
    <w:document xmlns:w="x"><w:body>
      <w:p><w:r><w:t>共享单车调度</w:t></w:r><w:r><w:tab/><w:t>2026</w:t></w:r></w:p>
      <w:p/>
      <w:tbl><w:tr><w:tc><w:p><w:t>站点</w:t></w:p></w:tc><w:tc><w:p><w:t>需求</w:t></w:p></w:tc></w:tr></w:tbl>
      <w:p><w:r><w:t xml:space="preserve">R&amp;D </w:t></w:r><w:r><w:t>投入</w:t></w:r></w:p>
    </w:body></w:document>`;
  const result = extractDocxText(pack({ "word/document.xml": document }));
  assert.deepEqual(result.text.split("\n"), ["共享单车调度\t2026", "站点", "需求", "R&D 投入"]);
  assert.equal(result.segments, 4);
});

test("ignores tab stops and run properties declared in paragraph settings", () => {
  const document = `<w:document xmlns:w="x"><w:body>
      <w:p>
        <w:pPr><w:rPr/><w:tabs><w:tab w:val="left" w:pos="420"/></w:tabs></w:pPr>
        <w:r><w:t>摘要</w:t></w:r>
      </w:p>
      <w:p><w:r><w:t>正文</w:t></w:r></w:p>
    </w:body></w:document>`;
  // w:pPr 里的 w:tab 是制表位设置，不是正文制表符；自闭合的 w:rPr 也不能提前
  // 抵消掉外层 w:pPr 的跳过计数。
  assert.deepEqual(extractDocxText(pack({ "word/document.xml": document })).text.split("\n"), [
    "摘要",
    "正文",
  ]);
});

test("rejects a zip that is not a Word document", () => {
  assert.throws(() => extractDocxText(pack({ "mimetype": "nope" })), /word\/document\.xml/);
});

test("orders slides numerically and folds speaker notes under each page", () => {
  const slide = body => `<p:sld xmlns:a="x"><p:cSld>${body}</p:cSld></p:sld>`;
  const result = extractPptxText(pack({
    "ppt/slides/slide2.xml": slide("<a:p><a:r><a:t>第二页</a:t></a:r></a:p>"),
    "ppt/slides/slide10.xml": slide("<a:p><a:t>第十页</a:t></a:p>"),
    "ppt/slides/slide1.xml": slide("<a:p><a:t>标题页</a:t></a:p>"),
    "ppt/notesSlides/notesSlide1.xml": slide("<a:p><a:t>开场白</a:t></a:p>"),
  }));
  assert.equal(result.segments, 3);
  assert.deepEqual(result.text.split("\n\n"), [
    "# 第 1 页\n标题页\n备注：开场白",
    "# 第 2 页\n第二页",
    "# 第 3 页\n第十页",
  ]);
});

test("resolves Excel sheet order through workbook relationships", () => {
  const workbook = `<workbook xmlns:r="x"><sheets>
      <sheet name="需求" sheetId="3" r:id="rId7"/>
      <sheet name="站点" sheetId="1" r:id="rId4"/>
    </sheets></workbook>`;
  const rels = `<Relationships>
      <Relationship Id="rId7" Target="worksheets/sheet3.xml"/>
      <Relationship Id="rId4" Target="/xl/worksheets/sheet1.xml"/>
    </Relationships>`;
  const shared = `<sst><si><t>站点</t></si><si><r><t>需</t></r><r><t>求</t></r></si></sst>`;
  const result = extractXlsxText(pack({
    "xl/workbook.xml": workbook,
    "xl/_rels/workbook.xml.rels": rels,
    "xl/sharedStrings.xml": shared,
    "xl/worksheets/sheet3.xml": `<worksheet><sheetData>
        <row r="1"><c r="A1" t="s"><v>1</v></c><c r="C1"><v>42</v></c></row>
      </sheetData></worksheet>`,
    "xl/worksheets/sheet1.xml": `<worksheet><sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c></row>
        <row r="2"><c r="A2" t="inlineStr"><is><t>东站</t></is></c><c r="B2"><v>3.5</v></c></row>
      </sheetData></worksheet>`,
    "xl/styles.xml": "<styleSheet/>",
  }));
  assert.equal(result.segments, 2);
  assert.deepEqual(result.text.split("\n"), [
    "# 工作表：需求（1 行）",
    // C1 前面缺席的 B1 要补成空列，否则 42 会串到 B 列上。
    "需求\t\t42",
    "",
    "# 工作表：站点（2 行）",
    "站点",
    "东站\t3.5",
  ]);
});

test("treats a self-closing empty cell as exactly one column", () => {
  const result = extractXlsxText(pack({
    "xl/workbook.xml": `<workbook><sheets><sheet name="表" sheetId="1"/></sheets></workbook>`,
    "xl/worksheets/sheet1.xml": `<worksheet><sheetData>
        <row r="1"><c r="A1"><v>1</v></c><c r="B1"/><c r="C1"><v>3</v></c></row>
      </sheetData></worksheet>`,
  }));
  // 自闭合的 <c/> 只能产生一个空列，否则 3 会被挤到 D 列去。
  assert.deepEqual(result.text.split("\n"), ["# 工作表：表（1 行）", "1\t\t3"]);
});

test("keeps OpenDocument table rows on one line", () => {
  const content = `<office:document-content xmlns:text="x"><office:body><office:text>
      <text:h>摘要</text:h>
      <text:p>第一段<text:tab/>缩进</text:p>
      <table:table-row><table:table-cell><text:p>甲</text:p></table:table-cell>
        <table:table-cell><text:p>乙</text:p></table:table-cell></table:table-row>
    </office:text></office:body></office:document-content>`;
  const result = extractOpenDocumentText(pack({ "content.xml": content }));
  assert.deepEqual(result.text.split("\n"), ["摘要", "第一段\t缩进", "甲\t乙"]);
});

test("lists archive entries without decompressing them", () => {
  const entries = listArchiveEntries(pack({
    "附件/站点.csv": "id,name\n1,东站\n",
    "附件/说明.txt": "读我",
  }));
  assert.deepEqual(entries.map(entry => entry.name).sort(), ["附件/站点.csv", "附件/说明.txt"]);
  assert.equal(entries.find(entry => entry.name.endsWith(".csv")).size, 17);
});
