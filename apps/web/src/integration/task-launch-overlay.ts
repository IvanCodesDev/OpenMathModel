/**
 * 任务创建的过场遮罩：接待判定放行后接管整个视口，把「创建项目 → 上传附件 →
 * 启动 Agent 工作流」的真实进度演成一段有节奏的过场，成功后打勾定格再进入
 * 运行工作台。
 *
 * 时序约定（task-start-controller 负责调用）：
 * - 接待判定通过前不出现——闲聊/缺题面转首页对话的路径完全不受影响；
 * - 每个阶段真实开始时 setPhase()，跳过的阶段（无附件）不显示对应步骤；
 * - 成功走 succeed(cb)：打勾动画定格后回调导航；失败走 dismiss() 淡出，
 *   错误信息仍由状态行呈现。
 *
 * 视觉与执行计划面板同语言（线性几何图标、黑白灰、扫光），全部动效尊重
 * prefers-reduced-motion。遮罩本体 aria-hidden：读屏通道仍是页面状态行
 * （role=status），不做重复播报。
 */

import { t } from "../i18n/locale";

export type TaskLaunchPhase = "project" | "attachments" | "agent";

export interface TaskLaunchOverlay {
  setPhase(phase: TaskLaunchPhase): void;
  /** 进度细节（如「正在上传附件 2/3…」），显示在步骤列表下方。 */
  setNote(text: string): void;
  /** 成功收尾：全部步骤打勾、主视觉变为对勾，定格后回调（通常是导航）。 */
  succeed(onDone: () => void): void;
  /** 失败/中断：淡出并移除遮罩，页面交互交还状态行。 */
  dismiss(): void;
}

const PHASE_ORDER: TaskLaunchPhase[] = ["project", "attachments", "agent"];

const PHASE_LABELS: Record<TaskLaunchPhase, string> = {
  project: "创建项目",
  attachments: "上传附件",
  agent: "启动 Agent 工作流",
};

/** 与执行计划面板同款的线性几何图标（16px viewBox 24）。 */
const ICON_PENDING = '<svg class="launch-icon launch-icon-pending" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="1.8 3.6" stroke-linecap="round" /></svg>';
const ICON_ACTIVE = '<svg class="launch-icon launch-icon-active" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M12 3a9 9 0 1 1-6.36 2.64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" /></svg>';
const ICON_DONE = '<svg class="launch-icon launch-icon-done" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';

const VISUAL_RING = '<svg class="launch-visual-ring" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="20" fill="none" stroke="currentColor" stroke-width="2.6" opacity=".16" /><path d="M24 4a20 20 0 0 1 20 20" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" /></svg>';
const VISUAL_CHECK = '<svg class="launch-visual-check" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22" fill="currentColor" /><path class="launch-visual-tick" d="m15.5 24.5 6 6 11-12" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round" /></svg>';

function reduceMotion(): boolean {
  if (document.documentElement.dataset.reduceMotion === "on") return true;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

export function showTaskLaunchOverlay(options: { hasAttachments: boolean }): TaskLaunchOverlay {
  const phases = PHASE_ORDER.filter(phase => phase !== "attachments" || options.hasAttachments);

  const overlay = document.createElement("div");
  overlay.className = "task-launch-overlay";
  overlay.dataset.state = "working";
  overlay.setAttribute("aria-hidden", "true");
  overlay.innerHTML = `
    <div class="task-launch-panel">
      <div class="task-launch-visual">${VISUAL_RING}${VISUAL_CHECK}</div>
      <h2 class="task-launch-title thinking-shimmer">${t("正在创建任务…")}</h2>
      <ul class="task-launch-steps">
        ${phases.map(phase => `
          <li class="task-launch-step" data-launch-step="${phase}">
            <span class="launch-icon-wrap">${ICON_PENDING}${ICON_ACTIVE}${ICON_DONE}</span>
            <span class="launch-step-label">${t(PHASE_LABELS[phase])}</span>
          </li>`).join("")}
      </ul>
      <p class="task-launch-note"></p>
    </div>`;
  document.body.append(overlay);
  // 强制一帧布局后再挂入场类，transition 才会播放
  void overlay.offsetHeight;
  overlay.classList.add("is-open");

  const title = overlay.querySelector<HTMLElement>(".task-launch-title")!;
  const note = overlay.querySelector<HTMLElement>(".task-launch-note")!;
  const steps = new Map(
    phases.map(phase => [
      phase,
      overlay.querySelector<HTMLElement>(`[data-launch-step="${phase}"]`)!,
    ]),
  );
  let settled = false;

  const applyStepState = (step: HTMLElement, state: "pending" | "active" | "done"): void => {
    step.dataset.stepState = state;
  };

  return {
    setPhase(phase) {
      if (settled || !steps.has(phase)) return;
      const activeIndex = phases.indexOf(phase);
      phases.forEach((candidate, index) => {
        applyStepState(
          steps.get(candidate)!,
          index < activeIndex ? "done" : index === activeIndex ? "active" : "pending",
        );
      });
    },
    setNote(text) {
      if (!settled) note.textContent = text;
    },
    succeed(onDone) {
      if (settled) return;
      settled = true;
      phases.forEach(phase => applyStepState(steps.get(phase)!, "done"));
      overlay.dataset.state = "done";
      title.classList.remove("thinking-shimmer");
      title.textContent = t("任务已创建");
      note.textContent = t("正在进入运行工作台…");
      // 打勾定格一拍再导航：过场有收尾感，又不拖慢整体节奏
      window.setTimeout(onDone, reduceMotion() ? 120 : 880);
    },
    dismiss() {
      if (settled) return;
      settled = true;
      overlay.classList.remove("is-open");
      window.setTimeout(() => overlay.remove(), reduceMotion() ? 0 : 220);
    },
  };
}
