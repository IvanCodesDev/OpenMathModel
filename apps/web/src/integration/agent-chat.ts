/**
 * 任务页 / 首页对话区的真实回复通道。
 *
 * 有归属（任务 run_… / 首页对话 chat_…）的对话走**服务端托管轮**（ADR-0016）：
 * POST /api/chat/turns 建轮即返回，模型调用在服务端后台进行，页面只是经 SSE
 * 附着直播的观众——切任务、刷新、关标签页都不影响生成；重进按 scope 拉全部轮，
 * 仍在生成的那一轮从 last_seq 续接（hydrateConversation / attachConversationTurn）。
 * 记录存服务端 chat_turns（「保存任务历史」关闭时只在服务端内存托管）。
 *
 * 没有归属的页面（演示态）退回无状态的 POST /api/chat：对话历史随请求携带，
 * 服务端不落库，浏览器断连即中断。
 *
 * 模型看到的上下文（history）仍由页面维护并随每次发起携带：托管轮只托管
 * 「这一轮」的生成与记录，不替页面拼上下文。
 */

import type { ChatImagePayload } from "../attachments/image-passthrough";
import { collectTaskAttachmentContext, resetTaskAttachmentContext } from "../attachments/task-attachment-context";
import { loadConversationLog, type ConversationLogEntry } from "../tasks/conversation-log";
import { currentChatMode } from "./chat-mode";
import {
  ChatError,
  errorFromResponse,
  fetchChatTurn,
  listChatTurns,
  parseSseChunk,
  readChatTurnEvents,
  startChatTurn,
  stopChatTurn,
  type ChatMeta,
  type ChatRouteMeta,
  type ChatTurnEvent,
  type ChatTurnView,
  type RunControlAction,
} from "./chat-turns-api";
import { actionTraceRow } from "./run-control-view";

export { ChatError };
export type { ChatMeta, ChatRouteMeta, ChatTurnView, RunControlAction };

export interface ChatHandlers {
  /** 每个增量回调一次；full 为累计文本，直接渲染即可。 */
  onDelta?: (delta: string, full: string) => void;
  /** 思考型模型的推理增量（DeepSeek reasoning_content / Claude thinking 等）。 */
  onReasoning?: (delta: string, full: string) => void;
  /** 首个事件（流式）或响应返回（非流式）时回调，携带实际接口与模型。 */
  onMeta?: (meta: ChatMeta) => void;
  /** 托管轮建立（拿到服务端轮 id）时回调：页面层据此在回复完成后补写轨迹行。 */
  onTurnStarted?: (turn: ChatTurnView) => void;
  /**
   * 服务端在**回复结束后**执行 / 提议的运行控制动作（ADR-0018 / ADR-0020），每条回调一次，
   * 晚于全部正文、早于终态；重进续接时按视图里已有的 meta.actions 逐条重放。
   */
  onAction?: (action: RunControlAction) => void;
}

export interface ChatTurnResult {
  text: string;
  reasoning: string;
  meta: ChatMeta;
  /** 托管轮的服务端 id；无状态通道为 null。 */
  turnId: string | null;
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

/** 当前对话绑定的归属与题面；null = 演示/无运行页面（沿用旧 sessionStorage 键）。 */
let scopeRunId: string | null = null;
let scopeGoal = "";

/**
 * 对话纪元：每次换绑/解绑归属自增。在途的对话轮记下发起时的纪元，完成时
 * 若纪元已变（用户中途切了归属），就不再碰当前 history——那已经是别人的上下文。
 */
let conversationEpoch = 0;

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
 * 绑定当前对话所属的归属：切到另一个任务时清空上下文；同一归属内重复调用只
 * 更新题面。这是「点最近任务进来不串上一个任务数据」的关键闸门。
 *
 * 本机对话记录（tasks/conversation-log）里的旧条目先进上下文——那是托管轮
 * 上线前落盘的历史，只读、不再写入；服务端的轮随后由 hydrateConversation 接上。
 */
export function configureConversation(runId: string | null, goal = ""): void {
  if (scopeRunId === runId) {
    scopeGoal = goal || scopeGoal;
    return;
  }
  conversationEpoch += 1;
  scopeRunId = runId;
  scopeGoal = goal;
  history.length = 0;
  routeState = null;
  // 任务附件的清单缓存与「已注入」标记也是上一个归属的页面态，一并清掉。
  resetTaskAttachmentContext();
  if (!runId) return;
  const entries = loadConversationLog(runId);
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry.role === "assistant" && entry.opening) {
      // 开场分析在记录里只有回复：按原始指令补出用户轮，保持 user/assistant 交替。
      history.push({ role: "user", content: goalPrefixed(history.length === 0 ? scopeGoal : "", OPENING_ANALYSIS_PROMPT) });
      history.push({ role: "assistant", content: entry.text });
    } else if (entry.role === "user") {
      // 回复被打断且一字未收的一对：页面上照常显示，但不进模型上下文——空的
      // assistant 轮各家协议都不认，跳过 assistant 又会出现连续两条 user。
      const reply = entries[index + 1];
      if (reply?.role === "assistant" && reply.interrupted && !reply.text) {
        index += 1;
        continue;
      }
      history.push({ role: "user", content: goalPrefixed(history.length === 0 ? scopeGoal : "", entry.text) });
    } else if (entry.text) {
      history.push({ role: "assistant", content: entry.text });
    }
  }
  trimHistory();
}

