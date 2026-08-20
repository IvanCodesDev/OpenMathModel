/**
 * 按运行（run_id）隔离的本机对话记录。
 *
 * 服务端对话保持无状态（/api/chat 不落库），任务页对话历史只存浏览器
 * localStorage：键按 run_id 隔离，跨任务互不可见；是否写入由调用方按
 * 「数据与隐私 → 保存任务历史」开关决定（本模块不做闸门，保持零依赖，
 * 与 last-task-record 一样可直接在 Node 测试里加载）。
 */

const LOG_KEY_PREFIX = "openmathmodel.chatLog.v1.";
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;
/** 单条文本与总条数上限：控制 localStorage 占用；超长回复截断保留开头。 */
const MAX_ENTRY_CHARS = 20_000;
const MAX_ENTRIES = 80;

export interface ConversationLogEntry {
  role: "user" | "assistant";
  text: string;
  /** 系统自动发起的开场分析（没有对应的用户气泡）。 */
  opening?: boolean;
  /** 随消息发送的附件名，恢复时重建气泡下的纸夹徽标。 */
  attachments?: string[];
}

function keyFor(runId: string): string {
  return LOG_KEY_PREFIX + runId;
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
    const entry = item as { role?: unknown; text?: unknown; opening?: unknown; attachments?: unknown };
    if (entry?.role !== "user" && entry?.role !== "assistant") continue;
    if (typeof entry.text !== "string" || !entry.text) continue;
    const attachments = Array.isArray(entry.attachments)
      ? entry.attachments.filter((name): name is string => typeof name === "string")
      : [];
    result.push({
      role: entry.role,
      text: entry.text,
      ...(entry.opening === true ? { opening: true } : {}),
      ...(attachments.length > 0 ? { attachments } : {}),
    });
  }
  return result;
}

export function loadConversationLog(runId: string): ConversationLogEntry[] {
  if (!RUN_ID_PATTERN.test(runId)) return [];
  try {
    return parseConversationLog(localStorage.getItem(keyFor(runId)));
  } catch {
    return [];
  }
}

/** 追加一轮对话（用户消息与回复成对写入；开场分析只有回复一条）。 */
export function appendConversationEntries(runId: string, entries: ConversationLogEntry[]): void {
  if (!RUN_ID_PATTERN.test(runId) || entries.length === 0) return;
  const trimmed = entries.map(entry => (
    entry.text.length > MAX_ENTRY_CHARS ? { ...entry, text: entry.text.slice(0, MAX_ENTRY_CHARS) } : entry
  ));
  const merged = [...loadConversationLog(runId), ...trimmed].slice(-MAX_ENTRIES);
  try {
    localStorage.setItem(keyFor(runId), JSON.stringify({ entries: merged, saved_at: Date.now() }));
  } catch {
    // 存储满或被禁用：本轮不落盘，对话仍在页面内存里继续。
  }
}

/** 删除任务时调用：清掉该运行的本机对话记录。 */
export function clearConversationLog(runId: string): void {
  try {
    localStorage.removeItem(keyFor(runId));
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
