/**
 * 设置中心「高级设置 · 请求超时」的运行时读取器。
 *
 * 设置本体由面板落盘（openmathmodelSettings），这里只负责读并夹紧到安全区间。
 * 超时作用于网页与服务端之间的 JSON 接口请求；SSE 长连接和附件上传刻意豁免，
 * 它们本来就该跑得比普通请求久。
 */

const SETTINGS_KEY = "openmathmodelSettings";

export const DEFAULT_REQUEST_TIMEOUT_SECONDS = 120;
export const MIN_REQUEST_TIMEOUT_SECONDS = 5;
export const MAX_REQUEST_TIMEOUT_SECONDS = 600;

/** 表单里存的是字符串；空串、NaN、越界都回落到安全值而不是照单全收。 */
export function normalizeTimeoutSeconds(value: unknown): number {
  const parsed = typeof value === "number" ? value
    : typeof value === "string" && value.trim() !== "" ? Number(value)
      : Number.NaN;
  if (!Number.isFinite(parsed)) return DEFAULT_REQUEST_TIMEOUT_SECONDS;
  return Math.min(MAX_REQUEST_TIMEOUT_SECONDS, Math.max(MIN_REQUEST_TIMEOUT_SECONDS, Math.round(parsed)));
}

export function requestTimeoutSeconds(): number {
  try {
    const settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
    return normalizeTimeoutSeconds(settings.requestTimeout);
  } catch {
    return DEFAULT_REQUEST_TIMEOUT_SECONDS;
  }
}

export function requestTimeoutMs(): number {
  return requestTimeoutSeconds() * 1000;
}
