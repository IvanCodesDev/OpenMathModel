/**
 * 输入框上方的「执行计划」面板（To-dos）。
 *
 * 取代此前渲染在首条 Agent 消息里的阶段时间线：计划不再作为聊天内容出现，
 * 而是常驻在 composer 上方、可折叠的小面板里。数据完全来自工作台快照的
 * pages 投影（服务端定义阶段数量与名称，前端不写死）；规划阶段显示思考态
 * 头部，计划揭示时列表级联浮现一次，之后的 SSE 刷新只原位更新状态，不重播
 * 动画。面板绝对定位锚在 composer 正上方，随窗格与 composer 尺寸实时复位，
 * 展开时向上生长，不挤压布局。
 */

import { t } from "../i18n/locale";

export interface TaskTodoItem {
  key: string;
  label: string;
  status: "pending" | "active" | "done" | "failed";
}

export interface TaskTodoState {
  runId: string;
  /** 规划中（含开场分析未结束）：头部显示思考态，列表暂不出现。 */
  planning: boolean;
  items: TaskTodoItem[];
}

const COLLAPSE_KEY = "openmathmodelTodosCollapsed";
const ROLL_MS = 380;

// 图标沿用参考实现的线性几何（16px viewBox 24），与产品的 Phosphor 线性系同语言。
const ICON_LIST = '<svg class="todo-list-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 5h8" /><path d="M13 12h8" /><path d="M13 19h8" /><path d="m3 17 2 2 4-4" /><path d="m3 7 2 2 4-4" /></svg>';
const ICON_CHEVRON = '<svg class="todo-chevron" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m19.5 8.25-7.5 7.5-7.5-7.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const ICON_HEAD_CHECK = '<svg class="todo-head-check" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill-rule="evenodd" clip-rule="evenodd" d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12Zm13.36-1.814a.75.75 0 1 0-1.22-.872l-3.236 4.53L9.53 12.22a.75.75 0 0 0-1.06 1.06l2.25 2.25a.75.75 0 0 0 1.14-.094l3.75-5.25Z" fill="currentColor" /></svg>';
const ICON_PIE = '<span class="todo-head-pie" aria-hidden="true"><svg class="todo-head-pie-ring" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-dasharray="2.2 4.4" stroke-linecap="round" /></svg></span>';
const ICON_DASHED = '<svg class="todo-icon todo-icon-pending" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="1.8 3.6" stroke-linecap="round" /></svg>';
const ICON_ARROW = '<svg class="todo-icon todo-icon-active" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m12.75 15 3-3m0 0-3-3m3 3h-7.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const ICON_CHECK = '<svg class="todo-icon todo-icon-done" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const ICON_FAILED = '<svg class="todo-icon todo-icon-failed" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m9.75 9.75 4.5 4.5m0-4.5-4.5 4.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';

function reduceMotion(): boolean {
  if (document.documentElement.dataset.reduceMotion === "on") return true;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function collapsedPreference(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}

function applyCollapsed(panel: HTMLElement, collapsed: boolean): void {
  panel.querySelector<HTMLElement>(".todo-head")?.setAttribute("aria-expanded", String(!collapsed));
  panel.querySelector<HTMLElement>(".todo-collapsible")?.classList.toggle("is-collapsed", collapsed);
}

/** 面板锚定：跟随 composer 的实际几何（各布局的 composer 位置/尺寸不同，不写死）。 */
function anchorPanel(panel: HTMLElement, composer: HTMLElement): void {
  const pane = composer.closest<HTMLElement>(".chat-pane") ?? composer.parentElement;
  if (!pane) return;
  const paneRect = pane.getBoundingClientRect();
  const composerRect = composer.getBoundingClientRect();
  if (composerRect.height === 0) return;
  panel.style.left = `${Math.round(composerRect.left - paneRect.left)}px`;
  panel.style.width = `${Math.round(composerRect.width)}px`;
  panel.style.bottom = `${Math.round(paneRect.bottom - composerRect.top + 10)}px`;
}

function ensurePanel(root: HTMLElement): HTMLElement | null {
  const existing = root.querySelector<HTMLElement>("[data-task-todos]");
  if (existing?.isConnected) return existing;
  const composer = root.querySelector<HTMLElement>(".chat-pane .composer");
  if (!composer?.parentElement) return null;
  const panel = document.createElement("div");
  panel.className = "task-todos";
  panel.dataset.taskTodos = "true";
  panel.hidden = true;
  panel.innerHTML = `
    <button type="button" class="todo-head" aria-expanded="true" aria-label="${t("展开或收起执行计划")}">
      <span class="todo-head-icon">${ICON_LIST}${ICON_PIE}${ICON_HEAD_CHECK}${ICON_CHEVRON}</span>
      <span class="todo-title"></span>
      <span class="todo-count" hidden></span>
    </button>
    <div class="todo-collapsible"><div class="todo-inner"><ul class="todo-list"></ul></div></div>`;
  composer.insertAdjacentElement("beforebegin", panel);
  applyCollapsed(panel, collapsedPreference());
  panel.querySelector<HTMLElement>(".todo-head")?.addEventListener("click", () => {
    const collapsed = !panel.querySelector(".todo-collapsible")?.classList.contains("is-collapsed");
    applyCollapsed(panel, collapsed);
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      // 存储不可用时折叠状态只在当前页面生效
    }
  });
  const reposition = (): void => anchorPanel(panel, composer);
  reposition();
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(reposition);
    observer.observe(composer);
    const pane = composer.closest<HTMLElement>(".chat-pane");
    if (pane) observer.observe(pane);
  }
  window.addEventListener("resize", reposition);
  return panel;
}

