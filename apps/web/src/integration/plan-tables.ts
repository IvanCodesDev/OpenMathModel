/**
 * 建模方案页「模型假设 / 符号表」两个子分页的纯数据整形（不碰 DOM，node --test 直接断言）：
 * plan-proposal 契约的 assumptions / symbols（H3 切片 2：归约后的规范化产出，
 * 也是论文「模型假设」「符号说明」两节的底稿）→ 表行 + 计数 + 验证重点。
 *
 * 顺序纪律：全局 / 共享在前，其后按 plans 里的方案顺序，归属对不上任何方案的
 * 条目排最后；同组内保持契约给出的顺序。节点已按同一纪律排过，这里再排一遍
 * 是为了不依赖上游——旧运行的投影、手工修过的数据都按同一张脸呈现。
 *
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。
 */

import type { PlanProposal } from "@openmathmodel/contracts";

export type Assumption = NonNullable<PlanProposal["assumptions"]>[number];
export type PlanSymbol = NonNullable<PlanProposal["symbols"]>[number];

/** 契约 scope 里表示「对所有方案成立」的哨兵值。 */
export const GLOBAL_SCOPE = "global";

export const IMPACT_LABELS: Record<Assumption["impact"], string> = {
  low: "低",
  medium: "中",
  high: "高",
};

export const STATUS_LABELS: Record<Assumption["status"], string> = {
  confirmed: "已确认",
  to_verify: "待检验",
  critical: "重点验证",
};

export const KIND_LABELS: Record<PlanSymbol["kind"], string> = {
  set: "集合",
  parameter: "参数",
  variable: "变量",
  objective: "目标",
  other: "其它",
};

export interface AssumptionRow {
  id: string;
  text: string;
  /** null = 全局；否则是方案 id（调用方拼「方案 A」）。 */
  planId: string | null;
  /** 依据原文；节点未给出时为空串，调用方渲染为「—」。 */
  basis: string;
  impact: Assumption["impact"];
  status: Assumption["status"];
}

export interface SymbolRow {
  /** LaTeX 记法、不带 $ 定界；调用方包成行内公式排版。 */
  symbol: string;
  kind: PlanSymbol["kind"];
  definition: string;
  unit: string | null;
  range: string | null;
  /** null = 题面共有（共享）；否则是方案 id。 */
  planId: string | null;
}

export type AssumptionsSection =
  /** 字段缺席（切片 2 之前的运行 / 规范化失败 / 单次调用路径）或空表：不放出分页。 */
  | { kind: "absent" }
  | {
      kind: "table";
      rows: AssumptionRow[];
      globalCount: number;
      planCount: number;
      /** 验证重点：status ≠ confirmed 的编号，critical 在前、to_verify 在后（各自保持表序）。 */
      focus: string[];
    };

export type SymbolsSection =
  | { kind: "absent" }
  | { kind: "table"; rows: SymbolRow[]; sharedCount: number; planCount: number };

type Plans = Pick<PlanProposal, "plans">["plans"];

/** 稳定分组排序：全局 / 共享 → 按方案顺序 → 未知归属；组内保持原序。 */
function groupOrder(planId: string | null, plans: Plans): number {
  if (planId === null) return 0;
  const index = plans.findIndex(plan => plan.id === planId);
  return index === -1 ? plans.length + 1 : index + 1;
}

function sortedByGroup<T extends { planId: string | null }>(rows: T[], plans: Plans): T[] {
  return rows
    .map((row, index) => ({ row, index, group: groupOrder(row.planId, plans) }))
    .sort((a, b) => a.group - b.group || a.index - b.index)
    .map(entry => entry.row);
}

export function describeAssumptions(
  proposal: Pick<PlanProposal, "assumptions" | "plans">,
): AssumptionsSection {
  const entries = proposal.assumptions ?? null;
  if (entries === null || entries.length === 0) return { kind: "absent" };
  const rows = sortedByGroup(
    entries.map(entry => ({
      id: entry.id,
      text: entry.text,
      planId: entry.scope === GLOBAL_SCOPE ? null : entry.scope,
      basis: entry.basis,
      impact: entry.impact,
      status: entry.status,
    })),
    proposal.plans,
  );
  const globalCount = rows.filter(row => row.planId === null).length;
  const focus = [
    ...rows.filter(row => row.status === "critical"),
    ...rows.filter(row => row.status === "to_verify"),
  ].map(row => row.id);
  return { kind: "table", rows, globalCount, planCount: rows.length - globalCount, focus };
}

export function describeSymbols(
  proposal: Pick<PlanProposal, "symbols" | "plans">,
): SymbolsSection {
  const entries = proposal.symbols ?? null;
  if (entries === null || entries.length === 0) return { kind: "absent" };
  const rows = sortedByGroup(
    entries.map(entry => ({
      symbol: entry.symbol,
      kind: entry.kind,
      definition: entry.definition,
      unit: entry.unit,
      range: entry.range,
      planId: entry.plan_id,
    })),
    proposal.plans,
  );
  const sharedCount = rows.filter(row => row.planId === null).length;
  return { kind: "table", rows, sharedCount, planCount: rows.length - sharedCount };
}
