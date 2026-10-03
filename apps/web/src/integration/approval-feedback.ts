/**
 * 审批卡的「上一轮反馈」（H4）：回退重做轮里开出的闸门，服务端把反馈包摘成几行写进
 * pending_approval.description（backend/api/omm_api/approval_feedback.py；首轮闸门为 null）。
 * 第一行是来由（从哪一段起回退重做、谁触发的、第几轮），「- 」开头的行是逐条事实。
 * 纯数据整形（不碰 DOM，node --test 直接断言）；正文是服务端拼好的中文原文，渲染在 controller。
 */

import type { ModelingWorkspaceView } from "@openmathmodel/contracts";

type ApprovalProjection = NonNullable<ModelingWorkspaceView["pending_approval"]>;

export interface ApprovalFeedback {
  approvalId: string;
  /** 来由（不以「- 」开头的行，多行时以空格相接）。 */
  lead: string;
  /** 逐条事实，已去掉「- 」前缀。 */
  items: string[];
  /** 幂等戳：同一道门、同一份正文只渲染一次。 */
  stamp: string;
}

/** 首轮闸门（description 为空）或没有待批的门 → null。 */
export function describeApprovalFeedback(
  approval: ApprovalProjection | null | undefined,
): ApprovalFeedback | null {
  const text = approval?.description?.trim();
  if (!approval || !text) return null;
  const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const isItem = (line: string): boolean => line === "-" || line.startsWith("- ");
  const items = lines.filter(isItem).map(line => line.slice(1).trim()).filter(Boolean);
  const lead = lines.filter(line => !isItem(line)).join(" ");
  if (!lead && !items.length) return null;
  return { approvalId: approval.id, lead, items, stamp: `${approval.id}:${text}` };
}
