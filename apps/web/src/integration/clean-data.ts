/**
 * 数据准备页「清洗数据」分页的纯数据整形（不碰 DOM，node --test 直接断言）：
 * dataset-profile 契约的 cleaning（执行结论 / 影响面 / 审稿）+ cleaning.outputs（清洗产物：cleaned/ 数据表
 * 与 cleaning.py，H4 切片 s23 起）+ cleaning.decision（G2 数据确认决策）→ 页头状态 / 三个指标 / 产物行 /
 * 结论条目。
 *
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。契约 enum 之外的 role / option_id
 * 原样透出（消费者须容忍未知取值）。没有清洗（未下发数据 / 跳过）→ kind="skipped" 带原因，页面据此
 * 给诚实空态而不是装作清洗过；该字段出现之前的运行 → kind="absent"，分页不出现。
 */

import type { DatasetProfile } from "@openmathmodel/contracts";

import { describeCleaning, describeReview } from "./experiment-notes";
import type { ReviewSection } from "./experiment-notes";

export type CleaningReport = NonNullable<DatasetProfile["cleaning"]>;
export type CleaningOutput = NonNullable<CleaningReport["outputs"]>[number];
export type CleaningDecision = NonNullable<CleaningReport["decision"]>;

/** 产物角色 → 中文源串（调用方 t()）；enum 外原样。 */
export const OUTPUT_ROLE_LABELS: Record<string, string> = {
  cleaned_data: "清洗后数据",
  script: "清洗脚本",
  other: "其他产物",
};

/** G2 选项 → 中文源串（与节点 G2_OPTIONS 的 label 同一口径）；enum 外原样。 */
export const DECISION_LABELS: Record<string, string> = {
  adopt_cleaned: "采用清洗结果",
  use_raw: "改用原始数据",
  reject: "退回调整",
};

/** 字节数 → 「12.3 KB」；未知 → "—"。 */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

/** 登记哈希的短写（前 12 位 + …）；缺失 → "—"。与成果页交付记录同一写法。 */
export function shortHash(sha256: string | null | undefined): string {
  const text = String(sha256 ?? "").trim();
  return text ? `${text.slice(0, 12)}…` : "—";
}

export interface OutputRow {
  artifactId: string;
  name: string;
  role: string;
  /** 中文源串（调用方 t()）。 */
  roleLabel: string;
  size: string;
  hash: string;
  downloadUrl: string | null;
}

export function outputRows(outputs: readonly CleaningOutput[] | null | undefined): OutputRow[] {
  return (outputs ?? []).map(output => ({
    artifactId: output.artifact_id,
    name: output.name,
    role: output.role,
    roleLabel: OUTPUT_ROLE_LABELS[output.role] ?? output.role,
    size: formatSize(output.size_bytes),
    hash: shortHash(output.sha256),
    downloadUrl: output.download_url ?? null,
  }));
}

export interface DecisionView {
  optionId: string;
  /** 中文源串（调用方 t()）；enum 外原样。 */
  label: string;
  actor: string;
  comment: string | null;
  resolvedAt: string;
  /** 下游用的是清洗后的表（adopt_cleaned）还是原始 data/（use_raw）；退回调整 → null。 */
  usesCleaned: boolean | null;
}

export function describeDecision(decision: CleaningDecision | null | undefined): DecisionView | null {
  if (!decision) return null;
  return {
    optionId: decision.option_id,
    label: DECISION_LABELS[decision.option_id] ?? decision.option_id,
    actor: decision.actor,
    comment: decision.comment ?? null,
    resolvedAt: decision.resolved_at,
    usesCleaned: decision.option_id === "adopt_cleaned" ? true : decision.option_id === "use_raw" ? false : null,
  };
}

export type CleanDataView =
  /** 契约字段缺席（该字段出现之前的运行 / 模拟节点）：分页不出现。 */
  | { kind: "absent" }
  /** 清洗没跑：诚实空态——原因 + 「后续阶段按 data/ 原始文件继续」。 */
  | { kind: "skipped"; reason: string }
  | {
      kind: "executed";
      passed: boolean;
      tone: "pass" | "fail";
      /** 中文源串（调用方 t()）。 */
      statusLabel: string;
      attempts: number;
      rowsBefore: number;
      rowsAfter: number;
      /** 「1,104」 一类已格式化的行数。 */
      rowsBeforeText: string;
      rowsAfterText: string;
      /** 保留比例，「92.0%」；rows_before 为 0 时 "—"。 */
      retainedRatio: string;
      /** 删行比例，「8.0%」（G2 阈值 5%）。 */
      deletedRatio: string;
      /** 删行比例是否越过 G2 阈值（5%）。 */
      deletionOverThreshold: boolean;
      imputed: string[];
      imputedTargets: string[];
      summary: string;
      review: ReviewSection;
      outputs: OutputRow[];
      dataOutputs: OutputRow[];
      script: OutputRow | null;
      decision: DecisionView | null;
    };

const G2_ROW_DELETION_THRESHOLD = 0.05;

export function describeCleanData(profile: DatasetProfile): CleanDataView {
  const cleaning = profile.cleaning ?? null;
  const section = describeCleaning(cleaning);
  if (section.kind === "absent" || !cleaning) return { kind: "absent" };
  if (section.kind === "skipped") return { kind: "skipped", reason: section.reason };
  const rows = outputRows(cleaning.outputs);
  const retained = cleaning.rows_before > 0
    ? `${((cleaning.rows_after / cleaning.rows_before) * 100).toFixed(1)}%`
    : "—";
  return {
    kind: "executed",
    passed: section.passed,
    tone: section.tone,
    statusLabel: section.passed ? "通过验收" : "未通过验收",
    attempts: cleaning.attempts,
    rowsBefore: cleaning.rows_before,
    rowsAfter: cleaning.rows_after,
    rowsBeforeText: section.rowsBefore,
    rowsAfterText: section.rowsAfter,
    retainedRatio: retained,
    deletedRatio: section.deletedRatio,
    deletionOverThreshold: cleaning.rows_deleted_ratio > G2_ROW_DELETION_THRESHOLD,
    imputed: section.imputed,
    imputedTargets: section.imputedTargets,
    summary: section.summary,
    review: describeReview(cleaning.review),
    outputs: rows,
    dataOutputs: rows.filter(row => row.role === "cleaned_data"),
    script: rows.find(row => row.role === "script") ?? null,
    decision: describeDecision(cleaning.decision),
  };
}
