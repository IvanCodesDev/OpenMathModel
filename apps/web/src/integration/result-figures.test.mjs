import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const compilerOptions = { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 };
const transpile = async relative => ts.transpileModule(
  await readFile(new URL(relative, import.meta.url), "utf8"), { compilerOptions },
).outputText;
const dataUrl = code => `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`;
// result-figures.ts 运行时依赖三个纯函数模块（它们只有 type import）：先编成 data: 模块再改写 import 目标
const deps = {};
for (const name of ["paper-audit", "paper-figures", "paper-references"]) deps[name] = dataUrl(await transpile(`./${name}.ts`));
let source = await transpile("./result-figures.ts");
for (const [name, url] of Object.entries(deps)) source = source.replace(new RegExp(`from\\s+"\\./${name}"`, "g"), `from ${JSON.stringify(url)}`);
const { VERIFICATION_LABELS, describePaperPackage, describeResultFigures, referenceItems } = await import(dataUrl(source));

const fixture = async name => JSON.parse(
  await readFile(new URL(`../../../../packages/contracts/fixtures/v1/valid/${name}`, import.meta.url), "utf8"),
);
/** experiment-summary.5：三张图（实验两张，其一无产物 id；检验一张）。 */
const summaryWithFigures = await fixture("experiment-summary.5.json");
/** experiment-summary.3：figures 字段出现之前的形状。 */
const summaryLegacy = await fixture("experiment-summary.3.json");
/** document-draft.4：四张图含 PAPER_WRITING；document-draft.5：引用库。 */
const draftFigures = await fixture("document-draft.4.json");
const draftReferences = await fixture("document-draft.5.json");

test("describeResultFigures: experiment + validation figures → ordered cards, image only with an artifact id", () => {
  const view = describeResultFigures(summaryWithFigures);
  assert.equal(view.kind, "figures");
  assert.deepEqual([view.total, view.withImage], [3, 2]);
  assert.deepEqual(view.stages, ["实验运行", "结果验证"]);
  assert.deepEqual(view.cards.map(card => [card.label, card.name, card.caption, card.stage, card.imageUrl, card.inserted]), [
    ["图 1", "fit_vs_baseline.png", "贪心解与随机基线的目标值对比", "实验运行", "/api/v1/artifacts/art_8f2a1c3d4e5b/download", null],
    ["图 2", "convergence.svg", "convergence.svg", "实验运行", null, null],
    ["图 3", "sensitivity.png", "需求率 ±20% 扰动下的 rmse 变化", "结果验证", "/api/v1/artifacts/art_9a3b2d4e5f6c/download", null],
  ]);
  // 乱序输入按编号排；未知阶段原样透出
  const shuffled = describeResultFigures({
    ...summaryWithFigures,
    figures: [
      { number: 2, name: "b.png", artifact_id: "art_b", caption: "", source_stage: "FUTURE_STAGE" },
      { number: 1, name: "a.png", artifact_id: null, caption: " 甲 ", source_stage: "EXPERIMENTING" },
    ],
  });
  assert.deepEqual(shuffled.cards.map(card => [card.number, card.caption, card.stage]), [[1, "甲", "实验运行"], [2, "b.png", "FUTURE_STAGE"]]);
});

test("describeResultFigures: empty list vs absent field", () => {
  assert.deepEqual(describeResultFigures({ ...summaryWithFigures, figures: [] }), { kind: "empty" });
  assert.deepEqual(describeResultFigures(summaryLegacy), { kind: "absent" });
  assert.deepEqual(describeResultFigures({ ...summaryLegacy, figures: null }), { kind: "absent" });
});

test("describePaperPackage: paper figures with inserted flags + verified references", () => {
  const withFigures = describePaperPackage(draftFigures);
  assert.deepEqual([withFigures.figures.total, withFigures.figures.inserted], [4, 3]);
  assert.deepEqual(withFigures.figures.cards.map(card => [card.label, card.stage, card.inserted, Boolean(card.imageUrl)]), [
    ["图 1", "实验运行", true, true],
    ["图 2", "实验运行", false, true],
    ["图 3", "结果验证", true, false],
    ["图 4", "论文撰写", true, true],
  ]);
  assert.equal(withFigures.references, null, "fixture 4 没有引用库 → null（不是空表）");

  const withReferences = describePaperPackage(draftReferences);
  assert.deepEqual([withReferences.references.total, withReferences.references.cited], [3, 1]);
  const items = withReferences.references.items;
  assert.deepEqual(items.map(item => [item.label, item.cited, item.source, item.verification, item.url !== null]), [
    ["[1]", true, "方案引用的先例", "来源可核", true],
    ["[2]", false, "用户提供", "按标题匹配", true],
    ["[3]", false, "方案引用的先例", null, false],
  ]);
  assert.equal(items[0].key, draftReferences.references[0].key);
  assert.equal(items[2].key, null, "老运行的条目没有 key");
  assert.equal(items[0].title, draftReferences.references[0].title);
});

test("describePaperPackage: absent vs empty lists; referenceItems sorts and guards urls", () => {
  const bare = describePaperPackage({ ...draftFigures, figures: undefined, references: undefined });
  assert.deepEqual(bare, { figures: null, references: null });
  const empty = describePaperPackage({ ...draftFigures, figures: [], references: [] });
  assert.deepEqual([empty.figures.total, empty.figures.inserted, empty.figures.cards, empty.references.total, empty.references.items], [0, 0, [], 0, []]);
  const items = referenceItems([
    { number: 2, title: "B", text: "B text", url: "javascript:alert(1)", source: "plan_citation", card_id: null, cited: false, key: "b", verification: "weird" },
    { number: 1, title: "A", text: "A text", url: "https://example.org/a", source: "unknown_source", card_id: "c", cited: true },
  ]);
  assert.deepEqual(items.map(item => [item.number, item.url, item.source, item.verification, item.key]), [
    [1, "https://example.org/a", "unknown_source", null, null],
    [2, null, "方案引用的先例", "weird", "b"],
  ]);
  assert.equal(VERIFICATION_LABELS.unverified, "未验证");
});
