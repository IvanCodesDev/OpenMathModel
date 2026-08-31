/**
 * 设置中心「数据与隐私」与服务端的同步，以及各行为闸门的运行时读取器。
 *
 * 面板的九个控件随整张设置表单落在本机 localStorage，但设置本体存服务端
 * （users.privacy_settings）：任务保留与文件缓存清理由服务端后台清扫执行，
 * 通知与历史开关则由本模块的读取器在各触发点把关。打开面板时以服务端值
 * 回填显示，保存时整体推送；开关布尔值同时并入本机设置，读取器立即生效。
 */

import { ApiError, authApi, type PrivacySettings } from "../auth/api";
import { clearAllChatSessions } from "../tasks/chat-sessions";
import { clearAllConversationLogs } from "../tasks/conversation-log";
import { forgetLastTask } from "../tasks/last-task-record";

const SETTINGS_KEY = "openmathmodelSettings";
const SYNC_GUARD_KEY = "openmathmodel.privacySynced";

export const DEFAULT_PRIVACY_SETTINGS: PrivacySettings = {
  save_history: true,
  local_first: true,
  model_training: false,
  retention: "forever",
  file_cache: "days_30",
  notify_task_done: true,
  notify_budget: true,
  notify_security: true,
  email_digest: false,
};

/** 面板开关名 ↔ 服务端字段名（下拉两项单独解析）。 */
const TOGGLE_FIELDS = [
  ["saveHistory", "save_history"],
  ["localFirst", "local_first"],
  ["modelTraining", "model_training"],
  ["notifyTaskDone", "notify_task_done"],
  ["notifyBudget", "notify_budget"],
  ["notifySecurity", "notify_security"],
  ["emailDigest", "email_digest"],
] as const;

function savedSettings(): Record<string, unknown> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
  } catch {
    return {};
  }
}

// ── 行为闸门读取器（默认值与面板初始状态一致） ─────────────────────

/** 保存任务历史：本机的「最近任务」记录与对话/论文草稿落盘都随它。 */
export function saveHistoryEnabled(): boolean {
  return savedSettings().saveHistory !== false;
}

/** 敏感文件优先本地处理：能在浏览器完成的附件解析不再上传服务端。 */
export function localFirstEnabled(): boolean {
  return savedSettings().localFirst !== false;
}

/** 任务完成通知：运行完成/失败时的系统通知。 */
export function notifyTaskDoneEnabled(): boolean {
  return savedSettings().notifyTaskDone !== false;
}

/** 预算与限额提醒：费用达到阈值时的提醒。 */
export function notifyBudgetEnabled(): boolean {
  return savedSettings().notifyBudget !== false;
}

/** 账户安全提醒：密码、双重验证或登录设备变化时的提醒。 */
export function notifySecurityEnabled(): boolean {
  return savedSettings().notifySecurity !== false;
}

// ── 表单值 ↔ 服务端值 ────────────────────────────────────────────

/** 「90 天」「30 天」「任务完成后删除」「永久保留」→ 服务端保留策略。 */
export function parseRetention(label: unknown): PrivacySettings["retention"] {
  const text = String(label ?? "");
  if (/90/.test(text)) return "days_90";
  if (/30/.test(text)) return "days_30";
  if (/完成|complete/i.test(text)) return "on_complete";
  return "forever";
}

/** 「30 天后清理」「7 天后清理」「关闭任务时清理」→ 服务端缓存策略。 */
export function parseFileCache(label: unknown): PrivacySettings["file_cache"] {
  const text = String(label ?? "");
  if (/30/.test(text)) return "days_30";
  if (/7/.test(text)) return "days_7";
  if (/关闭|close/i.test(text)) return "on_close";
  return "days_30";
}

/** 设置表单收集值 → 服务端九项。 */
export function privacySettingsFromForm(values: Record<string, unknown>): PrivacySettings {
  const settings = { ...DEFAULT_PRIVACY_SETTINGS };
  for (const [formName, field] of TOGGLE_FIELDS) {
    settings[field] = values[formName] === true;
  }
  settings.retention = parseRetention(values.retention);
  settings.file_cache = parseFileCache(values.fileCache);
  return settings;
}

