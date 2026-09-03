/**
 * 实验与验证页的纯数据整形（不碰 DOM，node --test 直接断言）：
 * - 指标值格式化；
 * - experiment-summary 契约 validation.robustness（沙盒复跑的稳健性检查，G3 结果
 *   采用闸门的判定依据）→「稳健性与风险结论」小节的条目。
 *
 * 文案只产出中文源串；调用方按片段 t() 翻译后再拼接——词典按整段文本节点匹配，
 * 拼好的「通过｜实测 0.25｜阈值 0.2」译不到。
 */

import type { ExperimentSummary } from "@openmathmodel/contracts";

export type ValidationReport = NonNullable<ExperimentSummary["validation"]>;
export type RobustnessReport = NonNullable<ValidationReport["robustness"]>;
export type RobustnessCheck = RobustnessReport["checks"][number];

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
  /** 检查名；脚本没给 name 时退回 id。 */
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
  return {
    tone: check.passed ? "pass" : "fail",
    name: check.name || check.id,
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