/** 一轮已定格的托管轮进模型上下文；生成中或没有正文的轮不进（空 assistant 各家协议不认）。 */
function pushTurnToHistory(turn: { opening: boolean; text: string }, reply: string): void {
  if (!reply) return;
  const userText = turn.opening ? OPENING_ANALYSIS_PROMPT : turn.text;
  history.push({ role: "user", content: goalPrefixed(history.length === 0 ? scopeGoal : "", userText) });
  history.push({ role: "assistant", content: reply });
  trimHistory();
}

/**
 * 拉取当前归属在服务端的全部对话轮：已定格的进模型上下文，返回全部轮供页面
 * 重建现场（running 的那一轮由页面层 attachConversationTurn 续接直播）。
 * 后端不可达 / 旧后端没有该路由时返回空数组，页面只剩本机记录兜底。
 */
export async function hydrateConversation(scopeId: string): Promise<ChatTurnView[]> {
  const epoch = conversationEpoch;
  let turns: ChatTurnView[];
  try {
    turns = await listChatTurns(scopeId);
  } catch {
    return [];
  }
  // 等待期间归属已变（用户又切走了）：这批轮不是当前上下文的
  if (conversationEpoch !== epoch || scopeRunId !== scopeId) return [];
  for (const turn of turns) {
    if (turn.status !== "running") pushTurnToHistory(turn, turn.reply);
  }
  return turns;
}

/**
 * 已定格的托管轮 → 页面条目形态（与本机旧记录同构，任务页与首页共用一套渲染）。
 * 非正常完成的轮（暂停 / 失败 / 服务重启中断 / 中途出错）半截正文照留，
 * note 如实说明原因；页面层在正文下方以一行灰字展示。
 */