// ── 面板回填 ─────────────────────────────────────────────────────

function applyToggle(root: ParentNode, name: string, active: boolean): void {
  const toggle = root.querySelector<HTMLElement>(`[data-setting-toggle][name="${name}"]`);
  if (!toggle) return;
  toggle.classList.toggle("active", active);
  toggle.setAttribute("aria-checked", String(active));
}

/** 按解析后的策略值选中原生 select 与其旁的自定义下拉（两者都要，否则显示分叉）。 */
function applySelect(
  root: ParentNode,
  name: string,
  wanted: string,
  parse: (label: unknown) => string,
): void {
  const select = root.querySelector<HTMLSelectElement>(`select[name="${name}"]`);
  if (!select) return;
  const option = Array.from(select.options)
    .find(item => parse(item.value || item.textContent) === wanted);
  if (!option) return;
  select.value = option.value;

  const custom = select.nextElementSibling;
  if (!(custom instanceof HTMLElement) || !custom.classList.contains("settings-custom-select")) return;
  const trigger = custom.querySelector("[data-custom-select-trigger] span");
  if (trigger) trigger.textContent = (option.textContent ?? "").trim();
  custom.querySelectorAll("[data-custom-select-option]").forEach(button => {
    button.setAttribute(
      "aria-selected",
      String(button.getAttribute("data-custom-select-option") === option.value),
    );
  });
}

/** 开关布尔值并入本机设置：各触发点的读取器不必等下一次「保存更改」。 */
function mergeTogglesIntoLocal(settings: PrivacySettings): void {
  try {
    const local = savedSettings();
    for (const [formName, field] of TOGGLE_FIELDS) {
      local[formName] = settings[field];
    }
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(local));
  } catch {
    // 本机存储不可用时闸门退回默认值，服务端设置不受影响。
  }
}

/** 打开设置面板时调用：以服务端为准覆盖「数据与隐私」的显示；未登录保持原样。 */
export async function hydratePrivacyPane(root: ParentNode): Promise<void> {
  try {
    const { settings } = await authApi.getPrivacySettings();
    for (const [formName, field] of TOGGLE_FIELDS) {
      applyToggle(root, formName, settings[field]);
    }
    applySelect(root, "retention", settings.retention, parseRetention);
    applySelect(root, "fileCache", settings.file_cache, parseFileCache);
    mergeTogglesIntoLocal(settings);
  } catch {
    // 未登录或网络失败：保留本机显示，等保存时再尝试同步。
  }
}

/** 「保存更改」时调用：九项落服务端。返回要提示用户的文案，null = 成功。 */
export async function persistPrivacySettings(values: Record<string, unknown>): Promise<string | null> {
  const settings = privacySettingsFromForm(values);
  // 关闭「保存任务历史」立即清掉本机的最近任务记录、首页对话目录与全部对话
  // 正文，不等下一次写入。
  if (!settings.save_history) {
    forgetLastTask();
    clearAllChatSessions();
    clearAllConversationLogs();
  }
  try {
    await authApi.updatePrivacySettings(settings);
    return null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return "隐私设置已在本机保存，登录后才会同步到服务端生效。";
    }
    return error instanceof Error
      ? `隐私设置同步失败：${error.message}`
      : "隐私设置同步失败，请稍后重试。";
  }
}

/** 应用启动后调用一次：把服务端开关并入本机，换浏览器后闸门立即正确。 */
export async function syncPrivacyGatesOnce(): Promise<void> {
  try {
    if (sessionStorage.getItem(SYNC_GUARD_KEY)) return;
    sessionStorage.setItem(SYNC_GUARD_KEY, "1");
  } catch {
    return;
  }
  try {
    const { settings } = await authApi.getPrivacySettings();
    mergeTogglesIntoLocal(settings);
  } catch {
    // 未登录或网络失败：保持本机现状，下次会话再试。
  }
}
