/**
 * 可读性偏好：正文字号、减少动态效果、增强文字对比度。
 *
 * 与主题、语言走同一条路子——设置中心里即时预览，未保存关闭即还原，启动时应用。
 * 具体表现全部由 accessibility.css 承担，这里只负责把状态写到 <html> 上。
 */

const SETTINGS_KEY = "openmathmodelSettings";

/** 与 accessibility.css 的 --omm-text-base 保持一致。 */
export const TEXT_BASE_PX = 14;
export const TEXT_MIN_PX = 13;
export const TEXT_MAX_PX = 19;

export interface DisplayPreferences {
  fontSize: number;
  reduceMotion: boolean;
  highContrast: boolean;
}

const DEFAULTS: DisplayPreferences = {
  fontSize: TEXT_BASE_PX,
  reduceMotion: false,
  highContrast: false,
};

let current: DisplayPreferences = { ...DEFAULTS };

function clampFontSize(value: unknown): number {
  // Number("")、Number(" ")、Number(null) 都等于 0，直接夹紧会把"没有值"
  // 悄悄变成最小字号；这些空值应当回落默认，而不是把界面缩到 13px。
  const raw = typeof value === "string" ? value.trim() : value;
  if (raw === "" || raw === null || typeof raw === "boolean") return DEFAULTS.fontSize;
  const size = Math.round(Number(raw));
  if (!Number.isFinite(size)) return DEFAULTS.fontSize;
  return Math.min(TEXT_MAX_PX, Math.max(TEXT_MIN_PX, size));
}

export function normalizeDisplayPreferences(raw: unknown): DisplayPreferences {
  const source = (raw ?? {}) as Record<string, unknown>;
  return {
    fontSize: source.fontSize === undefined ? DEFAULTS.fontSize : clampFontSize(source.fontSize),
    reduceMotion: source.reduceMotion === true,
    highContrast: source.highContrast === true,
  };
}

export function savedDisplayPreferences(): DisplayPreferences {
  try {
    return normalizeDisplayPreferences(JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"));
  } catch {
    return { ...DEFAULTS };
  }
}

export function currentDisplayPreferences(): DisplayPreferences {
  return { ...current };
}

export function applyDisplayPreferences(raw: unknown): DisplayPreferences {
  const next = normalizeDisplayPreferences(raw);
  const root = document.documentElement;

  root.style.setProperty("--omm-text-scale", String(next.fontSize / TEXT_BASE_PX));
  // 属性置空仍会被 [data-x="on"] 之外的选择器看到，因此不需要时整个删掉
  if (next.reduceMotion) root.dataset.reduceMotion = "on";
  else delete root.dataset.reduceMotion;
  if (next.highContrast) root.dataset.contrast = "high";
  else delete root.dataset.contrast;

  current = next;
  return next;
}

/** 应用启动时调用。 */
export function initDisplayPreferences(): DisplayPreferences {
  return applyDisplayPreferences(savedDisplayPreferences());
}
