import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-references.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { referenceRows, safeHttpUrl, summarizeReferences, REFERENCE_SOURCE_LABELS } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture document-draft.5：三条文献（方案引用已引用 / 用户提供未引用 / 无链接无卡）。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/document-draft.5.json", import.meta.url),
    "utf8",
  ),
);

test("summary counts total and cited; absent or empty library yields null", () => {
  assert.deepEqual(summarizeReferences(fixture.references), { total: 3, cited: 1 });
  assert.equal(summarizeReferences(null), null);
  assert.equal(summarizeReferences(undefined), null);
  assert.equal(summarizeReferences([]), null);
});

test("rows keep contract text verbatim, label sources, and only pass http(s) links", () => {
  const rows = referenceRows(fixture.references);
  assert.deepEqual(rows.map(row => [row.label, row.source, row.cited, row.url]), [
    ["[1]", "方案引用的先例", true, "https://www.mcm.edu.cn/html_cn/node/2021c.html"],
    ["[2]", "用户提供", false, "https://github.com/Jackksonns/MCM-ICM-Outstanding-Papers/blob/d29267cb9e993419749e6111981b30a44183fdf8/2025/D/2504188.pdf"],
    ["[3]", "方案引用的先例", false, null],
  ]);
  assert.equal(rows[0].title, "生产企业原材料的订购与运输");
  assert.equal(rows[0].text, fixture.references[0].text, "条目正文原样");
  // enum 外的来源原样透出（消费者容忍未知取值）
  assert.equal(referenceRows([{ ...fixture.references[0], source: "future_source" }])[0].source, "future_source");
  assert.equal(REFERENCE_SOURCE_LABELS.plan_citation, "方案引用的先例");
});

test("safeHttpUrl refuses anything but explicit http(s)", () => {
  assert.equal(safeHttpUrl("https://example.org/a"), "https://example.org/a");
  assert.equal(safeHttpUrl("  http://example.org/b  "), "http://example.org/b");
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("ftp://example.org"), null);
  assert.equal(safeHttpUrl(""), null);
  assert.equal(safeHttpUrl(null), null);
  assert.equal(safeHttpUrl("https://exa mple.org"), null, "含空白不算 URL");
});