export function entryFromTurn(turn: ChatTurnView): ConversationLogEntry {
  let note = "";
  if (turn.status === "stopped") {
    note = turn.reply ? "已暂停生成，以上为暂停前已生成的部分。" : "已暂停生成。";
  } else if (turn.status === "failed" || turn.status === "interrupted") {
    note = turn.error?.message ? `回复生成中断：${turn.error.message}` : "回复生成中断，请重新发送。";
  } else if (turn.status === "completed" && turn.meta?.error?.message) {
    note = `回复在生成中出错，以上为出错前已生成的部分。${turn.meta.error.message}`;
  }
  // 运行控制回执（ADR-0018）随轮落在 meta.actions；页面侧补写的 trace 已含这些行
  // （发起 / 续接的那一页在收到事件时同步记入轨迹），只有轨迹没来得及补写
  // （回复未完成就离开）的轮才由回执重建，避免重复。
  const trace = turn.trace?.length ? turn.trace : (turn.meta?.actions ?? []).map(actionTraceRow);
  return {
    role: "assistant",
    text: turn.reply || "",
    turnId: turn.id,
    feedback: turn.feedback ?? null,
    ...(turn.opening ? { opening: true } : {}),
    ...(turn.reasoning ? { reasoning: turn.reasoning } : {}),
    ...(trace.length ? { trace } : {}),
    ...(note ? { interrupted: true, note } : {}),
  };
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

export interface ChatTurnOptions {
  /** 随消息发送的附件上下文块（ADR-0010 批次三）；只进请求内容，不进气泡展示。 */
  attachmentContext?: string;
  /** 本轮是系统自动发起的开场分析：记录只保留回复，不留用户气泡。 */
  openingAnalysis?: boolean;
  /** 随消息发送的附件名：进入对话记录，恢复时重建纸夹徽标。 */
  attachmentNames?: string[];
  /** 直通给视觉模型的原图（ADR-0010 直通）：仅本条消息生效，历史轮不重发。 */
  images?: ChatImagePayload[];
  /** 携图时钉住的接口 id：绕过 Auto 难度路由，确保图片落在视觉模型上。 */
  pinEndpointId?: string;
  /** 暂停生成：托管轮向服务端发 stop（已生成的部分定格为完整回复）；无状态通道
   *  中止请求。一字未收则抛 GENERATION_STOPPED，由调用方安静收尾（不按错误渲染）。 */
  signal?: AbortSignal;
}

interface StreamOutcome {
  text: string;
  reasoning: string;
  meta: ChatMeta;
  turnId: string | null;
}

/**
 * 发送一轮对话并返回完整回复。首轮把任务目标并入用户消息作为背景，
 * 保持 user/assistant 交替（Anthropic 协议要求严格交替）。
 */
export async function sendConversationTurn(
  text: string,
  handlers: ChatHandlers = {},
  options: ChatTurnOptions = {},
): Promise<ChatTurnResult> {
  const turnScope = scopeRunId;
  const epoch = conversationEpoch;
  const goal = history.length === 0 ? taskGoal() : "";
  // 任务附件（首页上传的项目产物）与随消息附件（对话框托盘）是互补的两条来源：
  // 前者按解析就绪进度逐轮并入，后者由调用方通过 attachmentContext 传入。
  // 任务附件只属于任务归属（run_…）：首页对话与演示态没有任务，也绝不能去读
  // 标签页里上一个任务页留下的身份——那正是「新开对话却延续了上一个任务」的来源。
  const taskAttachments = turnScope !== null && turnScope.startsWith("run_")
    ? await collectTaskAttachmentContext(turnScope)
    : null;
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
  // 纪元一变（退出/切任务），history 已被清空或重建，本轮的用户消息早已不在
  // 其中：失败回滚只在纪元未变时进行，否则会误删别人的上下文。
  const rollbackUserTurn = (): void => {
    if (conversationEpoch === epoch) history.pop();
  };

  // 携图直通时钉住视觉接口：Auto 难度路由看不见图片，可能把消息发给纯文本模型。
  const images = options.images?.length ? options.images : undefined;
  const routing = images && options.pinEndpointId
    ? { endpoint_id: options.pinEndpointId }
    : routeSelection();
  const body = {
    messages: [...history],
    ...routing,
    ...routeExtras(routing, text),
    ...(images ? { images } : {}),
  };

  let outcome: StreamOutcome;
  try {
    outcome = turnScope
      ? await runHostedTurn(turnScope, text, options, body, handlers)
      : await runStatelessTurn(body, handlers, options.signal);
  } catch (error) {
    rollbackUserTurn();
    throw error;
  }
  const { text: full, meta } = outcome;

  // 思考过程（outcome.reasoning）只用于展示，不进对话历史（回传会浪费上下文且各家协议不认）。
  // 纪元未变：正常接在本轮用户消息后。纪元变了但用户已回到同一归属：history
  // 已按记录重建、缺这轮往返，补回去；归属已是别人（切了任务/新对话）：不碰。
  if (conversationEpoch === epoch) {
    history.push({ role: "assistant", content: full });
    trimHistory();
  } else if (turnScope && scopeRunId === turnScope && history[history.length - 1]?.role !== "user") {
    // 末尾是 user 说明用户已另发一轮在途消息——此时不插，保住 user/assistant
    // 严格交替（Anthropic 协议要求）；上下文里少这一轮，记录不受影响。
    history.push({ role: "user", content });
    history.push({ role: "assistant", content: full });
    trimHistory();
  }
  // Auto 路由状态推进：judged=true 表示服务端真的花了一次判定（计数清零），
  // 否则累加继承轮数，攒够后服务端会强制重判一次。纪元变了不推进——
  // routeState 已随换绑重置，别把上一段对话的难度状态污染进新对话。
  const route = meta.route;
  if (conversationEpoch === epoch && route?.mode === "auto" && typeof route.difficulty === "number") {
    routeState = {
      difficulty: route.difficulty,
      endpointId: route.endpoint_id ?? routeState?.endpointId,
      turns: route.judged ? 0 : (routeState?.turns ?? 0) + 1,
    };
  }
  // 发送成功才把任务附件标记为已注入；失败路径保留，下一轮重新并入。
  taskAttachments?.commit();
  return outcome;
}

// ── 托管轮（有归属） ──────────────────────────────────────────────────────────

async function runHostedTurn(
  scopeId: string,
  text: string,
  options: ChatTurnOptions,
  body: Record<string, unknown>,
  handlers: ChatHandlers,
): Promise<StreamOutcome> {
  if (options.signal?.aborted) throw new ChatError("GENERATION_STOPPED", "已暂停生成");
  // 建轮不挂 signal：请求一旦发出，服务端就该把这一轮生成完，页面此刻走人也一样
  const turn = await startChatTurn({
    ...(body as Omit<Parameters<typeof startChatTurn>[0], "scope_id" | "text" | "opening" | "attachments">),
    scope_id: scopeId,
    text,
    opening: options.openingAnalysis === true,
    attachments: options.attachmentNames ?? [],
  });
  handlers.onTurnStarted?.(turn);
  return followTurn(turn, handlers, options.signal);
}

/**
 * 续接一轮服务端仍在生成的托管轮（重进页面时由页面层调用）：从视图里的
 * last_seq 附着直播，完成后把这一轮补进模型上下文（hydrate 时它还在生成、没进）。
 */
export async function attachConversationTurn(
  turn: ChatTurnView,
  handlers: ChatHandlers = {},
  signal?: AbortSignal,
): Promise<ChatTurnResult> {
  const epoch = conversationEpoch;
  const result = await followTurn(turn, handlers, signal);
  if (conversationEpoch === epoch && scopeRunId === turn.scope_id && history[history.length - 1]?.role !== "user") {
    pushTurnToHistory(turn, result.text);
  }
  return result;
}

interface FollowState {
  full: string;
  reasoning: string;
  meta: ChatMeta;
  seq: number;
  terminal: ChatTurnEvent | null;
}

/** 与服务端 chat_turns.terminal_event_for 同一口径：按定格状态合成终态事件。 */
function terminalEventFor(view: ChatTurnView): ChatTurnEvent {
  if (view.status === "completed") {
    return {
      type: "done",
      usage: view.meta.usage,
      elapsed_ms: view.meta.elapsed_ms,
      ...(view.meta.error ? { partial: true, error: view.meta.error } : {}),
    };
  }
  if (view.status === "stopped") {
    if (view.reply || view.reasoning) return { type: "done", stopped: true };
    return { type: "error", code: "GENERATION_STOPPED", message: "已暂停生成" };
  }
  return {
    type: "error",
    code: view.error?.code ?? (view.status === "interrupted" ? "GENERATION_INTERRUPTED" : "CHAT_FAILED"),
    message: view.error?.message ?? "回复生成失败",
  };
}

/** meta 事件正文并入累计 meta（去掉事件封装字段 type / seq）。 */
function mergeMeta(target: ChatMeta, event: ChatTurnEvent): void {
  for (const [key, value] of Object.entries(event)) {
    if (key === "type" || key === "seq") continue;
    (target as Record<string, unknown>)[key] = value;
  }
}

/** action 事件正文 → 回执条目（去掉事件封装字段），与服务端 meta.actions 的形态一致。 */
function actionOf(event: ChatTurnEvent): RunControlAction {
  const action: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(event)) {
    if (key === "type" || key === "seq") continue;
    action[key] = value;
  }
  return action as unknown as RunControlAction;
}

