/**
 * 高级设置「最大并发任务」与服务端的同步。
 *
 * 显示值随整张设置表单落在本机 localStorage，但真正生效的上限存服务端——创建
 * 任务的闸门在后端校验，放浏览器里改个缓存就能绕过。所以打开面板时用服务端值
 * 回填显示，保存时把选择推送上去。
 */

import { ApiError, authApi } from "../auth/api";

/** 面板上「不限」选项的取值；服务端用 null 表示不限。 */
const UNLIMITED_LABEL = "不限";

/**
 * 面板选项形如「3 个」或「不限」；也兼容直接存数字的历史值。
 * 返回上限数字，`null` 表示不限，`undefined` 表示认不出来（不该推送）。
 */
export function parseMaxConcurrency(label: unknown): number | null | undefined {
  if (typeof label === "string" && label.trim() === UNLIMITED_LABEL) return null;
  const value = typeof label === "number" ? label
    : typeof label === "string" ? Number(label.match(/\d+/)?.[0] ?? Number.NaN)
      : Number.NaN;
  return Number.isInteger(value) && value >= 1 && value <= 8 ? value : undefined;
}

/** 把服务端值写回原生 select 和它旁边的自定义下拉（两者都要，否则显示会分叉）。 */
function applyToPanel(root: ParentNode, value: number | null): void {
  const select = root.querySelector<HTMLSelectElement>('select[name="maxConcurrency"]');
  if (!select) return;
  const option = Array.from(select.options)
    .find(item => parseMaxConcurrency(item.value ?? item.textContent) === value);
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

/** 打开设置面板时调用：以服务端为准覆盖本机残留的显示值；未登录时保持原样。 */
export async function hydrateMaxConcurrency(root: ParentNode): Promise<void> {
  try {
    const { preferences } = await authApi.getPreferences();
    applyToPanel(root, preferences.max_concurrent_runs);
  } catch {
    // 未登录或网络失败：保留本机显示，等保存时再尝试同步。
  }
}

/** 保存设置时调用；返回要给用户看的提示文案，null 表示成功无需提示。 */
export async function persistMaxConcurrency(label: unknown): Promise<string | null> {
  const value = parseMaxConcurrency(label);
  if (value === undefined) return null;
  try {
    await authApi.updatePreferences({ max_concurrent_runs: value });
    return null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return "最大并发任务已在本机保存，登录后才会同步到服务端生效。";
    }
    return error instanceof Error
      ? `最大并发任务同步失败：${error.message}`
      : "最大并发任务同步失败，请稍后重试。";
  }
}
