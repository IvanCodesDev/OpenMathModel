/**
 * 任务页对话区的真实回复通道（POST /api/chat）。
 *
 * 服务端按设置中心「自定义 API」的配置出网调用模型：流式时转发 SSE 事件
 * （meta → delta* → done/error），关闭流式则一次性返回 JSON。对话历史保存在
 * 页面内存里随请求携带，服务端无状态、不落库（本机留存策略归「数据与隐私」）。
 */

import { collectTaskAttachmentContext } from "../attachments/task-attachment-context";
import {
  appendConversationEntries,
  loadConversationLog,
  type ConversationLogEntry,
} from "../tasks/conversation-log";
import { saveHistoryEnabled } from "../preferences/privacy-preferences";
import { currentChatMode } from "./chat-mode";

/** Auto 模式的路由判定结果：难度 1-5 与判定用的模型（空 = 规则估计/继承）。 */
export interface ChatRouteMeta {
  mode?: string;
  difficulty?: number;
  reason?: string;
  judge_model?: string;
  /** 本次实际使用的接口 id：下一轮回传给服务端做接口粘性。 */
  endpoint_id?: string;
  /** 本次是否真的花了一次判定调用；false = 短路/继承，用于重判轮数计数。 */
  judged?: boolean;
  /** true = 难度未跳档，沿用了上一轮接口（保住供应商侧 prompt cache）。 */
  sticky?: boolean;
}

export interface ChatMeta {
  endpoint?: string;
  host?: string;
  model?: string;
  third_party?: boolean;
  fallback_used?: boolean;
  usage?: { prompt_tokens?: number; completion_tokens?: number };
  elapsed_ms?: number;
  route?: ChatRouteMeta | null;
}

export interface ChatHandlers {
  /** 每个增量回调一次；full 为累计文本，直接渲染即可。 */
  onDelta?: (delta: string, full: string) => void;
  /** 思考型模型的推理增量（DeepSeek reasoning_content / Claude thinking 等）。 */
  onReasoning?: (delta: string, full: string) => void;
  /** 首个事件（流式）或响应返回（非流式）时回调，携带实际接口与模型。 */
  onMeta?: (meta: ChatMeta) => void;
}

export class ChatError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

/** 单页会话内的对话历史；超长时保留开头的任务背景与最近的往返。 */
const history: ChatTurn[] = [];
const HISTORY_LIMIT = 24;

function trimHistory(): void {
  if (history.length > HISTORY_LIMIT) {
    history.splice(2, history.length - HISTORY_LIMIT);
  }
}

/** 系统自动发起的开场分析指令：恢复对话时用同一文案重建模型上下文。 */
export const OPENING_ANALYSIS_PROMPT =
  "请先对这个建模任务做开场分析：你对题目的理解、主要难点、以及接下来的执行思路，控制在两三段。";

/** 当前对话绑定的运行与题面；null = 演示/无运行页面（沿用旧 sessionStorage 键）。 */
let scopeRunId: string | null = null;
let scopeGoal = "";

/**
 * Auto 路由的会话内状态：上一轮判定结果随下一条消息回传（服务端无状态）。
 * 服务端据此实现「短追问继承难度」与「接口粘性」，省掉重复判定调用并保住
 * 供应商侧 prompt cache。turns = 距上次真实判定的轮数（judged 时清零）。
 */
let routeState: { difficulty: number; endpointId?: string; turns: number } | null = null;

function goalPrefixed(goal: string, text: string): string {
  return goal ? `【当前建模任务】${goal}\n\n${text}` : text;
}

/**
 * 绑定当前对话所属的运行：切到另一个任务时清空上下文并从本机对话记录
 * （tasks/conversation-log，按 run_id 隔离）重建；同一运行内重复调用只
 * 更新题面。这是「点最近任务进来不串上一个任务数据」的关键闸门。
 */
export function configureConversation(runId: string | null, goal = ""): void {
  if (scopeRunId === runId) {
    scopeGoal = goal || scopeGoal;
    return;
  }
  scopeRunId = runId;
  scopeGoal = goal;
  history.length = 0;
  routeState = null;
  if (!runId) return;
  for (const entry of loadConversationLog(runId)) {
    if (entry.role === "assistant" && entry.opening) {
      // 开场分析在记录里只有回复：按原始指令补出用户轮，保持 user/assistant 交替。
      history.push({ role: "user", content: goalPrefixed(history.length === 0 ? scopeGoal : "", OPENING_ANALYSIS_PROMPT) });
      history.push({ role: "assistant", content: entry.text });
    } else if (entry.role === "user") {
      history.push({ role: "user", content: goalPrefixed(history.length === 0 ? scopeGoal : "", entry.text) });
    } else {
      history.push({ role: "assistant", content: entry.text });
    }
  }
  trimHistory();
}

