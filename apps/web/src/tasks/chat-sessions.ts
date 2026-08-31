/**
 * 首页普通对话的本机会话目录。
 *
 * 接待判定把闲聊 / 缺题面的输入分流到首页对话，这条链刻意不建项目、不起
 * 运行（避免垃圾项目），因此服务端没有任何可列的记录。本模块补上缺的那一
 * 环：给每段对话发一个 `chat_<32 hex>` 身份并登记标题与时间，正文仍按既有
 * 语义写进 tasks/conversation-log（同一把 localStorage 钥匙，同受「数据与
 * 隐私 → 保存任务历史」管辖），侧栏「最近任务」据此与服务端任务合并展示。
 *
 * 条目带 owner：同一浏览器换账号登录后只列自己的对话，不串上一位用户的。
 * 与 last-task-record 一样刻意不依赖其他模块（只用同目录的对话记录），
 * 纯函数可以直接在 Node 测试里加载。
 */

import { clearConversationLog } from "./conversation-log";

const REGISTRY_KEY = "openmathmodel.chatSessions.v1";

export const CHAT_ID_PATTERN = /^chat_[0-9a-f]{32}$/;

/** 目录容量：超出后淘汰最久未更新的一段，并连带清掉它的正文记录。 */
const MAX_SESSIONS = 40;
const MAX_TITLE_CHARS = 120;

export interface ChatSession {
  id: string;
  /** 归属用户 id；未登录时对话根本发不出去，故恒为真实 id。 */
  owner: string;
  title: string;
  created_at: number;
  updated_at: number;
  /** 归档时间戳；未归档为 0。 */
  archived_at: number;
}

/** 会话标题：取首条用户消息的第一个短句，与任务名同一套收敛口径。 */
export function deriveChatTitle(text: string): string {
  const compact = String(text ?? "").replace(/\s+/g, " ").trim();
  const firstClause = compact.split(/[。！？!?；;]/, 1)[0]?.trim() || compact;
  const characters = Array.from(firstClause || "新对话");
  return characters.length > 24 ? `${characters.slice(0, 24).join("")}…` : characters.join("");
}

function timestamp(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

export function parseChatSessions(raw: string | null): ChatSession[] {
  if (!raw) return [];
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return [];
  }
  const items = (payload as { sessions?: unknown })?.sessions;
  if (!Array.isArray(items)) return [];
  const sessions: ChatSession[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    const entry = item as Partial<Record<keyof ChatSession, unknown>>;
    const id = typeof entry?.id === "string" ? entry.id : "";
    if (!CHAT_ID_PATTERN.test(id) || seen.has(id)) continue;
    if (typeof entry.owner !== "string" || !entry.owner) continue;
    seen.add(id);
    const created = timestamp(entry.created_at);
    sessions.push({
      id,
      owner: entry.owner,
      title: (typeof entry.title === "string" ? entry.title : "").slice(0, MAX_TITLE_CHARS) || "新对话",
      created_at: created,
      updated_at: timestamp(entry.updated_at) || created,
      archived_at: timestamp(entry.archived_at),
    });
  }
  return sessions;
}

function loadAll(): ChatSession[] {
  try {
    return parseChatSessions(localStorage.getItem(REGISTRY_KEY));
  } catch {
    return [];
  }
}

function saveAll(sessions: ChatSession[]): void {
  // 超出容量先淘汰最久未更新的，正文记录一并回收，避免孤儿键长期占位。
  const ordered = [...sessions].sort((a, b) => b.updated_at - a.updated_at);
  for (const dropped of ordered.slice(MAX_SESSIONS)) clearConversationLog(dropped.id);
  try {
    localStorage.setItem(
      REGISTRY_KEY,
      JSON.stringify({ sessions: ordered.slice(0, MAX_SESSIONS), saved_at: Date.now() }),
    );
  } catch {
    // 存储满或被禁用：本段对话仍在页面内存里继续，只是不进目录。
  }
}

/** 按更新时间倒序列出某用户的对话；默认只看未归档。 */
export function listChatSessions(owner: string, options: { archived?: boolean } = {}): ChatSession[] {
  if (!owner) return [];
  const wantArchived = options.archived === true;
  return loadAll()
    .filter(session => session.owner === owner && (session.archived_at > 0) === wantArchived)
    .sort((a, b) => b.updated_at - a.updated_at);
}

export function findChatSession(id: string): ChatSession | null {
  return loadAll().find(session => session.id === id) ?? null;
}

export function newChatSessionId(): string {
  return `chat_${crypto.randomUUID().replaceAll("-", "")}`;
}

/** 登记一段新对话；id 由调用方持有，正文按同 id 写进 conversation-log。 */
export function createChatSession(id: string, owner: string, title: string): ChatSession | null {
  if (!CHAT_ID_PATTERN.test(id) || !owner) return null;
  const now = Date.now();
  const session: ChatSession = {
    id,
    owner,
    title: deriveChatTitle(title),
    created_at: now,
    updated_at: now,
    archived_at: 0,
  };
  saveAll([session, ...loadAll().filter(item => item.id !== id)]);
  return session;
}

function patchSession(id: string, patch: (session: ChatSession) => ChatSession): void {
  const sessions = loadAll();
  const index = sessions.findIndex(session => session.id === id);
  if (index < 0) return;
  sessions[index] = patch(sessions[index]);
  saveAll(sessions);
}

/** 每轮对话结束后调用：更新时间戳，让它回到列表最前。 */
export function touchChatSession(id: string): void {
  patchSession(id, session => ({ ...session, updated_at: Date.now() }));
}

export function renameChatSession(id: string, title: string): void {
  const next = title.trim().slice(0, MAX_TITLE_CHARS);
  if (!next) return;
  patchSession(id, session => ({ ...session, title: next }));
}

export function setChatSessionArchived(id: string, archived: boolean): void {
  patchSession(id, session => ({ ...session, archived_at: archived ? Date.now() : 0 }));
}

/** 删除一段对话：目录条目与正文记录一起清，删了就找不回来。 */
export function deleteChatSession(id: string): void {
  clearConversationLog(id);
  saveAll(loadAll().filter(session => session.id !== id));
}

/** 关闭「保存任务历史」时调用：清空本机全部对话目录与其正文记录。 */
export function clearAllChatSessions(): void {
  for (const session of loadAll()) clearConversationLog(session.id);
  try {
    localStorage.removeItem(REGISTRY_KEY);
  } catch {
    // 没有存储就没有目录可清。
  }
}
