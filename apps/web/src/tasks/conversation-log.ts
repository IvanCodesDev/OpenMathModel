/**
 * 按对话归属隔离的本机对话记录。
 *
 * 服务端对话保持无状态（/api/chat 不落库），对话历史只存浏览器
 * localStorage：键按归属 id 隔离，互不可见；是否写入由调用方按
 * 「数据与隐私 → 保存任务历史」开关决定（本模块不做闸门，保持零依赖，
 * 与 last-task-record 一样可直接在 Node 测试里加载）。
 *
 * 归属 id 有两种，键空间不重叠：任务页对话用运行 `run_…`；首页普通对话
 * 不建项目、没有运行，用 tasks/chat-sessions 发的 `chat_…`。
 */

const LOG_KEY_PREFIX = "openmathmodel.chatLog.v1.";
const SCOPE_ID_PATTERN = /^(?:run|chat)_[0-9a-f]{32}$/;
/** 单条文本与总条数上限：控制 localStorage 占用；超长回复截断保留开头。 */
const MAX_ENTRY_CHARS = 20_000;
const MAX_ENTRIES = 80;

/** 回复执行轨迹的一行（已落定状态）：恢复对话时按原样重建过程区。 */
export interface ConversationTraceRow {
  icon: string;
  title: string;
  suffix?: string;
  detail?: string;
  /** 已落定的耗时文本（如 "12.3s"）；恢复时直接展示，不再走秒。 */
  elapsed?: string;
}

export interface ConversationLogEntry {
  role: "user" | "assistant";
  text: string;
  /** 系统自动发起的开场分析（没有对应的用户气泡）。 */
  opening?: boolean;
  /** 随消息发送的附件名，恢复时重建气泡下的纸夹徽标。 */
  attachments?: string[];
  /** 回复的执行轨迹（读取上下文/附件解析/难度路由/生成计时等真实过程）。 */
  trace?: ConversationTraceRow[];
  /**
   * 回复的思考过程（推理型模型）：只用于恢复「已思考」回看盒，不回传模型
   * （agent-chat 的请求历史仍然只有正文）。刷新前生成的思考内容因此不再丢失。
   */
  reasoning?: string;
}

/** 轨迹落盘上限：行数与字段长度都收口，控制 localStorage 占用。 */
const MAX_TRACE_ROWS = 8;
const MAX_TRACE_FIELD_CHARS = 600;

function sanitizeTrace(value: unknown): ConversationTraceRow[] {
  if (!Array.isArray(value)) return [];
  const rows: ConversationTraceRow[] = [];
  for (const item of value.slice(0, MAX_TRACE_ROWS)) {
    const row = item as Partial<ConversationTraceRow>;
    if (typeof row?.icon !== "string" || typeof row?.title !== "string" || !row.title) continue;
    rows.push({
      icon: row.icon.slice(0, 40),
      title: row.title.slice(0, 120),
      ...(typeof row.suffix === "string" && row.suffix ? { suffix: row.suffix.slice(0, 40) } : {}),
      ...(typeof row.detail === "string" && row.detail ? { detail: row.detail.slice(0, MAX_TRACE_FIELD_CHARS) } : {}),
      ...(typeof row.elapsed === "string" && row.elapsed ? { elapsed: row.elapsed.slice(0, 20) } : {}),
    });
  }
  return rows;
}

function keyFor(scopeId: string): string {
  return LOG_KEY_PREFIX + scopeId;
}

export function parseConversationLog(raw: string | null): ConversationLogEntry[] {
  if (!raw) return [];
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return [];
  }
  const entries = (payload as { entries?: unknown })?.entries;
  if (!Array.isArray(entries)) return [];
  const result: ConversationLogEntry[] = [];
  for (const item of entries) {
    const entry = item as {
      role?: unknown; text?: unknown; opening?: unknown; attachments?: unknown; trace?: unknown; reasoning?: unknown;
    };
    if (entry?.role !== "user" && entry?.role !== "assistant") continue;
    if (typeof entry.text !== "string" || !entry.text) continue;
    const attachments = Array.isArray(entry.attachments)
      ? entry.attachments.filter((name): name is string => typeof name === "string")
      : [];
    const trace = sanitizeTrace(entry.trace);
    result.push({
      role: entry.role,
      text: entry.text,
      ...(entry.opening === true ? { opening: true } : {}),
      ...(attachments.length > 0 ? { attachments } : {}),
      ...(trace.length > 0 ? { trace } : {}),
      ...(typeof entry.reasoning === "string" && entry.reasoning ? { reasoning: entry.reasoning } : {}),
    });
  }
  return result;
}

export function loadConversationLog(scopeId: string): ConversationLogEntry[] {
  if (!SCOPE_ID_PATTERN.test(scopeId)) return [];
  try {
    return parseConversationLog(localStorage.getItem(keyFor(scopeId)));
  } catch {
    return [];
  }
}

/** 追加一轮对话（用户消息与回复成对写入；开场分析只有回复一条）。 */
export function appendConversationEntries(scopeId: string, entries: ConversationLogEntry[]): void {
  if (!SCOPE_ID_PATTERN.test(scopeId) || entries.length === 0) return;
  const trimmed = entries.map(entry => ({
    ...entry,
    ...(entry.text.length > MAX_ENTRY_CHARS ? { text: entry.text.slice(0, MAX_ENTRY_CHARS) } : {}),
    ...(entry.reasoning && entry.reasoning.length > MAX_ENTRY_CHARS
      ? { reasoning: entry.reasoning.slice(0, MAX_ENTRY_CHARS) }
      : {}),
  }));
  const merged = [...loadConversationLog(scopeId), ...trimmed].slice(-MAX_ENTRIES);
  try {
    localStorage.setItem(keyFor(scopeId), JSON.stringify({ entries: merged, saved_at: Date.now() }));
  } catch {
    // 存储满或被禁用：本轮不落盘，对话仍在页面内存里继续。
  }
}

/**
 * 把执行轨迹补写到最近一条对话回复上：轨迹在回复完成后才最终落定
 * （生成耗时等），晚于回复本身的落盘。用回复文本校验目标条目，
 * 防止「保存任务历史」中途开关造成的错位。
 */
export function attachTraceToLastReply(scopeId: string, replyText: string, trace: ConversationTraceRow[]): void {
  if (!SCOPE_ID_PATTERN.test(scopeId) || trace.length === 0) return;
  const entries = loadConversationLog(scopeId);
  const last = [...entries].reverse().find(entry => entry.role === "assistant" && !entry.opening);
  if (!last || last.text !== replyText.slice(0, MAX_ENTRY_CHARS)) return;
  last.trace = sanitizeTrace(trace);
  try {
    localStorage.setItem(keyFor(scopeId), JSON.stringify({ entries, saved_at: Date.now() }));
  } catch {
    // 存储满或被禁用：轨迹不落盘，页面内展示不受影响。
  }
}

/** 删除任务或对话时调用：清掉该归属的本机对话记录。 */
export function clearConversationLog(scopeId: string): void {
  try {
    localStorage.removeItem(keyFor(scopeId));
  } catch {
    // 没有存储就没有记录可清。
  }
}

/** 关闭「保存任务历史」时调用：清空本机全部任务的对话记录。 */
export function clearAllConversationLogs(): void {
  try {
    const doomed: string[] = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key?.startsWith(LOG_KEY_PREFIX)) doomed.push(key);
    }
    doomed.forEach(key => localStorage.removeItem(key));
  } catch {
    // 同上。
  }
}
