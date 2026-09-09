/**
 * 按对话归属隔离的本机对话记录（只读兜底）。
 *
 * 对话记录自 ADR-0016 起存服务端（chat_turns，经 /api/chat/turns 系列接口读写）：
 * 生成是服务端的后台作业，页面断连不再丢轮。本模块保留的是托管轮上线前落在
 * localStorage 里的旧记录——重进任务时照常渲染在服务端记录之前，不再写入新条目；
 * 删除任务 / 关闭「保存任务历史」时随服务端记录一起清掉。零依赖，可直接在
 * Node 测试里加载。
 *
 * 归属 id 有两种，键空间不重叠：任务页对话用运行 `run_…`；首页普通对话
 * 不建项目、没有运行，用 tasks/chat-sessions 发的 `chat_…`。
 */

const LOG_KEY_PREFIX = "openmathmodel.chatLog.v1.";
/** 上一代「在途轮」记录的键前缀：已废弃，清理时一并删除。 */
const LEGACY_PENDING_KEY_PREFIX = "openmathmodel.chatPending.v1.";
const SCOPE_ID_PATTERN = /^(?:run|chat)_[0-9a-f]{32}$/;
const MAX_NOTE_CHARS = 300;

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
  /** 回复的执行轨迹（附件解析/难度路由/运行控制回执等真实过程）。 */
  trace?: ConversationTraceRow[];
  /**
   * 回复的思考过程（推理型模型）：只用于恢复「已思考」回看盒，不回传模型
   * （agent-chat 的请求历史仍然只有正文）。
   */
  reasoning?: string;
  /**
   * 回复在生成中被打断（接口报错 / 用户暂停 / 服务重启）：text 是打断前已收到的
   * 半截正文（可能为空），reasoning 同理；note 说明原因。这是唯一允许空 text 的
   * 条目形态——用户的提问必须留下来，不能因为回复没收完就连问题一起消失。
   * 服务端的托管轮按同一形态映射到页面（见页面层 entryFromTurn）。
   */
  interrupted?: boolean;
  note?: string;
  /**
   * 服务端托管轮的 id 与用户评价（赞 / 踩）：只有由 entryFromTurn 映射来的条目才有，
   * 回复右下角的评价按钮据此回显并回写；本机旧记录没有服务端实体，不带这两项。
   */
  turnId?: string;
  feedback?: "up" | "down" | null;
}

/** 轨迹上限：行数与字段长度都收口（服务端 PATCH 与本机旧记录同一口径）。 */
const MAX_TRACE_ROWS = 8;
const MAX_TRACE_FIELD_CHARS = 600;

export function sanitizeTrace(value: unknown): ConversationTraceRow[] {
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
      interrupted?: unknown; note?: unknown;
    };
    if (entry?.role !== "user" && entry?.role !== "assistant") continue;
    if (typeof entry.text !== "string") continue;
    // 被打断的回复允许空正文（问题要留下，回复可能一字未收）；其余条目空文本视为坏数据
    const interrupted = entry.role === "assistant" && entry.interrupted === true;
    if (!entry.text && !interrupted) continue;
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
      ...(interrupted ? { interrupted: true } : {}),
      ...(interrupted && typeof entry.note === "string" && entry.note ? { note: entry.note.slice(0, MAX_NOTE_CHARS) } : {}),
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

/** 删除任务或对话时调用：清掉该归属的本机旧记录（服务端记录由调用方另行删除）。 */
export function clearConversationLog(scopeId: string): void {
  try {
    localStorage.removeItem(keyFor(scopeId));
    localStorage.removeItem(LEGACY_PENDING_KEY_PREFIX + scopeId);
  } catch {
    // 没有存储就没有记录可清。
  }
}

/** 关闭「保存任务历史」时调用：清空本机全部旧对话记录（服务端记录由服务端同步清空）。 */
export function clearAllConversationLogs(): void {
  try {
    const doomed: string[] = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key?.startsWith(LOG_KEY_PREFIX) || key?.startsWith(LEGACY_PENDING_KEY_PREFIX)) doomed.push(key);
    }
    doomed.forEach(key => localStorage.removeItem(key));
  } catch {
    // 同上。
  }
}
