/**
 * 建模方案页对 G1 决策台账与方案卡「实现语言」的纯数据整形（不碰 DOM，node --test 直接断言）。
 *
 * plan-proposal 契约（H3 切片 6）多了两样东西：
 * - `decision`：用户对**这一版**方案的正向确认（采用推荐案 / 改用某备选案），服务端已按
 *   「adopt 目标 → 推荐案 → 首个」折算出 `chosen_plan_id`，与实验阶段实际所用方案同一规则；
 *   等待审批、无人值守、旧运行时为 null，页面沿用「建议采用」口径。
 * - `plans[].language`：实现语言小写标识（python / r / matlab …），随 G1 一并确认；
 *   2026-09-06 之前的运行没有该键或为 null，页面不显示语言而不是猜一个。
 *
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。
 */

import type { PlanProposal } from "@openmathmodel/contracts";

export type PlanOption = PlanProposal["plans"][number];
export type PlanDecision = NonNullable<PlanProposal["decision"]>;

/** G1 选项 id 约定（与 skills `_g1_review` / backend `_plan_decision` 一致）。 */
export const G1_APPROVE_OPTION_ID = "approve";
export const G1_ADOPT_OPTION_PREFIX = "adopt:";

/** 实现语言小写标识 → 展示名。未收录的标识原样展示，不臆造品牌写法。 */
export const LANGUAGE_LABELS: Record<string, string> = {
  python: "Python",
  r: "R",
  matlab: "MATLAB",
  octave: "Octave",
  julia: "Julia",
  baltamatica: "北太天元",
};

export function languageLabel(code: string | null | undefined): string | null {
  const raw = (code ?? "").trim();
  if (!raw) return null;
  return LANGUAGE_LABELS[raw.toLowerCase()] ?? raw;
}

export interface PlanDecisionView {
  /** 用户确认采用的方案 id；一定是 plans 里存在的 id。 */
  chosenPlanId: string;
  /** 是否落在推荐案上（approve，或 adopt 的正好是推荐案）。 */
  asRecommended: boolean;
  optionId: string;
  actor: string;
  /** 用户确认时的备注原文（去首尾空白）；没写为 null。 */
  comment: string | null;
  /** ISO-8601（UTC）；调用方按界面语言格式化。 */
  resolvedAt: string;
}

type ProposalSlice = Pick<PlanProposal, "plans" | "recommended_plan_id"> & Partial<Pick<PlanProposal, "decision">>;

/** 推荐案；推荐 id 对不上任何方案（不该发生，契约有校验）时退到首个。 */
export function recommendedPlan(proposal: ProposalSlice): PlanOption | undefined {
  return proposal.plans.find(plan => plan.id === proposal.recommended_plan_id) ?? proposal.plans[0];
}

/**
 * 台账 → 页面视图。服务端给的 `chosen_plan_id` 直接用；万一对不上任何方案（手工修过
 * 的数据 / 旧投影），按同一规则本地折算一次：adopt 目标 → 推荐案 → 首个，绝不落空。
 */
export function describePlanDecision(proposal: ProposalSlice): PlanDecisionView | null {
  const decision = proposal.decision;
  if (!decision || proposal.plans.length === 0) return null;
  const ids = new Set(proposal.plans.map(plan => plan.id));
  let chosen = decision.chosen_plan_id;
  if (!ids.has(chosen)) {
    const adopted = decision.option_id.startsWith(G1_ADOPT_OPTION_PREFIX)
      ? decision.option_id.slice(G1_ADOPT_OPTION_PREFIX.length)
      : "";
    chosen = ids.has(adopted) ? adopted : (recommendedPlan(proposal) as PlanOption).id;
  }
  const comment = (decision.comment ?? "").trim();
  return {
    chosenPlanId: chosen,
    asRecommended: chosen === proposal.recommended_plan_id,
    optionId: decision.option_id,
    actor: decision.actor,
    comment: comment || null,
    resolvedAt: decision.resolved_at,
  };
}

/** 页面默认聚焦的方案：有台账 → 用户确认的那一案；否则推荐案。 */
export function focusedPlan(proposal: ProposalSlice): PlanOption | undefined {
  const decision = describePlanDecision(proposal);
  if (decision) return proposal.plans.find(plan => plan.id === decision.chosenPlanId);
  return recommendedPlan(proposal);
}

/**
 * 面板幂等签名：方案正文按 updated_at 变，台账落地不改 updated_at（那是产出时间），
 * 所以签名要把 resolved_at 一起带上——否则用户确认后页面停在「建议采用」不刷新。
 */
export function planProposalStamp(proposal: Pick<PlanProposal, "updated_at"> & Partial<Pick<PlanProposal, "decision">>): string {
  return `${proposal.updated_at}|${proposal.decision?.resolved_at ?? ""}`;
}
