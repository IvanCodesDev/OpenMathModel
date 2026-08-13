/**
 * 「允许使用第三方中转站」开关的透明化行为：记录接口用量（本机）。
 *
 * 开关开启时，每次对话完成后把实际接口、模型与 token 用量追加到 localStorage
 * 环形记录（上限 100 条），设置中心「自定义 API」面板据此展示最近调用；
 * 关闭后停止记录（第三方域名同时会被服务端门控挡下）。
 */

export interface LlmUsageRecord {
  ts: number;
  endpoint: string;
  host: string;
  model: string;
  third_party: boolean;
  fallback_used: boolean;
  prompt_tokens?: number;
  completion_tokens?: number;
  elapsed_ms?: number;
  /** Auto 路由的难度判定结果（非 Auto 请求为空）。 */
  difficulty?: number;
}

const USAGE_KEY = "openmathmodelLlmUsage";
const USAGE_LIMIT = 100;

/** 「允许使用第三方中转站」当前开关值（发送前显示域名与用量记录都随它）。 */
export function proxyTransparencyEnabled(): boolean {
  try {
    const settings = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}") as Record<string, unknown>;
    return settings.allowProxyApi !== false;
  } catch {
    return true;
  }
}

export function listLlmUsage(): LlmUsageRecord[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(USAGE_KEY) || "[]") as unknown;
    return Array.isArray(parsed)
      ? (parsed.filter(item => item && typeof item === "object") as LlmUsageRecord[])
      : [];
  } catch {
    return [];
  }
}

export function recordLlmUsage(record: LlmUsageRecord): void {
  try {
    const list = [record, ...listLlmUsage()];
    localStorage.setItem(USAGE_KEY, JSON.stringify(list.slice(0, USAGE_LIMIT)));
  } catch {
    // 本机存储不可用时静默放弃：用量记录是辅助信息，不能影响对话
  }
}

export function clearLlmUsage(): void {
  try {
    localStorage.removeItem(USAGE_KEY);
  } catch {
    // 与写入同理
  }
}
