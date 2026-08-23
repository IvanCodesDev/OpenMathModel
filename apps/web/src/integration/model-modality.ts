/**
 * 模型模态分类与「生效模型」解析（ADR-0010 批次一）。
 *
 * 判定是启发式的：按模型名模式识别，宁可漏报不可误报——unknown 一律沉默，
 * 免得错误提醒训练用户忽略提醒。厂商能力变化时只需更新本文件的两张模式表。
 */

import { authApi, type LlmConfig } from "../auth/api";

export type ModelModality = "vision" | "text" | "unknown";

/** 明确具备视觉输入能力的模型名模式（旗舰多模态家族 + 通用视觉命名记号）。 */
const VISION_PATTERNS: readonly RegExp[] = [
  /^gpt-5/i,
  /^gpt-4o/i,
  /^claude-/i,
  /^gemini-/i,
  /^grok-4/i,
  /-vl\b|-vl-/i,
  /vision/i,
  /^glm-4(\.\d+)?v/i,
  /llava|pixtral|internvl|minicpm-v/i,
];

/**
 * 明确为纯文本的模型名模式。deepseek 对话/推理线不收图；qwen 文本线与视觉线（-vl）分列。
 * 注意判定顺序：视觉表先于本表命中，deepseek 名下带 vision 记号的视觉线
 * （如 2026-08-21 上线的 deepseek-v4-flash-vision-exp）由上方 /vision/i 兜住，
 * 不会落进本表的 deepseek 纯文本规则。
 */
const TEXT_ONLY_PATTERNS: readonly RegExp[] = [
  /^deepseek-(?!vl)/i,
  /^qwen(?![\d.]*-?vl)(?!.*omni)/i,
  /^kimi-k/i,
];

export function modelModality(model: string): ModelModality {
  const name = model.trim();
  if (!name) return "unknown";
  if (VISION_PATTERNS.some(pattern => pattern.test(name))) return "vision";
  if (TEXT_ONLY_PATTERNS.some(pattern => pattern.test(name))) return "text";
  return "unknown";
}

/** llm-config 只取一次：未登录（401）或接口失败时记为 null，本页会话内不再重试。 */
let configPromise: Promise<LlmConfig | null> | undefined;

function loadConfig(): Promise<LlmConfig | null> {
  configPromise ??= authApi.getLlmConfig().then(
    payload => payload.config,
    () => null,
  );
  return configPromise;
}

export interface EffectiveModality {
  /** 生效模型名（对话页文案不展示，仅供模态判定与调试）；解析不出来时为空串 */
  model: string;
  modality: ModelModality;
  /** 生效模型来自哪条已保存接口：携图直通时用它钉住请求，绕过 Auto 难度路由 */
  endpointId?: string;
}

/**
 * 把模型选择器的取值解析成可判定模态的生效模型。
 * 取值语义与 agent-chat.ts 的 routeSelection 对齐：
 * "auto" → 主接口模型；"endpoint-<id>" → 该已保存接口的模型；其余 → 直接按模型名判定。
 * 未登录、未配置接口时返回 unknown（保持沉默）。
 */
export async function resolveSelectedModality(selected: string): Promise<EffectiveModality> {
  const raw = selected.trim() || "auto";
  if (raw !== "auto" && !raw.startsWith("endpoint-")) {
    return { model: raw, modality: modelModality(raw) };
  }

  const config = await loadConfig();
  if (!config || config.endpoints.length === 0) return { model: "", modality: "unknown" };

  const endpoint = raw.startsWith("endpoint-")
    ? config.endpoints.find(item => item.id === raw.slice("endpoint-".length))
    : config.endpoints.find(item => item.id === config.active_endpoint_id) ?? config.endpoints[0];
  if (!endpoint?.model) return { model: "", modality: "unknown" };
  return {
    model: endpoint.model,
    modality: modelModality(endpoint.model),
    endpointId: endpoint.id ?? undefined,
  };
}

/** 附件托盘提醒行文案；不需要提醒时返回空串。 */
export function modalityNotice(images: number, effective: EffectiveModality): string {
  if (images <= 0 || effective.modality !== "text") return "";
  // 对话页不显示具体模型名：提醒一律用「当前所选模型」指代。
  return `当前所选模型为纯文本模型，附件中的 ${images} 张图片不会被模型直接看到；`
    + "正文文字仍会提供，可切换视觉模型后再发送。";
}
