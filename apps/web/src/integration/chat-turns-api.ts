/**
 * 服务端托管对话轮的类型化客户端（ADR-0016）。
 *
 * 一轮对话是服务端的后台作业：POST 建轮立即返回视图，生成在服务端继续；页面
 * 经 SSE 附着直播（`readChatTurnEvents`），断开只影响观众。重进按 scope 拉全部
 * 轮（`listChatTurns`），running 的轮带着半截正文与 `last_seq`，从那一位继续附着。
 * 「暂停生成」是 `stopChatTurn`——不再是掐断 fetch（那只会让页面看不见，服务端
 * 照样在生成）。
 */

import type { ConversationTraceRow } from "../tasks/conversation-log";

export type ChatTurnStatus = "running" | "completed" | "failed" | "stopped" | "interrupted";

/** 用户对一轮回复的评价（回复右下角的赞 / 踩）；null = 未评价或已撤回。 */
export type ChatTurnFeedback = "up" | "down" | null;

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

/**
 * 对话即控制面（ADR-0018）：服务端在生成回复前识别并执行的运行控制动作回执。
 * `executed` 已生效；`proposed` 等用户确认（取消任务）；`rejected` 判出了意图但
 * 状态机不允许；`dismissed` 上一轮的提案被放弃。
 */
export interface RunControlAction {
  kind: "retry" | "resume" | "pause" | "cancel" | "approve" | "reject" | "revision" | "redo" | string;
  status: "executed" | "proposed" | "rejected" | "dismissed" | string;
  message?: string;
  stage?: string;
  stage_label?: string;
  /** redo 提案里带的用户原话：确认后作为要求落成备注（ADR-0019）。 */
  text?: string;
  note_id?: string | null;
  approval_id?: string;
  approval_title?: string;
  option_id?: string;
  option_label?: string;
  round?: number;
  confirm_hint?: string;
  code?: string;
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
  /** 用户中途暂停、已收到的部分按完整回复收尾时为 true。 */
  stopped?: boolean;
  /** 已有正文后上游报错：正文照留，错误如实记在这里（页面按完成收口）。 */
  error?: { code: string; message: string };
  /** 本轮服务端执行 / 提议的运行控制动作（ADR-0018），刷新后据此重画轨迹行。 */
  actions?: RunControlAction[];
}

export interface ChatTurnView {
  id: string;
  scope_id: string;
  status: ChatTurnStatus;
  opening: boolean;
  text: string;
  attachments: string[];
  reply: string;
  reasoning: string;
  meta: ChatMeta;
  error: { code: string; message: string } | null;
  trace: ConversationTraceRow[] | null;
  /** 赞 / 踩；旧后端的视图没有这一项，读取时按未评价处理。 */
  feedback?: ChatTurnFeedback;
  /** 附着直播的游标：视图里的 reply/reasoning 恰好包含前 last_seq 个事件。 */
  last_seq: number;
  /** 服务端内存里仍有事件缓冲（生成中或刚结束）；false = 只剩库里定格的文本。 */
  live: boolean;
  persisted: boolean;
  created_at: string;
  updated_at: string;
  ended_at: string | null;
}

