/**
 * 论文编辑器的本机草稿：每个项目一份，只存用户亲手编辑过的正文。
 *
 * 编辑器的编辑防抖与手动保存、task-autosave 的定时落盘与恢复、stage-content 的
 * 「用户主权」判断都经这里读写，三处看到的是同一份记录。作用域取 URL 上合法的
 * project_id，没有时落到 demo 档。
 *
 * 刻意不依赖其他模块，可以直接在 Node 测试里加载。
 */

const KEY_PREFIX = "openmathmodel.paperDraft.v1.";
// 早期编辑器把草稿存在这把不分项目的键里：在一个任务里编辑过的论文会出现在每个任务的
// 论文页，论文阶段还没开始的任务也是满满一篇别人的正文。已停用、不再写入；内容只用来
// 认出串台期间被存进别的项目的副本（见 demoteLeakedPaperDraft）。
const LEGACY_SHARED_KEY = "openmathmodelPaperDraft.v1";
const PROJECT_ID_PATTERN = /^proj_[0-9a-f]{32}$/;

export interface UserPaperDraft {
  html: string;
  savedAt: number | null;
}

interface DraftRecord {
  html?: unknown;
  saved_at?: unknown;
  user_edited?: unknown;
}

function draftKey(): string {
  const projectId = new URL(window.location.href).searchParams.get("project_id") ?? "";
  return KEY_PREFIX + (PROJECT_ID_PATTERN.test(projectId) ? projectId : "demo");
}

function readRecord(): DraftRecord | null {
  const raw = localStorage.getItem(draftKey());
  if (!raw) return null;
  const record = JSON.parse(raw) as DraftRecord | null;
  return record && typeof record === "object" ? record : null;
}

/**
 * 本项目里用户亲手编辑过的正文；没有记录、记录损坏都返回 null。
 * 旧版本落盘的记录没有 user_edited 标记（可能只是模板快照）：不恢复也不删除，
 * 让页面保持模板或真实阶段产出。
 */
export function readUserPaperDraft(): UserPaperDraft | null {
  try {
    const record = readRecord();
    if (!record || typeof record.html !== "string" || !record.html.trim()) return null;
    if (record.user_edited !== true) return null;
    const savedAt = typeof record.saved_at === "number" && Number.isFinite(record.saved_at) ? record.saved_at : null;
    return { html: record.html, savedAt };
  } catch {
    return null;
  }
}

/** 把编辑器正文记为本项目的用户草稿；存储满或被禁用时返回 false，正文只活在页面里。 */
export function writeUserPaperDraft(html: string): boolean {
  try {
    localStorage.setItem(draftKey(), JSON.stringify({ html, saved_at: Date.now(), user_edited: true }));
    return true;
  } catch {
    return false;
  }
}

/** 「恢复初始正文」：丢弃本项目的草稿，停用的共享键一并清掉。 */
export function clearPaperDraft(): void {
  try {
    localStorage.removeItem(draftKey());
    localStorage.removeItem(LEGACY_SHARED_KEY);
  } catch {
    // 没有存储就没有草稿可清。
  }
}

/**
 * 本项目的用户草稿与停用共享键的内容一字不差时，把它降为非用户草稿：之后不再恢复、
 * 也不再挡 Agent 正文，正文本身留在记录里不删。返回是否降级了。
 *
 * 只在确认本运行还没有论文产出时调用：串台副本正是这样存进来的（打开别的任务的
 * 论文页、在灌进来的旧稿上点了一下）。有论文产出的项目里，一字不差的那份多半是用户
 * 在自己的论文上改出来的——共享键记下的也正是它。
 */
export function demoteLeakedPaperDraft(): boolean {
  try {
    const record = readRecord();
    if (!record || record.user_edited !== true || typeof record.html !== "string") return false;
    if (record.html !== localStorage.getItem(LEGACY_SHARED_KEY)) return false;
    localStorage.setItem(draftKey(), JSON.stringify({ ...record, user_edited: false }));
    return true;
  } catch {
    return false;
  }
}