function taskGoal(): string {
  // 已绑定运行时只认该运行的题面；全局 sessionStorage 键是同标签页上一个
  // 任务留下的，读它会把别的任务的题目串进当前对话。
  if (scopeRunId !== null) return scopeGoal;
  try {
    return sessionStorage.getItem("openmathmodelPrompt")?.trim() ?? "";
  } catch {
    return "";
  }
}

/** 供页面层的回复执行轨迹展示：当前绑定运行、题面与已发生的对话轮数（只读）。 */
export function conversationSnapshot(): { runId: string | null; goal: string; turns: number } {
  return { runId: scopeRunId, goal: taskGoal(), turns: history.length };
}

/**
 * 模型选择器当前值 → 请求体路由参数。
 * "auto" 走服务端难度判定路由；"endpoint-<id>" 指定某条已保存接口；
 * 其余历史遗留值（演示期的静态模型名）不携带参数，按默认主接口链处理。
 */
function routeSelection(): { route?: string; endpoint_id?: string } {
  let raw = "auto";
  try {
    raw = localStorage.getItem("openmathmodelSelectedModel") || "auto";
  } catch {
    // 存储不可用时按 Auto 处理
  }
  if (raw.startsWith("endpoint-")) return { endpoint_id: raw.slice("endpoint-".length) };
  if (raw === "auto") return { route: "auto" };
  return {};
}

/** 难度重判用的微上下文：上一轮回复首行；首轮退到任务题面开头。 */
function judgeContext(): string | undefined {
  for (let i = history.length - 1; i >= 0; i -= 1) {
    if (history[i].role === "assistant") {
      const line = history[i].content.split("\n", 1)[0]?.trim();
      return line ? line.slice(0, 200) : undefined;
    }
  }
  const goal = taskGoal().trim();
  return goal ? goal.slice(0, 200) : undefined;
}

/** Auto 模式随消息携带的判定输入与会话路由状态（其余模式不带）。 */
function routeExtras(routing: { route?: string }, text: string): Record<string, unknown> {
  if (routing.route !== "auto") return {};
  return {
    // 判定只看用户敲的原文：注入的任务/附件/模式指令块会偏置难度并浪费判定 token
    route_question: text.slice(0, 8000),
    route_context: judgeContext(),
    ...(routeState
      ? {
        route_state: {
          difficulty: routeState.difficulty,
          endpoint_id: routeState.endpointId,
          turns: routeState.turns,
        },
      }
      : {}),
  };
}

async function errorFromResponse(response: Response): Promise<ChatError> {
  try {
    const payload = (await response.json()) as { code?: string; message?: string };
    if (payload.code === "NOT_FOUND") {
      // 对话路由只可能在我们自己的后端缺失：运行中的进程是旧代码
      return new ChatError("BACKEND_OUTDATED", "后端尚未加载对话接口，请重启后端服务（npm run dev）");
    }
    return new ChatError(payload.code ?? "CHAT_FAILED", payload.message ?? "对话请求失败，请稍后再试");
  } catch {
    if (response.status === 401) return new ChatError("AUTH_REQUIRED", "请先登录后再使用模型对话");
    return new ChatError("CHAT_FAILED", `对话请求失败（HTTP ${response.status}）`);
  }
}

function parseSseChunk(buffer: string): { events: Array<Record<string, unknown>>; rest: string } {
  const events: Array<Record<string, unknown>> = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    for (const line of block.split("\n")) {
      if (!line.startsWith("data:")) continue;
      try {
        events.push(JSON.parse(line.slice(5).trim()) as Record<string, unknown>);
      } catch {
        // 半截或非 JSON 行按空事件跳过
      }
    }
  }
  return { events, rest };
}

export interface ChatTurnOptions {
  /** 随消息发送的附件上下文块（ADR-0010 批次三）；只进请求内容，不进气泡展示。 */
  attachmentContext?: string;
  /** 本轮是系统自动发起的开场分析：本机记录只保留回复，不留用户气泡。 */
  openingAnalysis?: boolean;
  /** 随消息发送的附件名：进入本机对话记录，恢复时重建纸夹徽标。 */
  attachmentNames?: string[];
}

/**
 * 发送一轮对话并返回完整回复。首轮把任务目标并入用户消息作为背景，
 * 保持 user/assistant 交替（Anthropic 协议要求严格交替）。
 */
