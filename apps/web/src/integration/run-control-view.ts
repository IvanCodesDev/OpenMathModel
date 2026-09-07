/**
 * 对话即控制面（ADR-0018 / ADR-0019）的页面侧纯函数：把服务端在托管轮里执行 /
 * 提议的运行控制动作（`action` 事件 = `meta.actions[]` 条目）变成回复轨迹行。
 *
 * 标题一律是静态短语（语言切换按整句翻译），阶段名 / 选项名进后缀，服务端的
 * 说明进详情。不依赖 DOM，node --test 直接跑。
 */

import type { ConversationTraceRow } from "../tasks/conversation-log";
import type { RunControlAction } from "./chat-turns-api";

/** 「确认执行」按钮等价于用户回一句这个词——服务端按上一轮提案执行。 */
export const CONFIRM_REPLY_TEXT = "确认";

const KIND_LABELS: Record<string, string> = {
  retry: "重试阶段",
  resume: "恢复任务",
  pause: "暂停任务",
  cancel: "取消任务",
  approve: "选定审批选项",
  reject: "退回待确认事项",
  revision: "受理修改要求",
  redo: "从阶段重做",
};

const EXECUTED_TITLES: Record<string, string> = {
  retry: "已重试阶段",
  resume: "已恢复任务",
  pause: "已暂停任务",
  cancel: "已取消任务",
  approve: "已选定审批选项",
  reject: "已退回待确认事项",
  revision: "已受理修改要求",
  redo: "已从阶段重做",
};

export const PROPOSED_TITLE = "等待你确认操作";
export const REJECTED_TITLE = "当前状态不允许该操作";
export const DISMISSED_TITLE = "已放弃提案";
const FALLBACK_TITLE = "已执行运行操作";

/** 动作类别的中文短语（后缀 / 详情用）。 */
export function actionKindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** 轨迹行标题：静态短语，便于整句翻译。 */
export function actionTraceTitle(action: RunControlAction): string {
  switch (action.status) {
    case "proposed":
      return PROPOSED_TITLE;
    case "rejected":
      return REJECTED_TITLE;
    case "dismissed":
      return DISMISSED_TITLE;
    default:
      return EXECUTED_TITLES[action.kind] ?? FALLBACK_TITLE;
  }
}

function executedSuffix(action: RunControlAction): string {
  switch (action.kind) {
    case "retry":
    case "resume":
    case "pause":
    case "redo":
      return action.stage_label ? ` · ${action.stage_label}` : "";
    case "approve":
      return action.option_label ? ` · ${action.option_label}` : "";
    case "revision": {
      const parts: string[] = [];
      if (typeof action.round === "number") parts.push(`第 ${action.round} 轮`);
      if (action.stage_label) parts.push(action.stage_label);
      return parts.length ? ` · ${parts.join(" · ")}` : "";
    }
    default:
      return "";
  }
}

/** 轨迹行后缀：阶段名 / 选项名 / 动作类别等动态部分。 */
export function actionTraceSuffix(action: RunControlAction): string {
  if (action.status === "executed") return executedSuffix(action);
  // 重做提案要让用户一眼看到会回到哪个阶段，再决定确认还是放弃
  if (action.kind === "redo" && action.stage_label) {
    return ` · ${actionKindLabel(action.kind)} · ${action.stage_label}`;
  }
  return ` · ${actionKindLabel(action.kind)}`;
}

/** 轨迹行图标（Phosphor 名称）。 */
export function actionTraceIcon(action: RunControlAction): string {
  switch (action.status) {
    case "proposed":
      return "question";
    case "rejected":
      return "warning-circle";
    case "dismissed":
      return "x-circle";
    default:
      return "check-circle";
  }
}

/** 服务端回执 → 回复轨迹行（与附件行 / 难度行同构，落盘后原样重建）。 */
export function actionTraceRow(action: RunControlAction): ConversationTraceRow {
  const detailParts = [action.message?.trim() ?? ""];
  if (action.status === "proposed" && action.confirm_hint) detailParts.push(action.confirm_hint.trim());
  const detail = detailParts.filter(Boolean).join("\n");
  return {
    icon: actionTraceIcon(action),
    title: actionTraceTitle(action),
    suffix: actionTraceSuffix(action),
    ...(detail ? { detail } : {}),
  };
}

/** 动作是否已真正改变了运行（页面据此让工作台重拉快照并重接事件流）。 */
export function isExecutedAction(action: RunControlAction): boolean {
  return action.status === "executed";
}

/** 这一轮结束时是否还留着待确认的提案（只看最后一条：提案只对紧接着的下一轮有效）。 */
export function lastPendingProposal(actions: RunControlAction[] | undefined | null): RunControlAction | null {
  if (!actions?.length) return null;
  const last = actions[actions.length - 1];
  return last.status === "proposed" ? last : null;
}
