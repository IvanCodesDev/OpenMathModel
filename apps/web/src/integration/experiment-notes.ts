/**
 * 实验与验证页、数据准备页的纯数据整形（不碰 DOM，node --test 直接断言）：
 * - 指标值格式化；
 * - experiment-summary 契约 validation.robustness（沙盒复跑的稳健性检查，G3 结果
 *   采用闸门的判定依据）→「稳健性与风险结论」小节的条目；
 * - 契约 review / validation.robustness.review（实验代码 / 检验脚本的独立审稿结论，
 *   §8.4 生成者-评审者环）→ 同一小节的审稿条目；
 * - dataset-profile 契约 cleaning（清洗脚本的执行结论 + 独立审稿，§8.4 第三个沙盒
 *   消费方；影响面数字是 G2 数据确认闸门的判定依据）→ 数据页「清洗执行与独立审稿」条目。
 *
 * 文案只产出中文源串；调用方按片段 t() 翻译后再拼接——词典按整段文本节点匹配，
 * 拼好的「通过｜实测 0.25｜阈值 0.2」译不到。
 */

import type { DatasetProfile, ExperimentSummary } from "@openmathmodel/contracts";

export type ValidationReport = NonNullable<ExperimentSummary["validation"]>;
export type RobustnessReport = NonNullable<ValidationReport["robustness"]>;
export type RobustnessCheck = RobustnessReport["checks"][number];
export type ReviewReport = NonNullable<ExperimentSummary["review"]>;
export type ReviewFinding = ReviewReport["findings"][number];
export type CleaningReport = NonNullable<DatasetProfile["cleaning"]>;

/** 指标值展示：千分位 + 有限小数；非数值原样。大数不带小数尾巴，小数保留 4 位。 */
export function formatMetricValue(value: unknown): string {
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toLocaleString("en-US", {
    maximumFractionDigits: Math.abs(num) >= 100 ? 2 : 4,
  });
}

export interface RobustnessCheckRow {
  tone: "pass" | "fail";
  /**
   * 检查名；脚本没给 name 时退回 id。检查回指了某条模型假设（assumption_id，
   * 与方案页假设表的编号同一套）时前置编号：「A1 · 需求率扰动」——编号是
   * 语言中立的，不在这里掺 UI 文案，en-US 下无需翻译。
   */
  name: string;
  /** 实测值（已格式化）；标记行没给数值时为 null。 */
  value: string | null;
  /** 阈值：数值已格式化，文字口径（如「≤ 0.05」）原样；没给时为 null。 */
  threshold: string | null;
  detail: string;
}

export type RobustnessSection =
  /** 契约字段缺席（沙盒化之前的运行 / 模拟节点）：小节不提稳健性复跑。 */
  | { kind: "absent" }
  /** 节点如实降级为「仅判读」：把原因摆出来，不让「没跑」看起来像「全过」。 */
  | { kind: "skipped"; reason: string }
  /** 复跑派出去了但沙盒会话没跑成（status ≠ passed）：checks 为空，G3 不触发。 */
  | { kind: "unfinished"; status: string; summary: string }
  /** 复跑跑成：逐项判定 + 供论文引用的一句话结论（数字只来自标记行）。 */
  | { kind: "executed"; summary: string; total: number; failed: number; rows: RobustnessCheckRow[] };

function checkRow(check: RobustnessCheck): RobustnessCheckRow {
  const threshold = check.threshold;
  const name = check.name || check.id;
  const assumption = typeof check.assumption_id === "string" ? check.assumption_id.trim() : "";
  return {
    tone: check.passed ? "pass" : "fail",
    name: assumption ? `${assumption} · ${name}` : name,
    value: check.value === null ? null : formatMetricValue(check.value),
    threshold:
      threshold === null || threshold === ""
        ? null
        : typeof threshold === "number"
          ? formatMetricValue(threshold)
          : threshold,
    detail: check.detail,
  };
}

export function describeRobustness(
  robustness: RobustnessReport | null | undefined,
): RobustnessSection {
  if (!robustness) return { kind: "absent" };
  if (!robustness.executed) return { kind: "skipped", reason: robustness.reason };
  if (robustness.status !== "passed") {
    return {
      kind: "unfinished",
      status: robustness.status ?? "unknown",
      summary: robustness.summary_text,
    };
  }
  return {
    kind: "executed",
    summary: robustness.summary_text,
    total: robustness.checks_total,
    failed: robustness.checks_failed,
    rows: robustness.checks.map(checkRow),
  };
}