function applyEvent(event: ChatTurnEvent, state: FollowState, handlers: ChatHandlers): void {
  if (typeof event.seq === "number") state.seq = event.seq;
  if (event.type === "meta") {
    mergeMeta(state.meta, event);
    handlers.onMeta?.(state.meta);
  } else if (event.type === "action") {
    const action = actionOf(event);
    state.meta.actions = [...(state.meta.actions ?? []), action];
    handlers.onAction?.(action);
  } else if (event.type === "delta" && typeof event.text === "string") {
    state.full += event.text;
    handlers.onDelta?.(event.text, state.full);
  } else if (event.type === "reasoning" && typeof event.text === "string") {
    state.reasoning += event.text;
    handlers.onReasoning?.(event.text, state.reasoning);
  } else if (event.type === "done") {
    if (event.usage) state.meta.usage = event.usage;
    if (typeof event.elapsed_ms === "number") state.meta.elapsed_ms = event.elapsed_ms;
    if (event.stopped) state.meta.stopped = true;
    if (event.error) state.meta.error = event.error;
    state.terminal = event;
  } else if (event.type === "error") {
    state.terminal = event;
  }
}

/** 连接在终态前断了、回读到的定格视图：把页面没看到的那截补给渲染层，再按状态收尾。 */
function applyTerminalView(view: ChatTurnView, state: FollowState, handlers: ChatHandlers): void {
  if (view.reasoning.length > state.reasoning.length) {
    const missing = view.reasoning.slice(state.reasoning.length);
    state.reasoning = view.reasoning;
    handlers.onReasoning?.(missing, state.reasoning);
  }
  if (view.reply.length > state.full.length) {
    const missing = view.reply.slice(state.full.length);
    state.full = view.reply;
    handlers.onDelta?.(missing, state.full);
  }
  Object.assign(state.meta, view.meta);
  applyEvent(terminalEventFor(view), state, handlers);
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => {
    window.setTimeout(resolve, ms);
  });
}

