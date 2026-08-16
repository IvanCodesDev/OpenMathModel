/** 认证与账户接口客户端：同源 Cookie 会话，统一错误结构 {code, message}。 */

import { requestTimeoutSeconds } from "../preferences/network-preferences";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export interface UserInfo {
  id: string;
  email: string;
  name: string;
  plan: string;
  avatar_letter: string;
  /** 已设置头像时为带内容摘要的同源地址，未设置时为 null（回落到 avatar_letter）。 */
  avatar_url: string | null;
  created_at: string;
}

export interface SecurityOverview {
  password_changed_at: string;
  two_factor_enabled: boolean;
  recovery_codes_remaining: number;
}

export interface MeResponse {
  user: UserInfo;
  security: SecurityOverview;
}

/** 高级设置里需要服务端生效的用户偏好；纯本机偏好仍走 localStorage。 */
export interface AccountPreferences {
  max_concurrent_runs: number;
}

/** 自定义 API 的一条已保存接口；字段与设置面板一一对应。 */
export interface LlmEndpoint {
  id?: string | null;
  name: string;
  protocol: "openai" | "anthropic" | "gemini" | "ollama" | "custom";
  base_url: string;
  api_key: string;
  model: string;
  organization?: string;
  headers?: string;
  path_prefix?: string;
  /** 模型能力权重（1-10，Auto 模式按它路由）；0/缺省 = 按模型名自动推断。 */
  weight?: number;
}

/** 设置中心「自定义 API」的服务端配置：对话回复与任务执行都按它调用模型。 */
export interface LlmConfig {
  endpoints: LlmEndpoint[];
  active_endpoint_id: string | null;
  allow_proxy: boolean;
  stream: boolean;
  fallback: boolean;
}

export interface LlmTestResult {
  ok: boolean;
  latency_ms: number;
  model: string;
  host: string;
  third_party: boolean;
  reply: string;
}

/** 设置中心「用量监控」的三个预算项；硬限制由服务端在调用路径上执行。 */
export interface UsageSettings {
  monthly_budget_cny: number | null;
  budget_threshold_percent: number;
  hard_limit: boolean;
}

export interface UsageDailyPoint {
  date: string;
  requests: number;
  total_tokens: number;
  estimated_cost_cny: number;
}

export interface UsageModelRow {
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_cny: number;
}

/** /api/usage/summary 的响应：合计、上月对比、Agent 任务数、14 天序列、模型分布与预算状态。 */
export interface UsageSummary {
  month: string;
  range: { start: string; end: string };
  totals: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_cost_cny: number;
  };
  previous: { total_tokens: number; estimated_cost_cny: number };
  agent_runs: { total: number; llm: number };
  daily: UsageDailyPoint[];
  models: UsageModelRow[];
  budget: UsageSettings & {
    used_cny: number;
    remaining_cny: number | null;
    used_percent: number;
    alert: boolean;
  };
}

export interface DeviceSession {
  id: string;
  device_label: string;
  browser: string;
  os: string;
  kind: "desktop" | "mobile";
  ip: string;
  created_at: string;
  last_seen_at: string;
  current: boolean;
}

export interface LoginResponse {
  two_factor_required: boolean;
  user?: UserInfo;
  challenge_token?: string;
}

export interface TwoFaSetupResponse {
  secret: string;
  otpauth_uri: string;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response: Response;
  // FormData 的 multipart 边界由浏览器生成，显式设置 Content-Type 反而会破坏请求体。
  const isFormData = options.body instanceof FormData;
  const timeoutSeconds = requestTimeoutSeconds();
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers: options.body !== undefined && !isFormData ? { "Content-Type": "application/json" } : undefined,
      body: options.body === undefined
        ? undefined
        : isFormData
          ? (options.body as FormData)
          : JSON.stringify(options.body),
      credentials: "same-origin",
      // 高级设置「请求超时」：账户接口都是小请求，统一挂用户配置的超时。
      signal: AbortSignal.timeout(timeoutSeconds * 1000),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError") {
      throw new ApiError(0, "TIMEOUT", `请求超过 ${timeoutSeconds} 秒未响应，已中止；可在设置中心「高级设置」调整请求超时。`);
    }
    throw new ApiError(0, "NETWORK_ERROR", "无法连接服务，请确认后端已启动");
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const shape = (payload ?? {}) as { code?: string; message?: string };
    throw new ApiError(response.status, shape.code ?? "UNKNOWN_ERROR", shape.message ?? "请求失败，请稍后再试");
  }
  return payload as T;
}

