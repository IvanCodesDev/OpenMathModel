/**
 * 设置中心「自定义 API」与服务端配置（/api/account/llm-config）的同步。
 *
 * 表单显示值随整张设置表落在 localStorage，但真正生效的配置存本机后端：
 * 对话回复与任务执行都在服务端出网调用模型，密钥不经过页面分发。打开面板
 * 时以服务端为准回填，保存时把表单值推送上去（更新当前主接口或创建首个）。
 */

import { ApiError, authApi, type LlmConfig, type LlmEndpoint } from "../auth/api";

const PROTOCOL_PAIRS: Array<[string, LlmEndpoint["protocol"]]> = [
  ["OpenAI Compatible", "openai"],
  ["Anthropic Messages API", "anthropic"],
  ["Google Gemini API", "gemini"],
  ["Ollama", "ollama"],
  ["自定义 REST", "custom"],
];

export function protocolFromLabel(label: unknown): LlmEndpoint["protocol"] {
  return PROTOCOL_PAIRS.find(([text]) => text === String(label ?? "").trim())?.[1] ?? "openai";
}

export function labelFromProtocol(protocol: unknown): string {
  return PROTOCOL_PAIRS.find(([, value]) => value === protocol)?.[0] ?? "OpenAI Compatible";
}

/** 权重输入 → 0-10 的整数；空串/非法值视为 0（按模型名自动推断）。 */
export function weightFromInput(raw: unknown): number {
  const value = Number.parseInt(String(raw ?? "").trim(), 10);
  if (!Number.isFinite(value)) return 0;
  return Math.min(10, Math.max(0, value));
}

/**
 * 表单当前值 → 一条接口配置。Base URL 无效或仍是示例占位（example.com）时
 * 返回 null，避免把演示默认值当成真实接口同步上去。
 */
export function endpointFromForm(values: Record<string, unknown>): LlmEndpoint | null {
  const baseUrl = String(values.apiBaseUrl ?? "").trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(baseUrl)) return null;
  try {
    if (/(^|\.)example\.com$/i.test(new URL(baseUrl).hostname)) return null;
  } catch {
    return null;
  }
  return {
    name: String(values.apiProfileName ?? "").trim() || "自定义接口",
    protocol: protocolFromLabel(values.apiProtocol),
    base_url: baseUrl,
    api_key: String(values.apiKey ?? "").trim(),
    model: String(values.apiModel ?? "").trim(),
    organization: String(values.apiOrganization ?? "").trim(),
    headers: String(values.customHeader ?? "").trim(),
    path_prefix: String(values.apiPathPrefix ?? "").trim(),
    weight: weightFromInput(values.apiWeight),
  };
}

/** 三个行为开关的即时读数；控件缺省时保持与面板初始值一致（全开）。 */
function flagsFromForm(values: Record<string, unknown>) {
  return {
    allow_proxy: values.allowProxyApi !== false,
    stream: values.streamResponse !== false,
    fallback: values.fallbackApi !== false,
  };
}

/** 读取服务端配置；未登录或后端不可用返回 null（面板保持本机显示）。 */
export async function fetchLlmConfig(): Promise<LlmConfig | null> {
  try {
    return (await authApi.getLlmConfig()).config;
  } catch {
    return null;
  }
}

/**
 * 「默认模型 ID」的补全来源：直接问接口本身要模型列表，比内置的厂商预设表新
 * （预设表写下来那天就开始过期，新模型上线当天就能从这里选到）。
 *
 * 纯锦上添花：拿不到（未登录、网关没实现 /models、网络不通）就返回空数组由
 * 调用方回落到预设，绝不打断填写。按「协议+地址+密钥」缓存，避免每次聚焦
 * 输入框都出一次网；换了地址或密钥自然是另一条缓存，失败的不留缓存以便重试。
 */
const modelListCache = new Map<string, Promise<string[]>>();

export async function fetchEndpointModels(values: Record<string, unknown>): Promise<string[]> {
  const endpoint = endpointFromForm(values);
  if (!endpoint) return [];
  const key = `${endpoint.protocol}|${endpoint.base_url}|${endpoint.api_key}`;
  let pending = modelListCache.get(key);
  if (!pending) {
    pending = authApi.listLlmModels({ ...endpoint, allow_proxy: values.allowProxyApi !== false }).then(
      result => result.models,
      () => {
        modelListCache.delete(key);
        return [];
      },
    );
    modelListCache.set(key, pending);
  }
  return pending;
}

function syncFailureMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "自定义 API 已在本机保存，登录后才会同步到服务端供对话与任务使用。";
  }
  return error instanceof Error
    ? `自定义 API 同步失败：${error.message}`
    : "自定义 API 同步失败，请稍后重试。";
}

/**
 * 「保存更改」时调用：表单值更新目标接口（编辑态更新被编辑的那条，否则
 * 更新当前主接口；无任何接口时创建首个），三个开关一并落库。
 * 返回要提示用户的文案，null 表示成功无需提示。
 */
export async function persistLlmSettings(values: Record<string, unknown>): Promise<string | null> {
  const endpoint = endpointFromForm(values);
  const editingId = String(values.apiEditingEndpointId ?? "").trim();
  try {
    const config = (await authApi.getLlmConfig()).config;
    if (!endpoint && config.endpoints.length === 0) {
      // 表单还是示例占位且从未保存过接口：只有开关需要落库
      await authApi.updateLlmConfig({ ...config, ...flagsFromForm(values) });
      return null;
    }
    const endpoints = [...config.endpoints];
    let active = config.active_endpoint_id;
    if (endpoint) {
      const targetId = editingId || active;
      const index = endpoints.findIndex(item => item.id === targetId);
      if (index >= 0) {
        endpoints[index] = { ...endpoints[index], ...endpoint, id: endpoints[index].id };
      } else {
        endpoints.unshift(endpoint);
        active = null; // 服务端回落到第一个（即新建的这条）
      }
    }
    await authApi.updateLlmConfig({
      ...flagsFromForm(values),
      endpoints,
      active_endpoint_id: active,
    });
    return null;
  } catch (error) {
    return syncFailureMessage(error);
  }
}

/** 「保存为新接口」：把表单当前值追加为一条新接口。返回提示文案。 */
export async function saveEndpointAsNew(values: Record<string, unknown>): Promise<string> {
  const endpoint = endpointFromForm(values);
  if (!endpoint) return "请先填写有效的 Base URL";
  try {
    const config = (await authApi.getLlmConfig()).config;
    await authApi.updateLlmConfig({
      ...config,
      ...flagsFromForm(values),
      endpoints: [...config.endpoints, endpoint],
    });
    return "已保存为新接口";
  } catch (error) {
    return syncFailureMessage(error);
  }
}

/** 「编辑」保存：把表单当前值写回指定接口（保留原 id 与主接口归属）。 */
export async function updateEndpoint(
  endpointId: string,
  values: Record<string, unknown>,
): Promise<{ config: LlmConfig | null; message: string }> {
  const endpoint = endpointFromForm(values);
  if (!endpoint) return { config: null, message: "请先填写有效的 Base URL" };
  try {
    const config = (await authApi.getLlmConfig()).config;
    const endpoints = config.endpoints.map(item =>
      item.id === endpointId ? { ...item, ...endpoint, id: item.id } : item,
    );
    const updated = await authApi.updateLlmConfig({ ...config, endpoints });
    return { config: updated.config, message: "接口已更新" };
  } catch (error) {
    return { config: null, message: syncFailureMessage(error) };
  }
}

/** 「调整权重」：单独更新一条接口的能力权重（0 = 恢复自动推断）。 */
export async function setEndpointWeight(endpointId: string, weight: number): Promise<LlmConfig | null> {
  try {
    const config = (await authApi.getLlmConfig()).config;
    const endpoints = config.endpoints.map(item =>
      item.id === endpointId ? { ...item, weight: weightFromInput(weight) } : item,
    );
    const updated = await authApi.updateLlmConfig({ ...config, endpoints });
    return updated.config;
  } catch {
    return null;
  }
}

/** 已保存接口的菜单动作：设为主接口 / 删除。成功返回最新配置。 */
export async function setPrimaryEndpoint(endpointId: string): Promise<LlmConfig | null> {
  try {
    const config = (await authApi.getLlmConfig()).config;
    const updated = await authApi.updateLlmConfig({ ...config, active_endpoint_id: endpointId });
    return updated.config;
  } catch {
    return null;
  }
}

export async function removeEndpoint(endpointId: string): Promise<LlmConfig | null> {
  try {
    const config = (await authApi.getLlmConfig()).config;
    const endpoints = config.endpoints.filter(item => item.id !== endpointId);
    const active = config.active_endpoint_id === endpointId ? null : config.active_endpoint_id;
    const updated = await authApi.updateLlmConfig({ ...config, endpoints, active_endpoint_id: active });
    return updated.config;
  } catch {
    return null;
  }
}
