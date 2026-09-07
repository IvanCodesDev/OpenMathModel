/**
 * 论文页已验证引用库的纯数据整形（不碰 DOM，node --test 直接断言）：
 * document-draft 契约的 references（H5 refs/ 第一步——方案阶段从知识库解析到的条目：选中方案
 * 引用的先例 + 用户提供的资料，记录级出处 URL，编号固定）→「终稿审计」条的计数与清单表行。
 *
 * 出处链接只放行 http(s)（与 renderMarkdown 的链接纪律一致）；标题 / 条目是契约原文，调用方不译。
 */

import type { DocumentDraft } from "@openmathmodel/contracts";

export type PaperReference = NonNullable<DocumentDraft["references"]>[number];

/** 条目来源 → 页面用的中文源串（调用方 t()）；enum 外的值原样透出。 */
export const REFERENCE_SOURCE_LABELS: Record<string, string> = {
  plan_citation: "方案引用的先例",
  user_reference: "用户提供",
};

/** 只放行显式 http(s) 的出处链接，其余（含空值）→ null，页面不做成超链接。 */
export function safeHttpUrl(url: string | null | undefined): string | null {
  const text = String(url ?? "").trim();
  return /^https?:\/\/\S+$/.test(text) ? text : null;
}

export interface ReferenceSummary {
  total: number;
  cited: number;
}

/** 「终稿审计」条的文献计数；库缺席 / 为空 → null（无库运行的条文案逐字不变）。 */
export function summarizeReferences(
  references: readonly PaperReference[] | null | undefined,
): ReferenceSummary | null {
  if (!references || references.length === 0) return null;
  return {
    total: references.length,
    cited: references.filter(reference => reference.cited).length,
  };
}

export interface ReferenceRow {
  /** 「[n]」。 */
  label: string;
  title: string;
  /** 条目正文原文（含 Markdown 链接语法），悬停可见。 */
  text: string;
  url: string | null;
  /** 来源的中文源串（调用方 t()）。 */
  source: string;
  cited: boolean;
}

export function referenceRows(references: readonly PaperReference[]): ReferenceRow[] {
  return references.map(reference => ({
    label: `[${reference.number}]`,
    title: reference.title,
    text: reference.text,
    url: safeHttpUrl(reference.url),
    source: REFERENCE_SOURCE_LABELS[reference.source] ?? reference.source,
    cited: reference.cited,
  }));
}