/** 一条审稿意见的展示行：严重度中文源串（调用方 t()）+ 「位置：问题」正文。 */
export interface ReviewFindingRow {
  severity: "blocker" | "major" | "minor";
  /** 「阻断 / 主要 / 次要」中文源串，调用方 t()。 */
  severityLabel: string;
  /** 「location：issue」；没给位置时只有 issue。修法不进结果页（生成者已按它修过）。 */
  text: string;
}

/** 节点确定性复跑核对的三态：一致 / 不一致 / 未复跑（预算不足或脚本正文缺失）。 */
export type RerunState = "consistent" | "inconsistent" | "not_run";

export type ReviewSection =
  /** 契约字段缺席（审稿环之前的运行 / 模拟节点）：小节不提审稿。 */
  | { kind: "absent" }
  /** 审稿没派出去（无监督者 / 预算不足 / 子代理未完成）：把原因摆出来，不让「没审」看起来像「审过」。 */
  | { kind: "skipped"; reason: string }
  /** 审稿通过：轮数、意见数（含 minor）、复跑三态、审稿人一句话。 */
  | { kind: "accepted"; rounds: number; findings: ReviewFindingRow[]; summary: string; rerun: RerunState }
  /** 僵持：驳回后修不动 / 未经复审，未解决的阻断性意见交 G3 裁定；结果页要能看到是哪几条。 */
  | { kind: "stalemate"; rounds: number; blockers: number; findings: ReviewFindingRow[]; summary: string; reason: string; rerun: RerunState };

const SEVERITY_LABEL: Record<ReviewFindingRow["severity"], string> = {
  blocker: "阻断",
  major: "主要",
  minor: "次要",
};

function findingRow(finding: ReviewFinding): ReviewFindingRow {
  const location = finding.location.trim();
  return {
    severity: finding.severity,
    severityLabel: SEVERITY_LABEL[finding.severity],
    text: location ? `${location}：${finding.issue}` : finding.issue,
  };
}

function rerunState(review: ReviewReport): RerunState {
  if (review.rerun_consistent === null || review.rerun_consistent === undefined) return "not_run";
  return review.rerun_consistent ? "consistent" : "inconsistent";
}

export function describeReview(review: ReviewReport | null | undefined): ReviewSection {
  if (!review) return { kind: "absent" };
  if (!review.executed) return { kind: "skipped", reason: review.reason };
  const findings = review.findings.map(findingRow);
  if (review.stalemate) {
    return {
      kind: "stalemate",
      rounds: review.rounds,
      blockers: review.blockers,
      // 僵持时只列阻断性意见：这些才是没解决、要进闸门与论文局限性的
      findings: findings.filter(row => row.severity === "blocker"),
      summary: review.summary,
      reason: review.reason,
      rerun: rerunState(review),
    };
  }
  return {
    kind: "accepted",
    rounds: review.rounds,
    findings,
    summary: review.summary,
    rerun: rerunState(review),
  };
}

export type CleaningSection =
  /** 契约字段缺席（该字段出现之前的运行 / 模拟节点）：数据页不提清洗执行。 */
  | { kind: "absent" }
  /** 清洗没跑（无工具 / 监督者 / 数据文件 / 预算、子代理未完成）：把原因摆出来，不让「没跑」看起来像「跑过」。 */
  | { kind: "skipped"; reason: string }
  /** 清洗跑了：验收结论 + 影响面（数字来自脚本标记行，已格式化）+ 工程师自述 + 审稿结论。 */
  | {
      kind: "executed";
      tone: "pass" | "fail";
      passed: boolean;
      attempts: number;
      rowsBefore: string;
      rowsAfter: string;
      /** 删行比例，「8.0%」；比例来自节点按标记行的换算（G2 阈值 5%）。 */
      deletedRatio: string;
      imputed: string[];
      imputedTargets: string[];
      summary: string;
      review: ReviewSection;
    };

export function describeCleaning(cleaning: CleaningReport | null | undefined): CleaningSection {
  if (!cleaning) return { kind: "absent" };
  if (!cleaning.executed) return { kind: "skipped", reason: cleaning.reason };
  const passed = cleaning.status === "passed";
  return {
    kind: "executed",
    tone: passed ? "pass" : "fail",
    passed,
    attempts: cleaning.attempts,
    rowsBefore: formatMetricValue(cleaning.rows_before),
    rowsAfter: formatMetricValue(cleaning.rows_after),
    deletedRatio: `${(cleaning.rows_deleted_ratio * 100).toFixed(1)}%`,
    imputed: cleaning.imputed_columns,
    imputedTargets: cleaning.imputed_target_columns,
    summary: cleaning.summary,
    review: describeReview(cleaning.review),
  };
}
