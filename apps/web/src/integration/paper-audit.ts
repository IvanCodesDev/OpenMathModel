/**
 * 论文页「终稿审计」条的纯数据整形（不碰 DOM，node --test 直接断言）：
 * document-draft 契约的 frozen_numbers / audit_findings（H5 数字冻结清单与终稿审计链——
 * 数值 / 图表 / 引用三条确定性审计，是 G4 定稿交付闸门「确认交付 / 退回修改」的证据）
 * → 四态 + 表行 + 按发现类型的计数。
 *
 * 数值一律原样字符串：清单的口径就是「保持数值原样、不换算不四舍五入」，
 * 这里若再做千分位 / 截小数，表里的数就对不上正文里被审计的 token。
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。
 */

import type { DocumentDraft } from "@openmathmodel/contracts";

export type FrozenNumber = NonNullable<DocumentDraft["frozen_numbers"]>[number];
export type AuditFinding = NonNullable<DocumentDraft["audit_findings"]>[number];

/**
 * 发现类型 → 一行原因（中文源串，调用方 t()）。契约 enum 之外的 kind 不在表里：
 * 调用方退回节点给的 detail 原文（消费者须容忍未知取值）。
 */
export const FINDING_KIND_REASONS: Record<string, string> = {
  unsourced_number: "不在冻结清单与材料中",
  phantom_figure: "引用的图没有对应的真实图件",
  phantom_table: "引用的表在全文找不到带该编号表题的表格",
  unverified_citation: "引用未经验证（参考文献库尚未建立）",
};

/** 发现类型 → 汇总一句里的量词短语（「N 处…」的后半段），图 / 表并成一类。 */
const FINDING_KIND_COUNT_LABELS: Record<string, string> = {
  unsourced_number: "处无出处数值",
  phantom_figure: "处图表引用不实",
  phantom_table: "处图表引用不实",
  unverified_citation: "处引用未经验证",
};
const FINDING_KIND_OTHER_LABEL = "处其他发现";

export interface FindingKindCount {
  /** 量词短语（中文源串，调用方 t()）。 */
  label: string;
  count: number;
}

/**
 * 按发现类型计数，顺序固定为 数值 → 图表 → 引用 → 其他；只有数值发现时结果与
 * 首版「N 处无出处数值」逐字相同。
 */
export function summarizeFindingKinds(findings: readonly { kind: string }[]): FindingKindCount[] {
  const counts = new Map<string, number>();
  for (const finding of findings) {
    const label = FINDING_KIND_COUNT_LABELS[finding.kind] ?? FINDING_KIND_OTHER_LABEL;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const order = [...new Set([...Object.values(FINDING_KIND_COUNT_LABELS), FINDING_KIND_OTHER_LABEL])];
  return order
    .filter(label => counts.has(label))
    .map(label => ({ label, count: counts.get(label) ?? 0 }));
}

/** 冻结清单条目的出处阶段 → 页面用的阶段名（与工作台执行轨迹的阶段文案一致）。 */
export const FROZEN_STAGE_LABELS: Record<string, string> = {
  DATA_PREPARATION: "数据准备",
  MODEL_PLANNING: "建模方案",
  EXPERIMENTING: "实验运行",
  VALIDATING: "结果验证",
};

export interface FrozenRow {
  id: string;
  /** 冻结值原样（String(value)），与正文被审计的 token 同口径。 */
  value: string;
  label: string;
  /** 出处阶段的中文标签（调用方 t() 翻译）；enum 外的值原样透出。 */
  stage: string;
  /** 阶段产出内的路径（metrics.rmse / robustness.checks[1].threshold …）。 */
  path: string;
}

export interface FindingRow {
  scope: string;
  kind: string;
  /** 对不上账的数值 token 原样（取样 ≤ 8）。 */
  numbers: string[];
  detail: string;
}

export type PaperAuditSection =
  /** 两个字段都缺席（2026-09-03 之前的运行 / 模拟节点）：不渲染审计条。 */
  | { kind: "absent" }
  /** 有清单、没审计（契约允许分别为 null）：只列清单，不下结论。 */
  | { kind: "unaudited"; rows: FrozenRow[] }
  /** 审计过且 0 违规：数值、图表、引用三条审计全部通过。 */
  | { kind: "clean"; rows: FrozenRow[] }
  /** 审计过且 ≥1 处发现（无出处数值 / 图表引用不实 / 引用未经验证）：G4 推荐「退回修改」的依据。 */
  | { kind: "findings"; rows: FrozenRow[]; findings: FindingRow[] };

function frozenRows(entries: readonly FrozenNumber[] | null | undefined): FrozenRow[] {
  return (entries ?? []).map(entry => ({
    id: entry.id,
    value: String(entry.value),
    label: entry.label || entry.id,
    stage: FROZEN_STAGE_LABELS[entry.source_stage] ?? entry.source_stage,
    path: entry.source_path,
  }));
}

function findingRows(entries: readonly AuditFinding[]): FindingRow[] {
  return entries.map(finding => ({
    scope: finding.scope,
    kind: finding.kind,
    numbers: [...finding.numbers],
    detail: finding.detail,
  }));
}

export function describePaperAudit(
  draft: Pick<DocumentDraft, "frozen_numbers" | "audit_findings">,
): PaperAuditSection {
  const frozen = draft.frozen_numbers ?? null;
  const findings = draft.audit_findings ?? null;
  if (frozen === null && findings === null) return { kind: "absent" };
  const rows = frozenRows(frozen);
  if (findings === null) return { kind: "unaudited", rows };
  if (findings.length === 0) return { kind: "clean", rows };
  return { kind: "findings", rows, findings: findingRows(findings) };
}

/** 同一份草稿的审计条只渲染一次（SSE 高频刷新）：签名 = 运行 + 版本 + 更新时间。 */
export function paperAuditStamp(
  draft: Pick<DocumentDraft, "run_id" | "version" | "updated_at">,
): string {
  return `${draft.run_id}:${draft.version}:${draft.updated_at}`;
}
