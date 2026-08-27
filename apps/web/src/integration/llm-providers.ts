/**
 * 国内外主流模型厂商的官方 API 预设：设置中心「模型厂商」页的卡片数据源，
 * 「配置」按钮据此一键填入自定义 API 表单（用户只需补 API Key）。
 *
 * 模型名与接入地址采集自各厂商官方文档（2026-08-27）：
 * - OpenAI GPT-5.6 家族 2026-07-09 GA（gpt-5.6 别名指向 gpt-5.6-sol）；
 * - Anthropic Fable 5 是当前能力最强的正式发布模型，Opus 5（2026-07-24）以
 *   一半价格贴近它，Mythos 5 仅限 Project Glasswing 不列入预设；
 * - Gemini 3.6 Flash / 3.5 Flash-Lite 2026-07-21 GA，3.5 Pro 仍在伙伴测试；
 * - DeepSeek V4（deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用；
 *   deepseek-v4-flash-vision-exp 视觉实验版 2026-08-21 上线，价格与 v4-flash 相同）；
 * - Qwen3.8-Max 2026-08-03、Qwen3.8-Flash 2026-08-26 上线（DashScope 兼容模式）；
 * - Kimi K3 2026-08-20 上线，K2.7-Code 为编程线（api.moonshot.cn，OpenAI 兼容）；
 * - GLM-5.3 2026-08-14 发布 / 08-19 开放 API，GLM-5.3-Flash 为原生多模态轻量档；
 *   智谱同时提供 open.bigmodel.cn 与 api.z.ai 两个入口，二者模型 ID 一致；
 * - xAI Grok 4.6 2026-08-12 发布（api.x.ai/v1）。
 * 模型迭代快，过期时更新本表即可，运行时行为不依赖具体模型名：预设只决定
 * 一键填入的默认值，模型 ID 输入框始终可以手填表里没有的新模型。
 */

import type { LlmEndpoint } from "../auth/api";

export interface ProviderPreset {
  id: string;
  /** 卡片显示名（品牌名） */
  label: string;
  /** providerLogo 的资源键；无对应资源时前端回落为首字母标 */
  logo: string;
  protocol: LlmEndpoint["protocol"];
  baseUrl: string;
  /** 当前官方在售模型，旗舰在前；第一项作为一键填入的默认模型 */
  models: string[];
  /** 卡片副标题（纯模型名时留空，用 models 拼接） */
  subtitle?: string;
  /**
   * 同一厂商的其他官方域名（国内外双入口、协议专用域名等）。只参与「已连接」
   * 判定与品牌标识匹配，不改变一键填入的 baseUrl。
   */
  altHosts?: string[];
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: "openai",
    label: "OpenAI",
    logo: "openai",
    protocol: "openai",
    baseUrl: "https://api.openai.com",
    models: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
  },
  {
    id: "anthropic",
    label: "Anthropic",
    logo: "anthropic",
    protocol: "anthropic",
    baseUrl: "https://api.anthropic.com",
    models: ["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
  },
  {
    id: "google",
    label: "Google Gemini",
    logo: "google",
    protocol: "gemini",
    baseUrl: "https://generativelanguage.googleapis.com",
    models: ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    logo: "deepseek",
    protocol: "openai",
    baseUrl: "https://api.deepseek.com",
    models: ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"],
  },
  {
    id: "qwen",
    label: "通义千问",
    logo: "qwen",
    protocol: "openai",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus"],
  },
  {
    id: "kimi",
    label: "Kimi",
    logo: "kimi",
    protocol: "openai",
    baseUrl: "https://api.moonshot.cn/v1",
    models: ["kimi-k3", "kimi-k2.7-code", "kimi-k2.6"],
    altHosts: ["api.moonshot.ai"],
  },
  {
    id: "zhipu",
    label: "智谱 GLM",
    logo: "zhipu",
    protocol: "openai",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-5.3", "glm-5.3-flash", "glm-5.2"],
    altHosts: ["api.z.ai"],
  },
  {
    id: "xai",
    label: "xAI Grok",
    logo: "xai",
    protocol: "openai",
    baseUrl: "https://api.x.ai/v1",
    models: ["grok-4.6", "grok-4.5"],
  },
  {
    id: "ollama",
    label: "本地模型",
    logo: "ollama",
    protocol: "ollama",
    baseUrl: "http://127.0.0.1:11434/v1",
    models: [],
    subtitle: "Ollama · 本地已安装模型",
  },
];

export function providerPreset(id: string | undefined): ProviderPreset | undefined {
  return PROVIDER_PRESETS.find(preset => preset.id === id);
}

export function presetHost(preset: ProviderPreset): string {
  try {
    return new URL(preset.baseUrl).hostname;
  } catch {
    return "";
  }
}

export function endpointHost(baseUrl: string): string {
  try {
    return new URL(baseUrl).hostname;
  } catch {
    return "";
  }
}

/** 主域名或任一备用域名命中即认为这条接口属于该厂商。 */
export function presetMatchesHost(preset: ProviderPreset, host: string): boolean {
  if (!host) return false;
  return host === presetHost(preset) || (preset.altHosts?.includes(host) ?? false);
}
