import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./experiment-notes.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { describeRobustness, formatMetricValue } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture experiment-summary.3 的 robustness：三项里一项未通过（G3 会触发）。 */
function executedWithFailure() {
  return {
    executed: true,
    status: "passed",
    summary_text: "沙盒复跑稳健性检查 3 项，通过 2 项；未通过：需求率扰动（sensitivity：value 0.25，阈值 0.2）。",
    checks: [
      { id: "sensitivity", name: "需求率扰动", passed: false, value: 0.25, threshold: 0.2, detail: "超出阈值" },
      { id: "bootstrap", name: "重采样稳定性", passed: true, value: 0.08, threshold: 0.15, detail: "在阈值内" },
      { id: "baseline", name: "", passed: true, value: null, threshold: "≥ 0.1", detail: "" },
    ],
    checks_total: 3,
    checks_failed: 1,
    reason: "",
  };
}

test("executed rerun: one row per check, numbers formatted, name falls back to id", () => {
  const section = describeRobustness(executedWithFailure());
  assert.equal(section.kind, "executed");
  assert.equal(section.total, 3);
  assert.equal(section.failed, 1);
  assert.match(section.summary, /通过 2 项/);
  assert.deepEqual(section.rows, [
    { tone: "fail", name: "需求率扰动", value: "0.25", threshold: "0.2", detail: "超出阈值" },
    { tone: "pass", name: "重采样稳定性", value: "0.08", threshold: "0.15", detail: "在阈值内" },
    // 标记行没给数值 → value null；阈值的文字口径原样；没给 name 退回 id
    { tone: "pass", name: "baseline", value: null, threshold: "≥ 0.1", detail: "" },
  ]);
});

test("skipped rerun surfaces the node's reason instead of looking like a pass", () => {
  const section = describeRobustness({
    executed: false,
    status: null,
    summary_text: "",
    checks: [],
    checks_total: 0,
    checks_failed: 0,
    reason: "工作区中没有实验脚本 experiment.py，无法复跑；检验结论仅来自评审判读",
  });
  assert.deepEqual(section, {
    kind: "skipped",
    reason: "工作区中没有实验脚本 experiment.py，无法复跑；检验结论仅来自评审判读",
  });
});

test("unfinished sandbox session (status ≠ passed) keeps the honest summary sentence", () => {
  const section = describeRobustness({
    executed: true,
    status: "failed",
    summary_text: "稳健性检查沙盒复跑未完成（failed），检验结论仅来自评审判读。",
    checks: [],
    checks_total: 0,
    checks_failed: 0,
    reason: "",
  });
  assert.deepEqual(section, {
    kind: "unfinished",
    status: "failed",
    summary: "稳健性检查沙盒复跑未完成（failed），检验结论仅来自评审判读。",
  });
});

test("absent field (runs before sandboxing / sim nodes) renders nothing about the rerun", () => {
  assert.deepEqual(describeRobustness(null), { kind: "absent" });
  assert.deepEqual(describeRobustness(undefined), { kind: "absent" });
});

test("formatMetricValue: thousands separators, bounded decimals, non-numbers untouched", () => {
  assert.equal(formatMetricValue(0.123456), "0.1235");
  assert.equal(formatMetricValue(1234.5678), "1,234.57");
  assert.equal(formatMetricValue(100), "100");
  assert.equal(formatMetricValue("0.5"), "0.5");
  assert.equal(formatMetricValue("n/a"), "n/a");
});
