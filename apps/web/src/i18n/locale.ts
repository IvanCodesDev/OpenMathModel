/**
 * 界面语言：读写设置、应用到文档、通知订阅者。
 *
 * 语言与主题共用 `openmathmodelSettings`，行为也保持一致——在设置中心里选择即时
 * 生效，未保存就关闭会还原为打开前的语言。
 */

import { EN_US_DICTIONARY } from "./en-US";
import { activateTranslation, deactivateTranslation, translateText } from "./dom-translator";

export type Locale = "zh-CN" | "en-US";

export const SOURCE_LOCALE: Locale = "zh-CN";
const SETTINGS_KEY = "openmathmodelSettings";
const HTML_LANG: Record<Locale, string> = { "zh-CN": "zh-CN", "en-US": "en" };

let current: Locale = SOURCE_LOCALE;
const listeners = new Set<(locale: Locale) => void>();

export function normalizeLocale(value: unknown): Locale {
  return value === "en-US" ? "en-US" : SOURCE_LOCALE;
}

export function savedLocale(): Locale {
  try {
    const settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
    return normalizeLocale(settings.interfaceLanguage);
  } catch {
    return SOURCE_LOCALE;
  }
}

export function currentLocale(): Locale {
  return current;
}

export function applyLocale(value: unknown): Locale {
  const locale = normalizeLocale(value);
  document.documentElement.lang = HTML_LANG[locale];
  if (locale === "en-US") activateTranslation(EN_US_DICTIONARY);
  else deactivateTranslation();
  current = locale;
  listeners.forEach(listener => listener(locale));
  return locale;
}

export function onLocaleChange(listener: (locale: Locale) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 应用启动时调用：在首屏渲染前装好翻译，React 插入的节点会被自动接管。 */
export function initInterfaceLocale(): Locale {
  return applyLocale(savedLocale());
}

/** 翻译单条文案；未命中时原样返回。 */
export function t(value: string): string {
  return current === SOURCE_LOCALE ? value : translateText(value);
}
