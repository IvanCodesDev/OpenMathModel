import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./experiment-notes.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { describeReview, describeRobustness, formatMetricValue } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture experiment-summary.3 的 robustness：三项里一项未通过（G3 会触发）。 */
function executedWithFailure() {
  return {
    executed: true,
    status: "passed",
    summary_text: "沙盒复跑稳健性检查 3 项，通过 2 项；未通过：需求率扰动（sensitivity：value 0.25，阈值 0.2）。",
    checks: [
      { id: "sensitivity", name: "需求率扰动", passed: false, value: 0.25, threshold: 0.2, detail: "超出阈值", assumption_id: "A1" },
      { id: "bootstrap", name: "重采样稳定性", passed: true, value: 0.08, threshold: 0.15, detail: "在阈值内", assumption_id: null },
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
    // 检查回指了假设 A1（方案页假设表的编号）→ 编号前置；null / 缺席都不加前缀
    { tone: "fail", name: "A1 · 需求率扰动", value: "0.25", threshold: "0.2", detail: "超出阈值" },
    { tone: "pass", name: "重采样稳定性", value: "0.08", threshold: "0.15", detail: "在阈值内" },
    // 标记行没给数值 → value null；阈值的文字口径原样；没给 name 退回 id
    { tone: "pass", name: "baseline", value: null, threshold: "≥ 0.1", detail: "" },
  ]);
});

test("assumption tag: blank ids are ignored, id-fallback names still get the tag", () => {
  const section = describeRobustness({
    ...executedWithFailure(),
    checks: [
      { id: "slack", name: "预算松紧扰动", passed: true, value: 0.03, threshold: 0.2, detail: "", assumption_id: "   " },
      { id: "corr", name: "", passed: false, value: 0.31, threshold: 0.15, detail: "", assumption_id: "G2" },
    ],
    checks_total: 2,
    checks_failed: 1,
  });
  assert.deepEqual(section.rows.map((row) => row.name), ["预算松紧扰动", "G2 · corr"]);
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

// ── 独立审稿（契约 review / robustness.review，fixture experiment-summary.5） ──

/** fixture.5 的实验代码审稿：一轮通过，一条 minor 意见，复跑一致。 */
function acceptedReview() {
  return {
    executed: true,
    verdict: "accept",
    rounds: 1,
    findings: [
      { id: "R1", severity: "minor", location: "experiment.py:12", issue: "随机种子写死在脚本里，建议改为常量集中管理", fix_hint: "抽成 SEED 常量" },
    ],
    blockers: 0,
    summary: "实现忠实于方案 A，指标口径与方案一致，可复现",
    stalemate: false,
    rerun_consistent: true,
    reason: "",
  };
}

/** fixture.5 的检验脚本审稿：两轮后僵持，一条 blocker + 一条 minor。 */
function stalemateReview() {
  return {
    executed: true,
    verdict: "reject",
    rounds: 2,
    findings: [
      { id: "R1", severity: "blocker", location: "robustness.py:perturb()", issue: "扰动只作用在训练集，评估集未同步扰动，敏感性数值偏乐观", fix_hint: "扰动后重新切分并同时评估" },
      { id: "R2", severity: "minor", location: "", issue: "阈值 0.2 未说明来源", fix_hint: "" },
    ],
    blockers: 1,
    summary: "扰动实现有缺陷，敏感性结论不能采信",
    stalemate: true,
    rerun_consistent: true,
    reason: "审稿 2 轮后仍有阻断性意见未解决",
  };
}

test("accepted review: rounds, every finding (location-prefixed), rerun state, reviewer summary", () => {
  assert.deepEqual(describeReview(acceptedReview()), {
    kind: "accepted",
    rounds: 1,
    findings: [
      { severity: "minor", severityLabel: "次要", text: "experiment.py:12：随机种子写死在脚本里，建议改为常量集中管理" },
    ],
    summary: "实现忠实于方案 A，指标口径与方案一致，可复现",
    rerun: "consistent",
  });
});

test("stalemate review: only blockers are listed (they are what G3 and the paper must carry)", () => {
  assert.deepEqual(describeReview(stalemateReview()), {
    kind: "stalemate",
    rounds: 2,
    blockers: 1,
    findings: [
      { severity: "blocker", severityLabel: "阻断", text: "robustness.py:perturb()：扰动只作用在训练集，评估集未同步扰动，敏感性数值偏乐观" },
    ],
    summary: "扰动实现有缺陷，敏感性结论不能采信",
    reason: "审稿 2 轮后仍有阻断性意见未解决",
    rerun: "consistent",
  });
});

test("rerun state: null → not_run, false → inconsistent; blank location keeps the bare issue", () => {
  const noRerun = describeReview({ ...acceptedReview(), rerun_consistent: null, findings: [
    { id: "R1", severity: "major", location: "  ", issue: "只报了 rmse", fix_hint: "" },
  ] });
  assert.equal(noRerun.rerun, "not_run");
  assert.deepEqual(noRerun.findings, [{ severity: "major", severityLabel: "主要", text: "只报了 rmse" }]);
  assert.equal(describeReview({ ...stalemateReview(), rerun_consistent: false }).rerun, "inconsistent");
});

test("skipped review surfaces the node's reason; absent field renders nothing", () => {
  assert.deepEqual(
    describeReview({ executed: false, verdict: null, rounds: 0, findings: [], blockers: 0, summary: "", stalemate: false, rerun_consistent: null, reason: "未配置子代理监督者，跳过独立审稿" }),
    { kind: "skipped", reason: "未配置子代理监督者，跳过独立审稿" },
  );
  assert.deepEqual(describeReview(null), { kind: "absent" });
  assert.deepEqual(describeReview(undefined), { kind: "absent" });
});

test("formatMetricValue: thousands separators, bounded decimals, non-numbers untouched", () => {
  assert.equal(formatMetricValue(0.123456), "0.1235");
  assert.equal(formatMetricValue(1234.5678), "1,234.57");
  assert.equal(formatMetricValue(100), "100");
  assert.equal(formatMetricValue("0.5"), "0.5");
  assert.equal(formatMetricValue("n/a"), "n/a");
});
