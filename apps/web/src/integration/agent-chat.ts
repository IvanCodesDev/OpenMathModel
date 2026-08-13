/**
 * 任务页对话区的真实回复通道（POST /api/chat）。
 *
 * 服务端按设置中心「自定义 API」的配置出网调用模型：流式时转发 SSE 事件
 * （meta → delta* → done/error），关闭流式则一次性返回 JSON。对话历史保存在
 * 页面内存里随请求携带，服务端无状态、不落库（本机留存策略归「数据与隐私」）。
 */

/** Auto 模式的路由判定结果：难度 1-5 与判定用的模型（空 = 规则估计）。 */
export interface ChatRouteMeta {
  mode?: string;
  difficulty?: number;
  reason?: string;
  judge_model?: string;
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

function taskGoal(): string {
  try {
    return sessionStorage.getItem("openmathmodelPrompt")?.trim() ?? "";
  } catch {
    return "";
  }
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

/**
 * 发送一轮对话并返回完整回复。首轮把任务目标并入用户消息作为背景，
 * 保持 user/assistant 交替（Anthropic 协议要求严格交替）。
 */
export async function sendConversationTurn(
  text: string,
  handlers: ChatHandlers = {},
): Promise<{ text: string; reasoning: string; meta: ChatMeta }> {
  const goal = history.length === 0 ? taskGoal() : "";
  const content = goal ? `【当前建模任务】${goal}\n\n${text}` : text;
  history.push({ role: "user", content });
  trimHistory();

  let response: Response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
      body: JSON.stringify({ messages: [...history], ...routeSelection() }),
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
  return { text: full, reasoning, meta };
}

/** 切换任务/页面时清空上下文（当前按页面刷新自然重置，预留给控制器调用）。 */
export function resetConversation(): void {
  history.length = 0;
}
