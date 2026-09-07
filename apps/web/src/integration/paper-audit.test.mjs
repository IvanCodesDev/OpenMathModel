import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-audit.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { describePaperAudit, paperAuditStamp, summarizeFindingKinds, FINDING_KIND_REASONS, FROZEN_STAGE_LABELS } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture document-draft.2 的两个字段：六条冻结（四类来源）+ 第 6 章一处无出处数值。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/document-draft.2.json", import.meta.url),
    "utf8",
  ),
);
/** 契约 fixture document-draft.3：审计链四种 kind 各至少一条（数值 1 / 图 1 / 表 1 / 引用 2）。 */
const chainFixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/document-draft.3.json", import.meta.url),
    "utf8",
  ),
);

test("findings: rows keep values verbatim, stage label + path form the source, findings verbatim", () => {
  const section = describePaperAudit(fixture);
  assert.equal(section.kind, "findings");
  assert.equal(section.rows.length, 6);
  assert.deepEqual(section.rows[0], {
    id: "metrics.rmse",
    value: "0.12",
    label: "实验指标 rmse",
    stage: "实验运行",
    path: "metrics.rmse",
  });
  // 整数与小数都原样（不做千分位 / 截小数，与正文被审计的 token 同口径）
  assert.equal(section.rows.find(row => row.id === "cleaning.rows_before").value, "1200");
  assert.equal(section.rows.find(row => row.id === "robustness.bootstrap.threshold").value, "0.2");
  assert.deepEqual(
    section.rows.map(row => row.stage),
    ["实验运行", "结果验证", "结果验证", "数据准备", "数据准备", "建模方案"],
  );
  // fixture 只有三章，第 3 章标题是「6 结果分析与检验」：scope 按章序号计，标题原样
  assert.deepEqual(section.findings, [
    {
      scope: "第3章《6 结果分析与检验》",
      kind: "unsourced_number",
      numbers: ["0.87"],
      detail: "第3章《6 结果分析与检验》有 1 个数值不在冻结清单与材料中（0.87）",
    },
  ]);
});

test("clean: audited with zero findings", () => {
  const section = describePaperAudit({ frozen_numbers: fixture.frozen_numbers, audit_findings: [] });
  assert.equal(section.kind, "clean");
  assert.equal(section.rows.length, 6);
});

test("unaudited: list present but no audit → no verdict", () => {
  const section = describePaperAudit({ frozen_numbers: fixture.frozen_numbers.slice(0, 2), audit_findings: null });
  assert.deepEqual(Object.keys(section).sort(), ["kind", "rows"]);
  assert.equal(section.kind, "unaudited");
  assert.equal(section.rows.length, 2);
});

test("absent: both fields null / missing (runs before H5, sim nodes) → render nothing", () => {
  assert.deepEqual(describePaperAudit({ frozen_numbers: null, audit_findings: null }), { kind: "absent" });
  assert.deepEqual(describePaperAudit({}), { kind: "absent" });
});

test("empty frozen list still audits: clean with no rows (text may only cite material numbers)", () => {
  assert.deepEqual(describePaperAudit({ frozen_numbers: [], audit_findings: [] }), { kind: "clean", rows: [] });
});

test("label falls back to id; unknown stage passes through verbatim", () => {
  const section = describePaperAudit({
    frozen_numbers: [{ id: "x.y", label: "", value: 3.5, source_stage: "SOMETHING_NEW", source_path: "x.y" }],
    audit_findings: [],
  });
  assert.deepEqual(section.rows, [{ id: "x.y", value: "3.5", label: "x.y", stage: "SOMETHING_NEW", path: "x.y" }]);
  assert.equal(Object.keys(FROZEN_STAGE_LABELS).length, 4);
});

test("audit chain fixture: findings of all kinds pass through verbatim, tokens stay as chips", () => {
  const section = describePaperAudit(chainFixture);
  assert.equal(section.kind, "findings");
  assert.deepEqual(
    section.findings.map(f => f.kind),
    ["unsourced_number", "phantom_figure", "phantom_table", "unverified_citation", "unverified_citation"],
  );
  assert.deepEqual(section.findings[1].numbers, ["图 1", "图 2", "fit.png"]);
  assert.deepEqual(section.findings[3].numbers, ["[3]", "[5-7]"]);
  // 每个契约 kind 都有一行原因（中文源串，调用方 t()）；未知 kind 查不到 → 调用方退回 detail
  for (const finding of section.findings) assert.equal(typeof FINDING_KIND_REASONS[finding.kind], "string");
  assert.equal(FINDING_KIND_REASONS.future_kind, undefined);
});

test("summarizeFindingKinds: numbers → figures/tables → citations → other; figure and table share one bucket", () => {
  assert.deepEqual(summarizeFindingKinds(chainFixture.audit_findings), [
    { label: "处无出处数值", count: 1 },
    { label: "处图表引用不实", count: 2 },
    { label: "处引用未经验证", count: 2 },
  ]);
  // 只有数值发现：与首版「N 处无出处数值」同一短语
  assert.deepEqual(summarizeFindingKinds(fixture.audit_findings), [{ label: "处无出处数值", count: 1 }]);
  // 顺序按类型固定，不按出现顺序；未知 kind 归「其他」
  assert.deepEqual(
    summarizeFindingKinds([{ kind: "future_kind" }, { kind: "unverified_citation" }, { kind: "phantom_table" }]),
    [
      { label: "处图表引用不实", count: 1 },
      { label: "处引用未经验证", count: 1 },
      { label: "处其他发现", count: 1 },
    ],
  );
  assert.deepEqual(summarizeFindingKinds([]), []);
});

test("stamp identifies one draft version (idempotent render under SSE refresh)", () => {
  assert.equal(
    paperAuditStamp({ run_id: fixture.run_id, version: 2, updated_at: fixture.updated_at }),
    `${fixture.run_id}:2:${fixture.updated_at}`,
  );
});