/** 附着重连上限：超过后不再等，回复仍在服务端后台生成，刷新页面可续接。 */
const FOLLOW_MAX_ATTEMPTS = 5;
/** stop 发出后服务端应立刻收尾；超过这个时长仍没收到终态就放弃观看，按已收到的部分定格。 */
const STOP_GRACE_MS = 3000;

async function followTurn(turn: ChatTurnView, handlers: ChatHandlers, signal?: AbortSignal): Promise<StreamOutcome> {
  const state: FollowState = {
    full: turn.reply,
    reasoning: turn.reasoning,
    meta: { ...turn.meta },
    seq: turn.last_seq,
    terminal: null,
  };
  // 重新附着时 meta 事件与已生成的那截早已过去：先把视图里的 meta（路由行、域名行）
  // 与半截思考/正文交给页面层上屏，随后的事件从 last_seq 起接。运行控制回执在回复
  // 结束后才发出（ADR-0020）：仍在生成的轮视图里通常还没有，已定格的轮从视图重放。
  for (const action of turn.meta?.actions ?? []) handlers.onAction?.(action);
  if (Object.keys(turn.meta ?? {}).length > 0) handlers.onMeta?.(state.meta);
  if (state.reasoning) handlers.onReasoning?.(state.reasoning, state.reasoning);
  if (state.full) handlers.onDelta?.(state.full, state.full);
  if (turn.status !== "running") {
    applyEvent(terminalEventFor(turn), state, handlers);
    return settleTurn(state, turn.id);
  }

  // 观看用独立的中止句柄：用户的暂停不是「不看了」，而是让服务端停——服务端
  // 定格后会向所有观众发终态事件，流自然收尾；stop 送达失败才放弃观看。
  const watcher = new AbortController();
  let stopTimer: number | undefined;
  const onAbort = (): void => {
    stopTimer = window.setTimeout(() => watcher.abort(), STOP_GRACE_MS);
    void stopChatTurn(turn.id).catch(() => watcher.abort());
  };
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });

  try {
    for (let attempt = 0; state.terminal === null && !watcher.signal.aborted; attempt += 1) {
      let gotTerminal = false;
      try {
        gotTerminal = await readChatTurnEvents(turn.id, state.seq, event => applyEvent(event, state, handlers), watcher.signal);
      } catch (error) {
        if (watcher.signal.aborted) break;
        if (error instanceof ChatError && error.code !== "NETWORK_ERROR") throw error;
        if (attempt + 1 >= FOLLOW_MAX_ATTEMPTS) throw error;
      }
      if (gotTerminal || watcher.signal.aborted) break;
      // 连接在终态前结束：回读视图——仍在生成就从上次 seq 续接，已定格就按状态收尾
      const view = await fetchChatTurn(turn.id).catch(() => null);
      if (view && view.status !== "running") {
        applyTerminalView(view, state, handlers);
        break;
      }
      if (attempt + 1 >= FOLLOW_MAX_ATTEMPTS) {
        throw new ChatError("NETWORK_ERROR", "与服务的连接反复中断；回复仍在后台生成，刷新页面可继续查看");
      }
      await delay(400 * (attempt + 1));
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    if (stopTimer !== undefined) window.clearTimeout(stopTimer);
  }

  if (state.terminal === null) {
    // stop 送达失败 / 服务端没能及时收尾：按已收到的部分定格（服务端那边的状态以它为准）
    if (!state.full) throw new ChatError("GENERATION_STOPPED", "已暂停生成");
    state.meta.stopped = true;
  }
  return settleTurn(state, turn.id);
}