export async function sendConversationTurn(
  text: string,
  handlers: ChatHandlers = {},
  options: ChatTurnOptions = {},
): Promise<{ text: string; reasoning: string; meta: ChatMeta }> {
  const goal = history.length === 0 ? taskGoal() : "";
  // 任务附件（首页上传的项目产物）与随消息附件（对话框托盘）是互补的两条来源：
  // 前者按解析就绪进度逐轮并入，后者由调用方通过 attachmentContext 传入。
  const taskAttachments = await collectTaskAttachmentContext();
  const parts: string[] = [];
  if (goal) parts.push(`【当前建模任务】${goal}`);
  if (taskAttachments) parts.push(taskAttachments.block);
  if (options.attachmentContext) parts.push(options.attachmentContext);
  // 对话模式（深度研究/快速分析）的风格指令随每条消息注入；开场分析
  // 有自己的长度约束，不叠加。
  const mode = currentChatMode();
  if (mode.instruction && !options.openingAnalysis) parts.push(mode.instruction);
  parts.push(text);
  const content = parts.join("\n\n");
  history.push({ role: "user", content });
  trimHistory();

  const routing = routeSelection();
  let response: Response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
      body: JSON.stringify({ messages: [...history], ...routing, ...routeExtras(routing, text) }),
    });
  } catch {
    history.pop();
    throw new ChatError("NETWORK_ERROR", "无法连接服务，请确认后端已启动");
  }
  if (!response.ok) {
    history.pop();
    throw await errorFromResponse(response);
  }

  const contentType = response.headers.get("Content-Type") ?? "";
  let full = "";
  let reasoning = "";
  const meta: ChatMeta = {};

  if (contentType.includes("text/event-stream") && response.body) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let failure: ChatError | null = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSseChunk(done ? `${buffer}\n\n` : buffer);
      buffer = done ? "" : rest;
      for (const event of events) {
        if (event.type === "meta") {
          Object.assign(meta, event);
          delete (meta as Record<string, unknown>).type;
          handlers.onMeta?.(meta);
        } else if (event.type === "delta" && typeof event.text === "string") {
          full += event.text;
          handlers.onDelta?.(event.text, full);
        } else if (event.type === "reasoning" && typeof event.text === "string") {
          reasoning += event.text;
          handlers.onReasoning?.(event.text, reasoning);
        } else if (event.type === "done") {
          meta.usage = event.usage as ChatMeta["usage"];
          meta.elapsed_ms = event.elapsed_ms as number | undefined;
        } else if (event.type === "error") {
          failure = new ChatError(String(event.code ?? "CHAT_FAILED"), String(event.message ?? "对话请求失败"));
        }
      }
      if (done) break;
    }
    if (failure && !full) {
      history.pop();
      throw failure;
    }
  } else {
    const payload = (await response.json()) as ChatMeta & { reply?: string; reasoning?: string };
    full = payload.reply ?? "";
    reasoning = payload.reasoning ?? "";
    Object.assign(meta, payload);
    delete (meta as Record<string, unknown>).reply;
    delete (meta as Record<string, unknown>).reasoning;
    handlers.onMeta?.(meta);
    if (reasoning) handlers.onReasoning?.(reasoning, reasoning);
    if (full) handlers.onDelta?.(full, full);
  }

  if (!full) {
    history.pop();
    throw new ChatError("EMPTY_REPLY", "模型没有返回内容，请重试或检查接口配置");
  }
  // 思考过程只用于展示，不进对话历史（回传会浪费上下文且各家协议不认）
  history.push({ role: "assistant", content: full });
  trimHistory();
  // Auto 路由状态推进：judged=true 表示服务端真的花了一次判定（计数清零），
  // 否则累加继承轮数，攒够后服务端会强制重判一次。
  const route = meta.route;
  if (route?.mode === "auto" && typeof route.difficulty === "number") {
    routeState = {
      difficulty: route.difficulty,
      endpointId: route.endpoint_id ?? routeState?.endpointId,
      turns: route.judged ? 0 : (routeState?.turns ?? 0) + 1,
    };
  }
  // 发送成功才把任务附件标记为已注入；失败路径保留，下一轮重新并入。
  taskAttachments?.commit();
  // 本机对话记录（按 run 隔离）：重新进入任务时恢复气泡与上下文。
  // 「保存任务历史」（数据与隐私）关闭时不落盘。
  if (scopeRunId && saveHistoryEnabled()) {
    const entries: ConversationLogEntry[] = options.openingAnalysis
      ? [{ role: "assistant", text: full, opening: true }]
      : [
        {
          role: "user",
          text,
          ...(options.attachmentNames?.length ? { attachments: options.attachmentNames } : {}),
        },
        { role: "assistant", text: full },
      ];
    appendConversationEntries(scopeRunId, entries);
  }
  return { text: full, reasoning, meta };
}

/** 切换任务/页面时清空上下文与运行绑定（预留给控制器调用）。 */
export function resetConversation(): void {
  history.length = 0;
  scopeRunId = null;
  scopeGoal = "";
  routeState = null;
}
