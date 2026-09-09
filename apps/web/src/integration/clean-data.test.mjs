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
// clean-data.ts 运行时依赖 experiment-notes.ts（纯函数）与 result-figures.ts（缩略图卡；其依赖 paper-audit /
// paper-figures / paper-references 只有纯函数）：先把依赖编成 data: 模块再改写 import 目标
const leaf = {};
for (const name of ["experiment-notes", "paper-audit", "paper-figures", "paper-references"]) leaf[name] = dataUrl(await transpile(`./${name}.ts`));
const resultFigures = dataUrl(rewire(await transpile("./result-figures.ts"), leaf));
const cleanDataSource = rewire(await transpile("./clean-data.ts"), { ...leaf, "result-figures": resultFigures });
const {
  DECISION_LABELS, OUTPUT_ROLE_LABELS, describeCleanData, describeDataFigures, describeDecision, formatSize, outputRows, shortHash,
} = await import(dataUrl(cleanDataSource));

/** 契约 fixture dataset-profile.2：清洗执行、审稿僵持、四个产物（两表一脚本一图，一表不可下载）、G2 决策 use_raw、
 * 两张探索性图（一张有产物、一张没有）。 */
const fixture = JSON.parse(
  await readFile(new URL("../../../../packages/contracts/fixtures/v1/valid/dataset-profile.2.json", import.meta.url), "utf8"),
);
/** 契约 fixture dataset-profile.1：cleaning 字段出现之前的形状。 */
const legacy = JSON.parse(
  await readFile(new URL("../../../../packages/contracts/fixtures/v1/valid/dataset-profile.1.json", import.meta.url), "utf8"),
);

test("describeCleanData: executed cleaning → status, ratios, outputs split by role, decision", () => {
  const view = describeCleanData(fixture);
  assert.equal(view.kind, "executed");
  assert.equal(view.passed, true);
  assert.equal(view.statusLabel, "通过验收");
  assert.equal(view.attempts, 2);
  assert.deepEqual([view.rowsBefore, view.rowsAfter, view.rowsBeforeText, view.rowsAfterText], [1200, 1104, "1,200", "1,104"]);
  assert.equal(view.retainedRatio, "92.0%");
  assert.equal(view.deletedRatio, "8.0%");
  assert.equal(view.deletionOverThreshold, true, "8% > G2 阈值 5%");
  assert.deepEqual(view.imputed, ["demand"]);
  assert.deepEqual(view.imputedTargets, ["demand"]);
  assert.equal(view.summary, fixture.cleaning.summary);
  assert.equal(view.review.kind, "stalemate");
  assert.deepEqual(view.outputs.map(row => [row.name, row.role, row.roleLabel, row.size, row.hash, row.downloadUrl]), [
    ["orders.csv", "cleaned_data", "清洗后数据", "47.1 KB", "9b3f0c2a6d5e…", "/api/v1/artifacts/art_c1e2a3d4b5f60718/download"],
    ["stations.csv", "cleaned_data", "清洗后数据", "3.0 KB", "—", null],
    ["cleaning.py", "script", "清洗脚本", "2.0 KB", "1c2d3e4f5a6b…", "/api/v1/artifacts/art_c1e2a3d4b5f6071a/download"],
    ["missing_by_column.png", "figure", "探索性图件", "18.3 KB", "2d3e4f5a6b7c…", "/api/v1/artifacts/art_c1e2a3d4b5f6071b/download"],
  ]);
  assert.deepEqual(view.dataOutputs.map(row => row.name), ["orders.csv", "stations.csv"]);
  assert.equal(view.script.name, "cleaning.py");
  assert.deepEqual(view.decision, {
    optionId: "use_raw", label: "改用原始数据", actor: "user", comment: "审稿僵持，先用原始数据建模",
    resolvedAt: "2026-09-06T12:05:00.000000Z", usesCleaned: false,
  });
  // 探索性图件（s32）：编号是全文「图 N」，有产物 id 才有缩略图地址；没有说明退回文件名
  assert.deepEqual([view.figures.total, view.figures.withImage], [2, 1]);
  assert.deepEqual(view.figures.cards.map(card => [card.label, card.name, card.caption, card.stage, card.stageKey, card.imageUrl, card.inserted]), [
    ["图 1", "missing_by_column.png", "清洗前各列缺失比例", "数据准备", "DATA_PREPARATION", "/api/v1/artifacts/art_c1e2a3d4b5f6071b/download", null],
    ["图 2", "demand_before_after.svg", "demand_before_after.svg", "数据准备", "DATA_PREPARATION", null, null],
  ]);
});