function settleTurn(state: FollowState, turnId: string): StreamOutcome {
  const terminal = state.terminal;
  if (terminal?.type === "error") {
    if (!state.full) {
      throw new ChatError(String(terminal.code ?? "CHAT_FAILED"), String(terminal.message ?? "回复生成失败"));
    }
    state.meta.error = { code: String(terminal.code ?? "CHAT_FAILED"), message: String(terminal.message ?? "") };
  }
  if (!state.full) {
    if (terminal?.stopped || state.meta.stopped) throw new ChatError("GENERATION_STOPPED", "已暂停生成");
    throw new ChatError("EMPTY_REPLY", "模型没有返回内容，请重试或检查接口配置");
  }
  return { text: state.full, reasoning: state.reasoning, meta: state.meta, turnId };
}

// ── 无状态通道（无归属的演示页） ──────────────────────────────────────────────

async function runStatelessTurn(
  body: Record<string, unknown>,
  handlers: ChatHandlers,
  signal?: AbortSignal,
): Promise<StreamOutcome> {
  let response: Response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ChatError("GENERATION_STOPPED", "已暂停生成");
    }
    throw new ChatError("NETWORK_ERROR", "无法连接服务，请确认后端已启动");
  }
  if (!response.ok) throw await errorFromResponse(response);

  const contentType = response.headers.get("Content-Type") ?? "";
  let full = "";
  let reasoning = "";
  const meta: ChatMeta = {};

  if (contentType.includes("text/event-stream") && response.body) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let failure: ChatError | null = null;
    let stopped = false;
    for (;;) {
      let done: boolean;
      let value: Uint8Array | undefined;
      try {
        ({ done, value } = await reader.read());
      } catch (error) {
        // 用户点了暂停：已收到的部分按完整回复处理；一字未收按停止收尾
        if (error instanceof DOMException && error.name === "AbortError") {
          stopped = true;
          break;
        }
        throw error;
      }
      if (value) buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSseChunk(done ? `${buffer}\n\n` : buffer);
      buffer = done ? "" : rest;
      for (const event of events) {
        if (event.type === "meta") {
          mergeMeta(meta, event);
          handlers.onMeta?.(meta);
        } else if (event.type === "delta" && typeof event.text === "string") {
          full += event.text;
          handlers.onDelta?.(event.text, full);
        } else if (event.type === "reasoning" && typeof event.text === "string") {
          reasoning += event.text;
          handlers.onReasoning?.(event.text, reasoning);
        } else if (event.type === "done") {
          meta.usage = event.usage;
          meta.elapsed_ms = event.elapsed_ms;
        } else if (event.type === "error") {
          failure = new ChatError(String(event.code ?? "CHAT_FAILED"), String(event.message ?? "对话请求失败"));
        }
      }
      if (done) break;
    }
    if (stopped && !full) throw new ChatError("GENERATION_STOPPED", "已暂停生成");
    if (stopped) meta.stopped = true;
    if (failure && !full) throw failure;
  } else {
    let payload: ChatMeta & { reply?: string; reasoning?: string };
    try {
      payload = (await response.json()) as ChatMeta & { reply?: string; reasoning?: string };
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ChatError("GENERATION_STOPPED", "已暂停生成");
      }
      throw new ChatError("CHAT_FAILED", "对话响应解析失败，请稍后再试");
    }
    full = payload.reply ?? "";
    reasoning = payload.reasoning ?? "";
    Object.assign(meta, payload);
    delete (meta as Record<string, unknown>).reply;
    delete (meta as Record<string, unknown>).reasoning;
    handlers.onMeta?.(meta);
    if (reasoning) handlers.onReasoning?.(reasoning, reasoning);
    if (full) handlers.onDelta?.(full, full);
  }

  if (!full) throw new ChatError("EMPTY_REPLY", "模型没有返回内容，请重试或检查接口配置");
  return { text: full, reasoning, meta, turnId: null };
}

/** 切换任务/页面时清空上下文与运行绑定（预留给控制器调用）。 */
export function resetConversation(): void {
  conversationEpoch += 1;
  history.length = 0;
  scopeRunId = null;
  scopeGoal = "";
  routeState = null;
}
