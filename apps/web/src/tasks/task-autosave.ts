/**
 * 「自动保存任务」的执行引擎：每 30 秒把正在编辑的现场落盘。
 *
 * 保存两类内容：
 * - 论文编辑器正文：localStorage，按 project_id 区分，跨会话保留；
 * - 工作台输入框里未发送的对话草稿：sessionStorage，按屏幕与 run_id 区分。
 *
 * 首页输入框不归这里管——新任务草稿在 task-start-controller 里逐键即时保存。
 * 开关状态每个周期重新读取，设置中心里改动后无需刷新即可生效。
 */

import { autoSaveEnabled } from "../preferences/task-preferences";
import type { ScreenId } from "../types/screens";

export const AUTOSAVE_INTERVAL_MS = 30_000;

const PAPER_KEY_PREFIX = "openmathmodel.paperDraft.v1.";
const CHAT_KEY_PREFIX = "openmathmodel.chatDraft.v1.";
const AUTOSAVE_SCREENS = new Set<ScreenId>(["running", "data", "model", "experiments", "editor", "complete"]);
const PROJECT_ID_PATTERN = /^proj_[0-9a-f]{32}$/;
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;

let timer: number | undefined;
let boundScreen: ScreenId | undefined;
let lastPaperHtml: string | undefined;
let pagehideBound = false;

function urlScope(key: "project_id" | "run_id", pattern: RegExp): string {
  const value = new URL(window.location.href).searchParams.get(key) ?? "";
  return pattern.test(value) ? value : "demo";
}

function paperKey(): string {
  return PAPER_KEY_PREFIX + urlScope("project_id", PROJECT_ID_PATTERN);
}

function chatKey(index: number): string {
  return `${CHAT_KEY_PREFIX}${boundScreen}.${index}.${urlScope("run_id", RUN_ID_PATTERN)}`;
}

function paperElement(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.editor-page[contenteditable="true"]');
}

function composerTextareas(): HTMLTextAreaElement[] {
  return Array.from(document.querySelectorAll<HTMLTextAreaElement>(".composer textarea"));
}

/** 编辑器顶栏的保存状态芯片；非编辑器页面没有该节点时静默跳过。 */
function markSaved(time: Date): void {
  const chip = document.querySelector<HTMLElement>(".saved-state");
  if (!chip) return;
  const icon = document.createElement("i");
  icon.className = "ph ph-check-circle";
  icon.setAttribute("aria-hidden", "true");
  const label = ` 已自动保存 ${time.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
  chip.replaceChildren(icon, label);
}

function savePaper(): void {
  const editor = paperElement();
  if (!editor) return;
  const html = editor.innerHTML;
  if (html === lastPaperHtml) return;
  try {
    localStorage.setItem(paperKey(), JSON.stringify({ html, saved_at: Date.now() }));
    lastPaperHtml = html;
    markSaved(new Date());
  } catch {
    // 存储满或被禁用时跳过本轮，不打断编辑。
  }
}

function restorePaper(): void {
  const editor = paperElement();
  if (!editor) return;
  try {
    const raw = localStorage.getItem(paperKey());
    if (!raw) return;
    const payload = JSON.parse(raw) as { html?: unknown; saved_at?: unknown };
    if (typeof payload.html !== "string" || !payload.html.trim()) return;
    lastPaperHtml = payload.html;
    if (payload.html === editor.innerHTML) return;
    // 恢复的是用户自己浏览器里存下的编辑现场，等价于其离开前的页面状态。
    editor.innerHTML = payload.html;
    if (typeof payload.saved_at === "number" && Number.isFinite(payload.saved_at)) {
      markSaved(new Date(payload.saved_at));
    }
  } catch {
    // 记录损坏时按没有草稿处理，保留页面原有内容。
  }
}

function saveChatDrafts(): void {
  composerTextareas().forEach((textarea, index) => {
    try {
      // 发送后输入框被清空，同步清掉记录，避免下次进来又冒出已发送的旧稿。
      if (textarea.value.trim()) sessionStorage.setItem(chatKey(index), textarea.value);
      else sessionStorage.removeItem(chatKey(index));
    } catch {
      // 会话存储不可用时草稿只活在页面里。
    }
  });
}

function restoreChatDrafts(): void {
  composerTextareas().forEach((textarea, index) => {
    if (textarea.value) return;
    try {
      const saved = sessionStorage.getItem(chatKey(index));
      if (saved) textarea.value = saved;
    } catch {
      // 同上。
    }
  });
}

function tick(): void {
  if (!autoSaveEnabled()) return;
  saveChatDrafts();
  savePaper();
}

/** 每次切屏调用；非工作台屏幕只负责清掉上一屏的定时器。 */
export function mountTaskAutosave(screen: ScreenId): void {
  if (timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
  boundScreen = screen;
  lastPaperHtml = undefined;
  if (!AUTOSAVE_SCREENS.has(screen)) return;

  if (autoSaveEnabled()) {
    restoreChatDrafts();
    restorePaper();
  }
  timer = window.setInterval(tick, AUTOSAVE_INTERVAL_MS);
  if (!pagehideBound) {
    pagehideBound = true;
    // 整页跳转前补一次落盘，30 秒周期内的最后编辑才不会丢。
    window.addEventListener("pagehide", tick);
  }
}
