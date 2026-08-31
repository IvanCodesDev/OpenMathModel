/**
 * 审批门选项的取舍规则（ADR-0013 第 14 项）。
 *
 * 单独成模块是为了让这段策略可被单测直接覆盖——它决定的是「用户按下确认时到底把
 * 哪个 option_id 提交给服务端」，修订门下这一下会决定从哪个阶段整段重跑、花掉一份
 * 运行配额，是本切片里最不能靠肉眼保证的一段。DOM 渲染留在 controller。
 */

import type { ModelingWorkspaceView } from "@openmathmodel/contracts";

type AgentAction = ModelingWorkspaceView["agent"]["action"];
type ApprovalProjection = NonNullable<ModelingWorkspaceView["pending_approval"]>;

/** 审批门里「否决/撤回」那一项的固定 id（与服务端 engine_glue.REJECT_OPTION_ID 一致）。 */
export const REJECT_OPTION_ID = "reject";

/** 用户在某道审批门里手选的那一项。带 approvalId 是为了让选择随门失效：
 *  门一换（下一轮，或另一道闸）id 就对不上，自动回落服务端预选，不会串到别的门上。 */
export interface ChosenApprovalOption {
  approvalId: string;
  optionId: string;
}

/**
 * 是否要把选项列表摆出来让用户挑。
 *
 * 只在**正向选项多于一个**时摆——目前即修订门（六个「从 X 重做」+ 撤回）。G1/G2 那种
 * 恰一个正向选项的闸门维持原样：一个「确认并继续」按钮就够，多摆一排单选纯是噪音。
 * 跨屏时 CTA 已被 actionForScreen 换成「前往某阶段」，此时也不摆——选了无处可提交。
 */
export function shouldOfferOptions(
  approval: ApprovalProjection | null,
  action: AgentAction,
): boolean {
  if (approval === null || action.kind !== "approve") return false;
  return approval.options.filter(option => option.id !== REJECT_OPTION_ID).length > 1;
}

/**
 * 把用户手选的项覆盖进动作。服务端只把建议项标成 recommended 并由 `_preferred_option`
 * 预选进 CTA，用户改选后以手选为准。
 *
 * 选中「撤回」时按钮文案换成该项自己的 label：沿用「确认重做起点」去执行撤回，
 * 是按钮说一套做一套。
 */
export function applyChosenOption(
  approval: ApprovalProjection | null,
  action: AgentAction,
  chosen: ChosenApprovalOption | null,
): AgentAction {
  if (action.kind !== "approve" || chosen === null) return action;
  if (chosen.approvalId !== action.approval_id) return action;
  const option = approval?.options.find(item => item.id === chosen.optionId);
  if (option === undefined) return action;
  return {
    ...action,
    option_id: option.id,
    ...(option.id === REJECT_OPTION_ID ? { label: option.label } : {}),
  };
}