export const authApi = {
  sendRegisterCode(email: string) {
    return request<{ ok: boolean; expires_in: number; dev_code?: string }>(
      "/api/auth/register/send-code",
      { method: "POST", body: { email } },
    );
  },
  register(body: { email: string; code: string; password: string; name: string }) {
    return request<{ user: UserInfo }>("/api/auth/register", { method: "POST", body });
  },
  login(body: { email: string; password: string }) {
    return request<LoginResponse>("/api/auth/login", { method: "POST", body });
  },
  loginTwoFactor(body: { challenge_token: string; code: string }) {
    return request<LoginResponse>("/api/auth/login/2fa", { method: "POST", body });
  },
  logout() {
    return request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  },
  me() {
    return request<MeResponse>("/api/account/me");
  },
  updateProfile(body: { name?: string; email?: string; password?: string }) {
    return request<{ user: UserInfo }>("/api/account/profile", { method: "PATCH", body });
  },
  getPreferences() {
    return request<{ preferences: AccountPreferences }>("/api/account/preferences");
  },
  updatePreferences(body: AccountPreferences) {
    return request<{ preferences: AccountPreferences }>("/api/account/preferences", { method: "PUT", body });
  },
  getLlmConfig() {
    return request<{ config: LlmConfig }>("/api/account/llm-config");
  },
  updateLlmConfig(body: LlmConfig) {
    return request<{ config: LlmConfig }>("/api/account/llm-config", { method: "PUT", body });
  },
  testLlmEndpoint(body: LlmEndpoint & { allow_proxy: boolean }) {
    return request<LlmTestResult>("/api/llm/test", { method: "POST", body });
  },
  getUsageSummary(month?: string) {
    return request<UsageSummary>(`/api/usage/summary${month ? `?month=${encodeURIComponent(month)}` : ""}`);
  },
  getUsageSettings() {
    return request<{ settings: UsageSettings }>("/api/usage/settings");
  },
  updateUsageSettings(body: UsageSettings) {
    return request<{ settings: UsageSettings }>("/api/usage/settings", { method: "PUT", body });
  },
  uploadAvatar(image: Blob, filename: string) {
    const form = new FormData();
    form.append("file", image, filename);
    return request<{ user: UserInfo }>("/api/account/avatar", { method: "POST", body: form });
  },
  removeAvatar() {
    return request<{ user: UserInfo }>("/api/account/avatar", { method: "DELETE" });
  },
  changePassword(body: { current_password: string; new_password: string }) {
    return request<{ ok: boolean; revoked_sessions: number }>("/api/account/password", { method: "POST", body });
  },
  twoFactorSetup() {
    return request<TwoFaSetupResponse>("/api/account/2fa/setup");
  },
  twoFactorEnable(code: string) {
    return request<{ ok: boolean; recovery_codes: string[] }>("/api/account/2fa/enable", {
      method: "POST",
      body: { code },
    });
  },
  twoFactorDisable(password: string) {
    return request<{ ok: boolean }>("/api/account/2fa/disable", { method: "POST", body: { password } });
  },
  regenerateRecoveryCodes(password: string) {
    return request<{ ok: boolean; recovery_codes: string[] }>("/api/account/2fa/recovery-codes", {
      method: "POST",
      body: { password },
    });
  },
  listSessions() {
    return request<{ sessions: DeviceSession[] }>("/api/account/sessions");
  },
  revokeSession(sessionId: string) {
    return request<{ ok: boolean }>(`/api/account/sessions/${sessionId}`, { method: "DELETE" });
  },
  revokeOtherSessions() {
    return request<{ ok: boolean; revoked_sessions: number }>("/api/account/sessions/revoke-others", {
      method: "POST",
    });
  },
};

/** 当前登录用户缓存：undefined = 未拉取，null = 未登录。 */
let cachedMe: MeResponse | null | undefined;

export async function fetchMe(force = false): Promise<MeResponse | null> {
  if (!force && cachedMe !== undefined) return cachedMe;
  try {
    cachedMe = await authApi.me();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      cachedMe = null;
    } else {
      throw error;
    }
  }
  return cachedMe;
}

export function invalidateMe(): void {
  cachedMe = undefined;
}

export function cachedUser(): MeResponse | null | undefined {
  return cachedMe;
}