export class ChatError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export async function errorFromResponse(response: Response): Promise<ChatError> {
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

async function request(input: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(input, {
      credentials: "same-origin",
      ...init,
      headers: { Accept: "application/json", ...(init.headers ?? {}) },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ChatError("NETWORK_ERROR", "无法连接服务，请确认后端已启动");
  }
  if (!response.ok) throw await errorFromResponse(response);
  return response;
}

async function turnOf(response: Response): Promise<ChatTurnView> {
  const payload = (await response.json()) as { turn: ChatTurnView };
  return payload.turn;
}

export interface StartChatTurnBody {
  scope_id: string;
  text: string;
  opening: boolean;
  attachments: string[];
  messages: Array<{ role: "user" | "assistant"; content: string }>;
  route?: string;
  endpoint_id?: string;
  route_question?: string;
  route_context?: string;
  route_state?: { difficulty: number; endpoint_id?: string; turns: number };
  images?: unknown[];
}

/** 发起一轮托管对话：202 即返回视图（status=running），随后用 readChatTurnEvents 附着。 */
export async function startChatTurn(body: StartChatTurnBody, signal?: AbortSignal): Promise<ChatTurnView> {
  return turnOf(await request("/api/chat/turns", {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function fetchChatTurn(turnId: string, signal?: AbortSignal): Promise<ChatTurnView> {
  return turnOf(await request(`/api/chat/turns/${encodeURIComponent(turnId)}`, { signal }));
}

/** scope（run_… / chat_…）下全部轮，按发起时间升序。 */
export async function listChatTurns(scopeId: string, signal?: AbortSignal): Promise<ChatTurnView[]> {
  const response = await request(`/api/chat/scopes/${encodeURIComponent(scopeId)}/turns`, { signal });
  const payload = (await response.json()) as { items: ChatTurnView[] };
  return payload.items ?? [];
}

/** 「暂停生成」：服务端定格已生成的部分并向所有观众发终态事件。 */
export async function stopChatTurn(turnId: string): Promise<ChatTurnView> {
  return turnOf(await request(`/api/chat/turns/${encodeURIComponent(turnId)}/stop`, { method: "POST" }));
}

/** 补写页面侧回复轨迹行，重进时原样重建。 */
export async function patchChatTurnTrace(turnId: string, trace: ConversationTraceRow[]): Promise<ChatTurnView> {
  return turnOf(await request(`/api/chat/turns/${encodeURIComponent(turnId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trace }),
  }));
}

/** 赞 / 踩这一轮回复：整体置值，null 撤回；返回更新后的视图。 */
export async function setChatTurnFeedback(turnId: string, feedback: ChatTurnFeedback): Promise<ChatTurnView> {
  return turnOf(await request(`/api/chat/turns/${encodeURIComponent(turnId)}/feedback`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback }),
  }));
}

/** 清空 scope 下的对话记录（删除首页对话 / 清除任务对话）。失败静默：本机侧照常清理。 */
export async function deleteChatScope(scopeId: string): Promise<void> {
  try {
    await request(`/api/chat/scopes/${encodeURIComponent(scopeId)}`, { method: "DELETE" });
  } catch {
    // 后端不可达或旧后端没有该路由：记录留在服务端，下次能连上时用户仍可再删
  }
}

/** SSE 事件（服务端每个事件带 seq；终态事件是 done / error）。 */
export interface ChatTurnEvent {
  type: string;
  seq?: number;
  text?: string;
  code?: string;
  message?: string;
  usage?: ChatMeta["usage"];
  elapsed_ms?: number;
  stopped?: boolean;
  partial?: boolean;
  error?: { code: string; message: string };
  [key: string]: unknown;
}

export function parseSseChunk(buffer: string): { events: ChatTurnEvent[]; rest: string } {
  const events: ChatTurnEvent[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    for (const line of block.split("\n")) {
      if (!line.startsWith("data:")) continue;
      try {
        events.push(JSON.parse(line.slice(5).trim()) as ChatTurnEvent);
      } catch {
        // 半截或非 JSON 行按空事件跳过
      }
    }
  }
  return { events, rest };
}

/**
 * 附着到一轮托管对话的事件流：回放 after 之后的事件并跟随到终态。
 * 每个事件回调一次；返回是否收到了终态事件（false = 连接在终态前结束，
 * 调用方应回读视图决定是重连还是收尾）。signal 只用于放弃**观看**，不影响生成。
 */
export async function readChatTurnEvents(
  turnId: string,
  after: number,
  onEvent: (event: ChatTurnEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  const response = await request(
    `/api/chat/turns/${encodeURIComponent(turnId)}/events?after=${Math.max(0, after)}`,
    { signal, headers: { Accept: "text/event-stream" } },
  );
  if (!response.body) return false;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    const { events, rest } = parseSseChunk(done ? `${buffer}\n\n` : buffer);
    buffer = done ? "" : rest;
    for (const event of events) {
      onEvent(event);
      if (event.type === "done" || event.type === "error") terminal = true;
    }
    if (done || terminal) break;
  }
  if (terminal) {
    try {
      await reader.cancel();
    } catch {
      // 服务端已收尾，取消只是释放连接
    }
  }
  return terminal;
}
