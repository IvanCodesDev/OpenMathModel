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
const rewire = (code, deps) => Object.entries(deps).reduce(
  (source, [name, url]) => source.replace(new RegExp(`from\\s+"\\./${name}"`, "g"), `from ${JSON.stringify(url)}`),
  code,
);
// 依赖链：paper-audit / paper-figures / paper-references / delivery-record 只有 type import；
// result-figures 依赖前三者；approval-evidence 依赖 delivery-record / paper-audit / result-figures
const leaf = {};
for (const name of ["paper-audit", "paper-figures", "paper-references", "delivery-record"]) leaf[name] = dataUrl(await transpile(`./${name}.ts`));
const resultFigures = dataUrl(rewire(await transpile("./result-figures.ts"), leaf));
const source = rewire(await transpile("./approval-evidence.ts"), { ...leaf, "result-figures": resultFigures });
const { EVIDENCE_FIGURES_LIMIT, EVIDENCE_FINDINGS_LIMIT, approvalEvidenceStamp, describeApprovalEvidence } = await import(dataUrl(source));

const clone = value => JSON.parse(JSON.stringify(value));
const fixture = async name => JSON.parse(
  await readFile(new URL(`../../../../packages/contracts/fixtures/v1/valid/${name}`, import.meta.url), "utf8"),
);
const confirmed = await fixture("delivery-manifest.3.json");
const draftWithFindings = await fixture("document-draft.3.json");
const draftWithFigures = await fixture("document-draft.4.json");
const draftWithReferences = await fixture("document-draft.5.json");

/** G4 挂起：交付记录等待确认、两项检查不过。 */
function pendingManifest() {
  const manifest = clone(confirmed);
  manifest.delivery = {
    ...manifest.delivery,
    status: "pending_confirmation",
    confirmed_at: null,
    comment: null,
    checks: manifest.delivery.checks.map(check => check.id === "audit_clean"
      ? { ...check, passed: false, detail: "终稿审计发现 5 处" }
      : check.id === "metrics_in_paper" ? { ...check, passed: false, detail: "未出现：metrics.mae" } : check),
  };
  return manifest;
}

test("describeApprovalEvidence: only a pending G4 with an approval id yields evidence", () => {
  assert.equal(describeApprovalEvidence(draftWithFindings, confirmed), null, "已确认交付 → 没有门可挂");
  assert.equal(describeApprovalEvidence(draftWithFindings, null), null);
  const legacy = clone(confirmed);
  delete legacy.delivery;
  assert.equal(describeApprovalEvidence(draftWithFindings, legacy), null, "旧接口无交付记录");
  const noId = pendingManifest();
  noId.delivery.approval_id = null;
  assert.equal(describeApprovalEvidence(draftWithFindings, noId), null, "没有审批 id 无处可挂");
  const evidence = describeApprovalEvidence(draftWithFindings, pendingManifest());
  assert.equal(evidence.approvalId, confirmed.delivery.approval_id);
});

test("describeApprovalEvidence: audit kinds, top findings with reasons, checks failed list", () => {
  const evidence = describeApprovalEvidence(draftWithFindings, pendingManifest());
  assert.equal(evidence.audit.total, 5);
  assert.deepEqual(evidence.audit.kinds, [
    { label: "处无出处数值", count: 1 }, { label: "处图表引用不实", count: 2 }, { label: "处引用未经验证", count: 2 },
  ]);
  assert.equal(evidence.audit.top.length, EVIDENCE_FINDINGS_LIMIT);
  assert.equal(evidence.audit.more, 2);
  assert.deepEqual(evidence.audit.top.map(f => [f.scope, f.kind, f.numbers, f.reason]), [
    ["第3章《6 结果分析与检验》", "unsourced_number", ["0.87"], "不在冻结清单与材料中"],
    ["第2章《5 模型建立与求解》", "phantom_figure", ["图 1", "图 2", "fit.png"], "引用的图没有对应的真实图件"],
    ["第2章《5 模型建立与求解》", "phantom_table", ["表 1"], "引用的表在全文找不到带该编号表题的表格"],
  ]);
  assert.deepEqual(evidence.checks, {
    passed: 3, total: 5,
    failed: [
      { id: "audit_clean", label: "终稿审计 0 发现", detail: "终稿审计发现 5 处" },
      { id: "metrics_in_paper", label: "实验指标出现在论文里", detail: "未出现：metrics.mae" },
    ],
  });
  assert.equal(evidence.figures, null, "fixture 3 没有图件清单");
  assert.equal(evidence.references, null, "fixture 3 没有引用库");
});

test("describeApprovalEvidence: inserted figures first, previewable cards capped, references counted", () => {
  const evidence = describeApprovalEvidence(draftWithFigures, pendingManifest());
  assert.deepEqual([evidence.figures.total, evidence.figures.inserted, evidence.figures.previewable, evidence.figures.more], [4, 3, 3, 0]);
  // 已插入且有产物 id 的在前（图 1、图 4），未插入的图 2 其后；图 3 无产物 id 不做缩略图
  assert.deepEqual(evidence.figures.cards.map(card => [card.number, card.inserted, Boolean(card.imageUrl)]), [[1, true, true], [4, true, true], [2, false, true]]);
  assert.equal(evidence.audit.total, 0, "fixture 4 审计 0 发现");
  assert.deepEqual(evidence.audit.kinds, []);
  const refs = describeApprovalEvidence(draftWithReferences, pendingManifest());
  assert.deepEqual(refs.references, { total: 3, cited: 1 });
  // 超过上限的缩略图只计 more
  const many = clone(draftWithFigures);
  many.figures = Array.from({ length: 7 }, (_, i) => ({ number: i + 1, name: `f${i + 1}.png`, artifact_id: `art_${i + 1}`, caption: "", source_stage: "EXPERIMENTING", inserted: i % 2 === 0 }));
  const capped = describeApprovalEvidence(many, pendingManifest());
  assert.equal(capped.figures.cards.length, EVIDENCE_FIGURES_LIMIT);
  assert.equal(capped.figures.more, 7 - EVIDENCE_FIGURES_LIMIT);
  assert.deepEqual(capped.figures.cards.map(card => card.number), [1, 3, 5, 7], "已插入的四张先占满名额");
});

test("describeApprovalEvidence: draft absent → evidence still names the gate with checks only; stamp changes with versions", () => {
  const manifest = pendingManifest();
  const evidence = describeApprovalEvidence(null, manifest);
  assert.equal(evidence.audit, null);
  assert.equal(evidence.figures, null);
  assert.equal(evidence.references, null);
  assert.equal(evidence.checks.total, 5);
  const stamp = approvalEvidenceStamp(evidence, null, manifest);
  assert.equal(stamp, `${manifest.delivery.approval_id}:-:${manifest.updated_at}`);
  assert.notEqual(approvalEvidenceStamp(evidence, draftWithFindings, manifest), stamp);
});
