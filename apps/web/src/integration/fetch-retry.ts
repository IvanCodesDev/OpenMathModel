/**
 * 「自动重试失败请求」的共享实现与开发者模式的请求诊断输出，
 * 建模工作台（integration/modeling-workspace-api）与账户（auth/api）两个客户端共用。
 *
 * 重试边界刻意保守：只有 GET 或带 Idempotency-Key 的请求才允许重发，
 * 非幂等 POST 在网络抖动下重发可能产生重复副作用，宁可直接报错。
 * 触发条件仅限网络层失败（fetch 抛 TypeError）与 HTTP 429；主动中止与
 * 超时（AbortError/TimeoutError）说明调用方已不想等，原样抛出不重试。
 * 开关的即时读数由调用方每次请求时传入，保证设置面板改完立即生效。
 * 本文件不做任何导入，方便按仓库惯例用 data URL 转译做单元测试。
 */

/** 最多重试 3 次，即一次请求总共最多发出 4 次。 */
export const MAX_RETRIES = 3;

/** 首次重试前的等待，之后按 2 倍递增：400ms → 800ms → 1600ms。 */
const BASE_DELAY_MS = 400;

/** Retry-After 超过该秒数就不采纳，退回指数退避，避免被服务端拖住太久。 */
const MAX_RETRY_AFTER_SECONDS = 10;

/** 只有 GET 或幂等 POST（带 Idempotency-Key）可以安全重发。 */
export function retryAllowed(method: string, idempotent: boolean): boolean {
  return method.toUpperCase() === "GET" || idempotent;
}

/** 解析 Retry-After 的秒数形式；HTTP 日期等其他写法、超过 10 秒的值一律忽略。 */
export function parseRetryAfterSeconds(value: string | null): number | undefined {
  const trimmed = value?.trim() ?? "";
  if (!/^\d+$/.test(trimmed)) return undefined;
  const seconds = Number(trimmed);
  return seconds <= MAX_RETRY_AFTER_SECONDS ? seconds : undefined;
}

/** 第 attempt 次重试（从 0 起）前的等待；服务端明示的小 Retry-After 优先。 */
export function retryDelayMs(attempt: number, retryAfterSeconds?: number): number {
  return retryAfterSeconds !== undefined ? retryAfterSeconds * 1000 : BASE_DELAY_MS * 2 ** attempt;
}

export interface RetryContext {
  /** 「自动重试失败请求」开关的即时读数。 */
  enabled: boolean;
  method: string;
  /** 请求是否携带 Idempotency-Key。 */
  idempotent: boolean;
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => {
    setTimeout(resolve, ms);
  });
}

/** 包住一次 fetch：按上述保守规则对网络错误与 429 做退避重试。 */
export async function fetchWithRetry(
  doFetch: () => Promise<Response>,
  context: RetryContext,
): Promise<Response> {
  const allowed = context.enabled && retryAllowed(context.method, context.idempotent);
  for (let attempt = 0; ; attempt += 1) {
    let response: Response;
    try {
      response = await doFetch();
    } catch (error) {
      // 浏览器只在网络层失败时抛 TypeError；中止与超时是 DOMException，直接抛出。
      if (!allowed || attempt >= MAX_RETRIES || !(error instanceof TypeError)) throw error;
      await sleep(retryDelayMs(attempt));
      continue;
    }
    if (allowed && response.status === 429 && attempt < MAX_RETRIES) {
      await sleep(retryDelayMs(attempt, parseRetryAfterSeconds(response.headers.get("Retry-After"))));
      continue;
    }
    return response;
  }
}

/** 开发者模式：统一前缀输出一行请求诊断，请求 ID 可直接拿去对照后端日志。 */
export function logRequestDiagnostics(
  method: string,
  path: string,
  response: Response,
  elapsedMs: number,
): void {
  console.info(
    `[OMM] ${method} ${path} ${response.status} ${Math.round(elapsedMs)}ms X-Request-Id=${response.headers.get("X-Request-Id") ?? "-"}`,
  );
}
