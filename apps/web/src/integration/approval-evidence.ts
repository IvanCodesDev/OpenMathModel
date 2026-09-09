/**
 * G4 定稿闸门卡片的内嵌证据（H5 切片 s30）：审批选项摆在用户面前时，同一屏给出「决定所需的事实」——
 * 终稿审计分类计数与前几处发现、已插入的论文附图缩略图、参考文献引用计数、交付记录的五项一致性检查。
 * 纯数据整形（不碰 DOM，node --test 直接断言）：输入是 stage-outputs 里的 document_draft 与
 * delivery_manifest，输出只有中文源串 / 契约原文，调用方按片段 t() 后拼接。
 *
 * 只在成果清单的交付记录处于「等待确认交付」（= G4 挂起）且带审批 id 时给证据，其余一律 null——
 * 页面据此把证据块挂到对应审批卡上、门一解决就摘掉。
 */

import type { DeliveryManifest, DocumentDraft } from "@openmathmodel/contracts";

import { describeDelivery } from "./delivery-record";
import { FINDING_KIND_REASONS, summarizeFindingKinds } from "./paper-audit";
import type { FindingKindCount } from "./paper-audit";
import { describePaperPackage } from "./result-figures";
import type { FigureCard } from "./result-figures";

export const EVIDENCE_FINDINGS_LIMIT = 3;
export const EVIDENCE_FIGURES_LIMIT = 4;

export interface EvidenceFinding {
  scope: string;
  kind: string;
  /** 违规 token 取样（契约原文）。 */
  numbers: string[];
  /** 类型原因（中文源串，调用方 t()）；契约外 kind 退回节点 detail。 */
  reason: string;
  detail: string;
}

export interface ApprovalEvidence {
  approvalId: string;
  /** 审计：null = 论文未做终稿审计（旧运行）。 */
  audit: {
    total: number;
    kinds: FindingKindCount[];
    /** 前几处发现（按节点给出的顺序：数值 → 图表 → 引用）。 */
    top: EvidenceFinding[];
    more: number;
  } | null;
  /** 图件：null = 清单缺席（旧运行）。 */
  figures: {
    total: number;
    inserted: number;
    /** 已插入正文的图在前、按编号，最多 EVIDENCE_FIGURES_LIMIT 张有产物 id 的做缩略图。 */
    cards: FigureCard[];
    more: number;
    /** 有清单却没有可预览的图（全部无产物 id）。 */
    previewable: number;
  } | null;
  references: { total: number; cited: number } | null;
  checks: {
    passed: number;
    total: number;
    /** 未通过项的 label / detail（契约原文）。 */
    failed: { id: string; label: string; detail: string }[];
  } | null;
}

/** 交付记录不是「等待确认交付」（没挂 G4 / 已解决 / 模拟链 / 旧接口）→ null。 */
export function describeApprovalEvidence(
  draft: DocumentDraft | null | undefined,
  manifest: DeliveryManifest | null | undefined,
): ApprovalEvidence | null {
  const delivery = manifest ? describeDelivery(manifest) : null;
  const approvalId = manifest?.delivery?.approval_id ?? null;
  if (!delivery || delivery.status !== "pending_confirmation" || !approvalId) return null;

  let audit: ApprovalEvidence["audit"] = null;
  if (draft && Array.isArray(draft.audit_findings)) {
    const findings = draft.audit_findings;
    audit = {
      total: findings.length,
      kinds: summarizeFindingKinds(findings),
      top: findings.slice(0, EVIDENCE_FINDINGS_LIMIT).map(finding => ({
        scope: finding.scope,
        kind: finding.kind,
        numbers: [...finding.numbers],
        reason: FINDING_KIND_REASONS[finding.kind] ?? finding.detail,
        detail: finding.detail,
      })),
      more: Math.max(0, findings.length - EVIDENCE_FINDINGS_LIMIT),
    };
  }

  let figures: ApprovalEvidence["figures"] = null;
  const pkg = draft ? describePaperPackage(draft) : { figures: null, references: null };
  if (pkg.figures) {
    const ordered = [...pkg.figures.cards].sort((a, b) => {
      const byInserted = Number(b.inserted === true) - Number(a.inserted === true);
      return byInserted !== 0 ? byInserted : a.number - b.number;
    });
    const previewable = ordered.filter(card => card.imageUrl);
    figures = {
      total: pkg.figures.total,
      inserted: pkg.figures.inserted,
      cards: previewable.slice(0, EVIDENCE_FIGURES_LIMIT),
      more: Math.max(0, previewable.length - EVIDENCE_FIGURES_LIMIT),
      previewable: previewable.length,
    };
  }

  const references = pkg.references ? { total: pkg.references.total, cited: pkg.references.cited } : null;
  const checks = {
    passed: delivery.summary.passed,
    total: delivery.summary.total,
    failed: delivery.checks
      .filter(check => !check.passed)
      .map(check => ({ id: check.id, label: check.label, detail: check.detail })),
  };
  return { approvalId, audit, figures, references, checks };
}

/** 证据块的幂等戳：同一道门 + 同一版草稿 / 清单只渲染一次。 */
export function approvalEvidenceStamp(
  evidence: ApprovalEvidence,
  draft: DocumentDraft | null | undefined,
  manifest: DeliveryManifest,
): string {
  return `${evidence.approvalId}:${draft?.updated_at ?? "-"}:${manifest.updated_at}`;
}
