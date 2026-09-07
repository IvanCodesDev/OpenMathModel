/**
 * 国内外主流模型厂商的品牌与接入事实：设置中心「模型厂商」页的卡片骨架，
 * 「配置」按钮据此一键填入自定义 API 表单的地址与协议（用户只需补 API Key）。
 *
 * 这里只放变化极慢的东西（品牌名、图标键、协议、官方接入域名）。在售型号与
 * 单价迭代很快，不再写死在前端：由服务端从公共模型目录定时同步
 * （ADR-0017，``GET /api/llm/catalog``），页面打开时填进卡片副标题、「配置」
 * 一键填入的默认模型和「默认模型 ID」的补全列表；目录不可达时后端回落内置
 * 快照并在页面标注新鲜度。
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
  /** 卡片副标题固定文案（本地模型这类没有目录概念的厂商）；留空则显示目录型号 */
  subtitle?: string;
  /**
   * 同一厂商的其他官方域名（国内外双入口、协议专用域名等）。只参与「已连接」
   * 判定与品牌标识匹配，不改变一键填入的 baseUrl。
   */
  altHosts?: string[];
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  { id: "openai", label: "OpenAI", logo: "openai", protocol: "openai", baseUrl: "https://api.openai.com" },
  { id: "anthropic", label: "Anthropic", logo: "anthropic", protocol: "anthropic", baseUrl: "https://api.anthropic.com" },
  {
    id: "google",
    label: "Google Gemini",
    logo: "google",
    protocol: "gemini",
    baseUrl: "https://generativelanguage.googleapis.com",
  },
  { id: "deepseek", label: "DeepSeek", logo: "deepseek", protocol: "openai", baseUrl: "https://api.deepseek.com" },
  {
    id: "qwen",
    label: "通义千问",
    logo: "qwen",
    protocol: "openai",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  },
  {
    id: "kimi",
    label: "Kimi",
    logo: "kimi",
    protocol: "openai",
    baseUrl: "https://api.moonshot.cn/v1",
    altHosts: ["api.moonshot.ai"],
  },
  {
    id: "zhipu",
    label: "智谱 GLM",
    logo: "zhipu",
    protocol: "openai",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    altHosts: ["api.z.ai"],
  },
  { id: "xai", label: "xAI Grok", logo: "xai", protocol: "openai", baseUrl: "https://api.x.ai/v1" },
  {
    id: "ollama",
    label: "本地模型",
    logo: "ollama",
    protocol: "ollama",
    baseUrl: "http://127.0.0.1:11434/v1",
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