/** 单个字位的滚动更新：旧字上移让位新字（参考实现的 RollDigit）。 */
function rollDigit(slot: HTMLElement, next: string): void {
  if (slot.textContent === next && !slot.querySelector(".roll-inner")) return;
  const from = slot.dataset.char ?? slot.textContent ?? "";
  slot.dataset.char = next;
  if (from === next) return;
  if (reduceMotion()) {
    slot.textContent = next;
    return;
  }
  slot.textContent = "";
  const inner = document.createElement("span");
  inner.className = "roll-inner";
  const fromSpan = document.createElement("span");
  fromSpan.textContent = from;
  const toSpan = document.createElement("span");
  toSpan.textContent = next;
  inner.append(fromSpan, toSpan);
  slot.append(inner);
  requestAnimationFrame(() => requestAnimationFrame(() => inner.classList.add("on")));
  window.setTimeout(() => {
    if (slot.dataset.char === next) slot.textContent = next;
  }, ROLL_MS);
}

function updateCount(host: HTMLElement, value: string): void {
  if (host.dataset.value === value) {
    return;
  }
  const previous = host.dataset.value ?? "";
  host.dataset.value = value;
  // 位数变化（如 9/10）直接重建字位，不做滚动
  if (previous.length !== value.length) {
    host.replaceChildren(...value.split("").map(char => {
      const slot = document.createElement("span");
      slot.className = "roll-digit";
      slot.dataset.char = char;
      slot.textContent = char;
      return slot;
    }));
    return;
  }
  value.split("").forEach((char, index) => {
    const slot = host.children[index];
    if (slot instanceof HTMLElement) rollDigit(slot, char);
  });
}

function buildItem(item: TaskTodoItem, index: number): HTMLLIElement {
  const li = document.createElement("li");
  li.className = "todo-item";
  li.dataset.key = item.key;
  li.style.setProperty("--i", String(index));
  li.innerHTML = `
    <span class="todo-icon-wrap">${ICON_DASHED}${ICON_ARROW}${ICON_CHECK}${ICON_FAILED}</span>
    <span class="todo-label"></span>`;
  const label = li.querySelector<HTMLElement>(".todo-label")!;
  label.textContent = item.label;
  label.dataset.label = item.label;
  return li;
}

function applyItemStatus(li: HTMLElement, status: TaskTodoItem["status"]): void {
  li.classList.toggle("done", status === "done");
  li.classList.toggle("active", status === "active");
  li.classList.toggle("failed", status === "failed");
  li.querySelector(".todo-icon-pending")?.classList.toggle("on", status === "pending");
  li.querySelector(".todo-icon-active")?.classList.toggle("on", status === "active");
  li.querySelector(".todo-icon-done")?.classList.toggle("on", status === "done");
  li.querySelector(".todo-icon-failed")?.classList.toggle("on", status === "failed");
}

/**
 * 用最新快照同步面板。规划中只显示思考态头部；计划就绪后列表级联浮现一次
 * （运行中途刷新页面不重播），此后状态原位翻转（图标淡变、进行中扫光）。
 */
export function renderTaskTodos(root: HTMLElement, state: TaskTodoState): void {
  const panel = ensurePanel(root);
  if (!panel) return;
  if (panel.dataset.runId !== state.runId) {
    panel.dataset.runId = state.runId;
    delete panel.dataset.phase;
    const staleList = panel.querySelector<HTMLElement>(".todo-list");
    if (staleList) {
      staleList.replaceChildren();
      delete staleList.dataset.signature;
      staleList.classList.remove("todo-reveal");
    }
  }

  const head = panel.querySelector<HTMLElement>(".todo-head")!;
  const title = panel.querySelector<HTMLElement>(".todo-title")!;
  const count = panel.querySelector<HTMLElement>(".todo-count")!;
  const list = panel.querySelector<HTMLElement>(".todo-list")!;
  panel.hidden = false;

  if (state.planning) {
    panel.dataset.phase = "planning";
    panel.dataset.state = "planning";
    title.textContent = t("正在思考并规划执行步骤…");
    title.classList.add("thinking-shimmer");
    count.hidden = true;
    return;
  }

  // 规划 → 揭示的一次性过渡：只有从 planning 落到就绪才播放级联
  const revealNow = panel.dataset.phase === "planning";
  panel.dataset.phase = "revealed";
  title.textContent = t("执行计划");
  title.classList.remove("thinking-shimmer");

  const signature = state.items.map(item => `${item.key}:${item.label}`).join("|");
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.classList.toggle("todo-reveal", revealNow && !reduceMotion());
    list.replaceChildren(...state.items.map((item, index) => buildItem(item, index)));
  }
  Array.from(list.children).forEach((li, index) => {
    const item = state.items[index];
    if (li instanceof HTMLElement && item) applyItemStatus(li, item.status);
  });

  const total = state.items.length;
  const done = state.items.filter(item => item.status === "done").length;
  const failed = state.items.some(item => item.status === "failed");
  const allDone = total > 0 && done === total;
  panel.dataset.state = allDone ? "done" : "running";
  count.hidden = total === 0;
  updateCount(count, `${done}/${total}`);
  const pie = panel.querySelector<HTMLElement>(".todo-head-pie");
  pie?.style.setProperty("--todo-pie", `${total > 0 ? Math.round((done / total) * 100) : 0}%`);
  head.classList.toggle("has-failure", failed);
}

/** 切换/退出运行时隐藏面板（演示页与无运行页面不显示）。 */
export function hideTaskTodos(root: HTMLElement): void {
  const panel = root.querySelector<HTMLElement>("[data-task-todos]");
  if (panel) panel.hidden = true;
}
