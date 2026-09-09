/**
 * 结果页图件条与成果页「论文文件」分页的纯数据整形（不碰 DOM，node --test 直接断言）：
 * - experiment-summary 契约的 figures（H5 切片 s25：实验 / 检验沙盒真实落盘并被采集的图件，编号与论文
 *   节点的图件清单同一规则）→ 结果页缩略图行；
 * - document-draft 契约的 figures（六键，含论文阶段补画的图）与 references（已验证引用库）→ 成果页
 *   「论文附图 N 张（已插入 M）」缩略图条 + 「参考文献 N 条（已引用 M）」清单。
 *
 * 缩略图 / 下载只给有 artifact_id 的图（同源产物下载链接，下载即哈希核验）；没有 id 的图只列文字行，
 * 绝不拼一个 404 的链接。文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。
 */

import type { DocumentDraft, ExperimentSummary } from "@openmathmodel/contracts";

import { FROZEN_STAGE_LABELS } from "./paper-audit";
import { artifactDownloadUrl, summarizeFigures } from "./paper-figures";
import { REFERENCE_SOURCE_LABELS, safeHttpUrl, summarizeReferences } from "./paper-references";

export type ExperimentFigure = NonNullable<ExperimentSummary["figures"]>[number];
export type PaperFigure = NonNullable<DocumentDraft["figures"]>[number];
export type PaperReference = NonNullable<DocumentDraft["references"]>[number];

/** 记录级验证状态 → 中文源串（调用方 t()）；enum 外原样。 */
export const VERIFICATION_LABELS: Record<string, string> = {
  source_verified: "来源可核",
  title_matched: "按标题匹配",
  unverified: "未验证",
};

export interface FigureCard {
  number: number;
  /** 「图 N」。 */
  label: string;
  name: string;
  /** 图题；画图工程师未给说明时退回文件名。 */
  caption: string;
  /** 来源阶段的中文源串（调用方 t()）；enum 外原样。 */
  stage: string;
  stageKey: string;
  artifactId: string | null;
  /** 有产物 id 才有：缩略图与下载共用同一同源地址。 */
  imageUrl: string | null;
  /** 论文清单才有的「已插入正文」；实验图源 → null。 */
  inserted: boolean | null;
}

export interface FigureLike {
  number: number;
  name: string;
  caption: string;
  source_stage: string;
  artifact_id: string | null;
  inserted?: boolean;
}

/** 任一契约的图件清单（experiment / dataset-profile / document-draft 的 figures）→ 按编号排序的缩略图卡。 */
export function figureCards(figures: readonly FigureLike[]): FigureCard[] {
  return [...figures].sort((a, b) => a.number - b.number).map(figureCard);
}

function figureCard(figure: FigureLike): FigureCard {
  const artifactId = figure.artifact_id ? String(figure.artifact_id) : null;
  return {
    number: figure.number,
    label: `图 ${figure.number}`,
    name: figure.name,
    caption: figure.caption.trim() || figure.name,
    stage: FROZEN_STAGE_LABELS[figure.source_stage] ?? figure.source_stage,
    stageKey: figure.source_stage,
    artifactId,
    imageUrl: artifactId ? artifactDownloadUrl(artifactId) : null,
    inserted: typeof figure.inserted === "boolean" ? figure.inserted : null,
  };
}

export type ResultFiguresView =
  /** 契约字段缺席（该字段出现之前的运行）：结果页不提图件。 */
  | { kind: "absent" }
  /** 字段在但为空：如实说「本次实验 / 检验没有产出图件」。 */
  | { kind: "empty" }
  | { kind: "figures"; cards: FigureCard[]; total: number; withImage: number; stages: string[] };

export function describeResultFigures(summary: ExperimentSummary): ResultFiguresView {
  const figures = summary.figures;
  if (!Array.isArray(figures)) return { kind: "absent" };
  if (figures.length === 0) return { kind: "empty" };
  const cards = figureCards(figures);
  const stages: string[] = [];
  for (const card of cards) if (!stages.includes(card.stage)) stages.push(card.stage);
  return {
    kind: "figures",
    cards,
    total: cards.length,
    withImage: cards.filter(card => card.imageUrl).length,
    stages,
  };
}

export interface ReferenceItem {
  number: number;
  /** 「[n]」。 */
  label: string;
  key: string | null;
  title: string;
  text: string;
  url: string | null;
  /** 来源 / 验证状态的中文源串（调用方 t()）。 */
  source: string;
  verification: string | null;
  cited: boolean;
}

export function referenceItems(references: readonly PaperReference[]): ReferenceItem[] {
  return [...references]
    .sort((a, b) => a.number - b.number)
    .map(reference => ({
      number: reference.number,
      label: `[${reference.number}]`,
      key: reference.key ?? null,
      title: reference.title,
      text: reference.text,
      url: safeHttpUrl(reference.url),
      source: REFERENCE_SOURCE_LABELS[reference.source] ?? reference.source,
      verification: reference.verification ? (VERIFICATION_LABELS[reference.verification] ?? reference.verification) : null,
      cited: reference.cited,
    }));
}

export interface PaperPackageView {
  /** 图件：清单缺席 → null（旧运行，分页不提图）；空表 → { total: 0 }。 */
  figures: { total: number; inserted: number; cards: FigureCard[] } | null;
  /** 文献：库缺席 → null；空表 → { total: 0 }。 */
  references: { total: number; cited: number; items: ReferenceItem[] } | null;
}

export function describePaperPackage(draft: DocumentDraft): PaperPackageView {
  const figures = Array.isArray(draft.figures)
    ? {
        total: draft.figures.length,
        inserted: summarizeFigures(draft.figures)?.inserted ?? 0,
        cards: figureCards(draft.figures),
      }
    : null;
  const references = Array.isArray(draft.references)
    ? {
        total: draft.references.length,
        cited: summarizeReferences(draft.references)?.cited ?? 0,
        items: referenceItems(draft.references),
      }
    : null;
  return { figures, references };
}
