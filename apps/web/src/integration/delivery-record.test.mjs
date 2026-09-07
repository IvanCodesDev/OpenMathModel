import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./delivery-record.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { DELIVERY_STATUS_LABELS, deliveryTone, describeDelivery, fileRows, shortHash, summarizeChecks } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture delivery-manifest.3：已确认交付、五项检查全过、四个产物（三个带哈希可下载）。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/delivery-manifest.3.json", import.meta.url),
    "utf8",
  ),
);
/** 契约 fixture delivery-manifest.1：切片 17 之前的形状（无 delivery、无 sha256）。 */
const legacy = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/delivery-manifest.1.json", import.meta.url),
    "utf8",
  ),
);

test("describeDelivery: confirmed record → status label, tone, checks summary, files and hashes", () => {
  const view = describeDelivery(fixture);
  assert.equal(view.status, "confirmed");
  assert.equal(view.statusLabel, "已确认交付");
  assert.equal(view.tone, "ok");
  assert.equal(view.confirmedAt, "2026-09-07T05:40:00.000000Z");
  assert.equal(view.comment, "结论可用，直接交付。");
  assert.deepEqual(view.summary, { passed: 5, total: 5 });
  assert.deepEqual(view.checks.map(check => check.id), [
    "paper_artifact_ready", "audit_clean", "figures_delivered", "metrics_in_paper", "validation_reported",
  ]);
  assert.deepEqual(view.audit, fixture.delivery.audit);
  assert.deepEqual(view.files, { total: 4, ready: 3, hashed: 3 });
  assert.deepEqual(view.rows, [
    { name: "results.csv", kind: "table", hash: "7f83b1657ff1…", ready: true },
    { name: "fit_vs_baseline.png", kind: "figure", hash: "2c26b46b68ff…", ready: true },
    { name: "paper-draft.md", kind: "paper", hash: "fcde2b2edba5…", ready: true },
    { name: "run.log", kind: "log", hash: "—", ready: false },
  ]);
});

test("describeDelivery: manifests without a delivery record yield null (legacy / sim runs)", () => {
  assert.equal(describeDelivery(legacy), null);
  assert.equal(describeDelivery({ ...legacy, delivery: null }), null);
  // 旧形状的产物没有 sha256：文件行短哈希退 "—"，不抛
  assert.deepEqual(fileRows(legacy.artifacts).map(row => row.hash), ["—", "—"]);
});

test("status labels, tones and unknown values", () => {
  assert.equal(DELIVERY_STATUS_LABELS.pending_confirmation, "等待确认交付");
  assert.equal(deliveryTone("confirmed"), "ok");
  assert.equal(deliveryTone("pending_confirmation"), "warn");
  assert.equal(deliveryTone("returned_for_revision"), "warn");
  assert.equal(deliveryTone("not_ready"), "muted");
  assert.equal(deliveryTone("unattended"), "muted");
  const future = describeDelivery({ ...fixture, delivery: { ...fixture.delivery, status: "future_status" } });
  assert.equal(future.statusLabel, "future_status", "契约外状态原样透出");
  assert.equal(future.tone, "muted");
  assert.deepEqual(summarizeChecks([{ passed: true }, { passed: false }, { passed: false }]), { passed: 1, total: 3 });
  assert.equal(shortHash(null), "—");
  assert.equal(shortHash("abcdef0123456789ff"), "abcdef012345…");
});
