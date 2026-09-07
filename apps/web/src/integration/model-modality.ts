/**
 * 模型模态分类与「生效模型」解析（ADR-0010 批次一）。
 *
 * 两级判定：服务端同步的模型目录（ADR-0017）明确收录的型号以目录标注的输入
 * 模态为准；目录没有的（中转站自定义名、本地模型、目录未同步）再按下面的
 * 模型名模式启发式识别——宁可漏报不可误报，unknown 一律沉默，免得错误提醒
 * 训练用户忽略提醒。
 */

import { authApi, type LlmConfig } from "../auth/api";
import { loadModelCatalog } from "./model-catalog";
import { catalogVision } from "./model-catalog-view";
import { resolveTaskRoute } from "./task-routes";

export type ModelModality = "vision" | "text" | "unknown";

/** 明确具备视觉输入能力的模型名模式（旗舰多模态家族 + 通用视觉命名记号）。 */
const VISION_PATTERNS: readonly RegExp[] = [
  // GPT-5.x 三档与 GPT-6 Astra（2026-09-03，文本+图像输入）都收图
  /^gpt-[5-9]/i,
  /^gpt-4o/i,
  /^claude-/i,
  /^gemini-/i,
  /^grok-4/i,
  /-vl\b|-vl-/i,
  /vision/i,
  // glm-4.5v / glm-4.6v / glm-5v-turbo 等视觉线，以及原生多模态的 GLM-5.3-Flash
  /^glm-\d+(\.\d+)?v/i,
  /^glm-5\.3-flash/i,
  // Kimi K2.5 起的 K 系列均可收图（K3 支持图像输入，K2.7-Code 另支持视频）
  /^kimi-k[2-9]/i,
  // Qwen3.8 整代原生视觉（Max 的视觉理解贯穿全流程，27B 是视觉语言 Dense 模型）
  /^qwen3\.8/i,
  /llava|pixtral|internvl|minicpm-v/i,
];

/**
 * 明确为纯文本的模型名模式。deepseek 对话/推理线不收图；qwen 3.7 及更早的文本线与
 * 视觉线（-vl）分列，3.8 起整代原生多模态已由上方视觉表接管；
 * GLM-5.3 官方声明仅处理文本模态（同代的 GLM-5.3-Flash 才是多模态）。
 * 注意判定顺序：视觉表先于本表命中，deepseek 名下带 vision 记号的视觉线
 * （如 2026-08-21 上线的 deepseek-v4-flash-vision-exp）由上方 /vision/i 兜住，
 * 不会落进本表的 deepseek 纯文本规则。
 * 本表只收官方明确不收图的型号，不做家族级推断：判错方向不同——漏报只是少一条
 * 提醒，误报会让用户以为图片没被看见。未来型号命中不了就按 unknown 沉默。
 */
const TEXT_ONLY_PATTERNS: readonly RegExp[] = [
  /^deepseek-(?!vl)/i,
  /^qwen(?![\d.]*-?vl)(?!.*omni)/i,
  /^glm-5\.3(?!-flash)/i,
];

export function modelModality(model: string): ModelModality {
  const name = model.trim();
  if (!name) return "unknown";
  if (VISION_PATTERNS.some(pattern => pattern.test(name))) return "vision";
  if (TEXT_ONLY_PATTERNS.some(pattern => pattern.test(name))) return "text";
  return "unknown";
}

/** 目录优先的模态判定：目录收录的按目录，其余回落命名规则。 */
async function classifyModel(model: string): Promise<ModelModality> {
  const known = catalogVision(await loadModelCatalog(), model);
  if (known !== undefined) return known ? "vision" : "text";
  return modelModality(model);
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

/**
 * 设置中心改过接口或智能路由后作废缓存：下一次携图判定重新拉配置，
 * 否则本页会话内仍按打开页面时的旧配置钉接口。
 */
export function invalidateModalityConfig(): void {
  configPromise = undefined;
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
 * "auto" → 设置中心「视觉理解」定向的接口（ADR-0015），没有定向则主接口模型；
 * "endpoint-<id>" → 该已保存接口的模型；其余 → 直接按模型名判定。
 * 未登录、未配置接口时返回 unknown（保持沉默）。
 */
export async function resolveSelectedModality(selected: string): Promise<EffectiveModality> {
  const raw = selected.trim() || "auto";
  if (raw !== "auto" && !raw.startsWith("endpoint-")) {
    return { model: raw, modality: await classifyModel(raw) };
  }

  const config = await loadConfig();
  if (!config || config.endpoints.length === 0) return { model: "", modality: "unknown" };

  // Auto 下携图应打给用户指定的视觉接口，而不是碰巧当主接口的那条；服务端
  // 对不带 endpoint_id 的携图请求也按同一定向兜底，这里钉住只是省掉一次判定。
  const visionRoute = resolveTaskRoute(config, "vision");
  const endpoint = raw.startsWith("endpoint-")
    ? config.endpoints.find(item => item.id === raw.slice("endpoint-".length))
    : config.endpoints.find(item => item.id === (visionRoute ?? config.active_endpoint_id)) ?? config.endpoints[0];
  if (!endpoint?.model) return { model: "", modality: "unknown" };
  return {
    model: endpoint.model,
    modality: await classifyModel(endpoint.model),
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
