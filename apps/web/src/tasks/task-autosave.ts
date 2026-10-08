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

import { saveHistoryEnabled } from "../preferences/privacy-preferences";
import { autoSaveEnabled } from "../preferences/task-preferences";
import type { ScreenId } from "../types/screens";
import { readUserPaperDraft, writeUserPaperDraft } from "./paper-draft";

export const AUTOSAVE_INTERVAL_MS = 30_000;

const CHAT_KEY_PREFIX = "openmathmodel.chatDraft.v1.";
const AUTOSAVE_SCREENS = new Set<ScreenId>(["running", "data", "model", "experiments", "editor", "complete"]);
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;

let timer: number | undefined;
let boundScreen: ScreenId | undefined;
let lastPaperHtml: string | undefined;
let pagehideBound = false;

function urlScope(key: "run_id", pattern: RegExp): string {
  const value = new URL(window.location.href).searchParams.get(key) ?? "";
  return pattern.test(value) ? value : "demo";
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
  // 只保存用户亲手编辑过的现场（编辑器在输入时打 data-user-edited 标记）。
  // 演示模板或 Agent 填充的正文不算草稿：否则打开页面 30 秒后模板内容就会
  // 伪装成「本机草稿」，反过来永远挡住真实论文正文的渲染。
  if (editor.dataset.userEdited !== "true") return;
  const html = editor.innerHTML;
  if (html === lastPaperHtml) return;
  // 存储满或被禁用时跳过本轮，不打断编辑。
  if (!writeUserPaperDraft(html)) return;
  lastPaperHtml = html;
  markSaved(new Date());
}

function restorePaper(): void {
  const editor = paperElement();
  if (!editor) return;
  const draft = readUserPaperDraft();
  if (!draft) return;
  lastPaperHtml = draft.html;
  editor.dataset.userEdited = "true";
  if (draft.html === editor.innerHTML) return;
  // 恢复的是用户自己浏览器里存下的编辑现场，等价于其离开前的页面状态。
  editor.innerHTML = draft.html;
  if (draft.savedAt !== null) markSaved(new Date(draft.savedAt));
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
  // 「保存任务历史」（数据与隐私）关闭时，对话与正文草稿一并停止落盘。
  if (!autoSaveEnabled() || !saveHistoryEnabled()) return;
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

  if (autoSaveEnabled() && saveHistoryEnabled()) {
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
