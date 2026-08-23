/**
 * 国内外主流模型厂商的官方 API 预设：设置中心「模型厂商」页的卡片数据源，
 * 「配置」按钮据此一键填入自定义 API 表单（用户只需补 API Key）。
 *
 * 模型名与接入地址采集自各厂商官方文档（2026-08-13）：
 * - OpenAI GPT-5.6 家族 2026-07-09 GA（gpt-5.6 别名指向 gpt-5.6-sol）；
 * - Anthropic Fable 5 / Opus 5 / Sonnet 5 为无日期固定 ID，Haiku 4.5 用别名；
 * - Gemini 3.5/3.6 Flash 系列 GA，3.5 Pro 尚未开放公共 API；
 * - DeepSeek V4（deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用；
 *   deepseek-v4-flash-vision-exp 视觉实验版 2026-08-21 上线，价格与 v4-flash 相同）；
 * - Qwen3.8-Max 2026-08-03 上线（DashScope OpenAI 兼容模式）；
 * - Kimi K3（api.moonshot.cn，OpenAI 兼容）；
 * - GLM-5.2 / GLM-5 系列（open.bigmodel.cn/api/paas/v4）；
 * - xAI Grok 4.6 2026-08-12 发布（api.x.ai/v1）。
 * 模型迭代快，过期时更新本表即可，运行时行为不依赖具体模型名。
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
    models: ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5"],
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
    models: ["qwen3.8-max", "qwen3.7-max", "qwen3.7-plus"],
  },
  {
    id: "kimi",
    label: "Kimi",
    logo: "kimi",
    protocol: "openai",
    baseUrl: "https://api.moonshot.cn/v1",
    models: ["kimi-k3", "kimi-k2.5"],
  },
  {
    id: "zhipu",
    label: "智谱 GLM",
    logo: "zhipu",
    protocol: "openai",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-5.2", "glm-5", "glm-5-turbo"],
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