test("describeDataFigures: absent field → null, empty → total 0, cards sorted by number", () => {
  assert.equal(describeDataFigures(undefined), null);
  assert.equal(describeDataFigures(null), null);
  assert.deepEqual(describeDataFigures([]), { total: 0, withImage: 0, cards: [] });
  const view = describeDataFigures([
    { number: 3, name: "b.png", artifact_id: null, caption: "", source_stage: "DATA_PREPARATION" },
    { number: 1, name: "a.png", artifact_id: "art_a", caption: "A", source_stage: "DATA_PREPARATION" },
  ]);
  assert.deepEqual(view.cards.map(card => card.label), ["图 1", "图 3"]);
  assert.deepEqual([view.total, view.withImage], [2, 1]);
});

test("describeCleanData: skipped cleaning keeps the reason; legacy / sim profiles are absent", () => {
  const skipped = describeCleanData({
    ...legacy,
    cleaning: {
      executed: false, status: null, reason: "工作区没有已下发的数据文件，无需清洗", attempts: 0,
      rows_before: 0, rows_after: 0, rows_deleted_ratio: 0, imputed_columns: [], imputed_target_columns: [],
      summary: "", review: null, outputs: [], decision: null,
    },
  });
  assert.deepEqual(skipped, { kind: "skipped", reason: "工作区没有已下发的数据文件，无需清洗" });
  assert.deepEqual(describeCleanData(legacy), { kind: "absent" });
  assert.deepEqual(describeCleanData({ ...legacy, cleaning: null }), { kind: "absent" });
});

test("describeCleanData: pre-s23 executed report (no outputs / decision keys) and zero rows", () => {
  const older = Object.fromEntries(Object.entries(fixture.cleaning).filter(([key]) => !["outputs", "decision", "figures"].includes(key)));
  const view = describeCleanData({ ...fixture, cleaning: { ...older, status: "failed", rows_before: 0, rows_after: 0, rows_deleted_ratio: 0 } });
  assert.equal(view.kind, "executed");
  assert.equal(view.passed, false);
  assert.equal(view.statusLabel, "未通过验收");
  assert.equal(view.retainedRatio, "—", "rows_before 为 0 不做除法");
  assert.equal(view.deletionOverThreshold, false);
  assert.deepEqual(view.outputs, []);
  assert.equal(view.script, null);
  assert.equal(view.decision, null);
  assert.equal(view.figures, null, "figures 字段出现之前的运行：不摆图件条");
});

test("helpers: sizes, hashes, unknown roles / options pass through, malformed outputs tolerated", () => {
  assert.equal(formatSize(0), "0 B");
  assert.equal(formatSize(1023), "1023 B");
  assert.equal(formatSize(48211), "47.1 KB");
  assert.equal(formatSize(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatSize(250 * 1024 * 1024), "250 MB");
  assert.equal(formatSize(null), "—");
  assert.equal(formatSize(-1), "—");
  assert.equal(shortHash("abcdef0123456789ffff"), "abcdef012345…");
  assert.equal(shortHash(null), "—");
  assert.deepEqual(outputRows(null), []);
  const rows = outputRows([
    { artifact_id: "art_x", name: "notes.txt", role: "future_role", media_type: "text/plain", size_bytes: null, sha256: null, download_url: null },
  ]);
  assert.deepEqual(rows, [{ artifactId: "art_x", name: "notes.txt", role: "future_role", roleLabel: "future_role", size: "—", hash: "—", downloadUrl: null }]);
  assert.equal(OUTPUT_ROLE_LABELS.other, "其他产物");
  assert.deepEqual(describeDecision(null), null);
  const future = describeDecision({ approval_id: "apr_1", option_id: "escalate", actor: "u", comment: null, resolved_at: "2026-09-06T12:05:00.000000Z" });
  assert.deepEqual([future.label, future.usesCleaned], ["escalate", null]);
  assert.equal(DECISION_LABELS.adopt_cleaned, "采用清洗结果");
  assert.equal(describeDecision({ approval_id: "apr_2", option_id: "adopt_cleaned", actor: "u", comment: null, resolved_at: "2026-09-06T12:05:00.000000Z" }).usesCleaned, true);
});
