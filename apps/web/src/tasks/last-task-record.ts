/**
 * 「最近使用的任务」记录：workspace 每次成功渲染真实运行时写入，
 * 供「启动时恢复上次任务」在下次打开应用时读取。
 *
 * 刻意不依赖其他模块，纯函数可以直接在 Node 测试里加载。
 */

const RECORD_KEY = "openmathmodel.lastTask.v1";
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;
const PROJECT_ID_PATTERN = /^proj_[0-9a-f]{32}$/;

export interface LastTaskRecord {
  run_id: string;
  project_id: string;
}

export function parseLastTaskRecord(raw: string | null): LastTaskRecord | null {
  if (!raw) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  const record = payload as { run_id?: unknown; project_id?: unknown };
  if (typeof record?.run_id !== "string" || !RUN_ID_PATTERN.test(record.run_id)) return null;
  if (typeof record?.project_id !== "string" || !PROJECT_ID_PATTERN.test(record.project_id)) return null;
  return { run_id: record.run_id, project_id: record.project_id };
}

export function savedLastTask(): LastTaskRecord | null {
  try {
    return parseLastTaskRecord(localStorage.getItem(RECORD_KEY));
  } catch {
    return null;
  }
}

export function rememberLastTask(runId: string, projectId: string): void {
  try {
    localStorage.setItem(RECORD_KEY, JSON.stringify({ run_id: runId, project_id: projectId, saved_at: Date.now() }));
  } catch {
    // 存储被禁用时放弃记录；恢复功能退化为不生效，不影响当前会话。
  }
}

/** 传入 runId 时只在记录确实指向该运行时清除，避免误删别的任务的记录。 */
export function forgetLastTask(runId?: string): void {
  try {
    if (runId) {
      const record = parseLastTaskRecord(localStorage.getItem(RECORD_KEY));
      if (record && record.run_id !== runId) return;
    }
    localStorage.removeItem(RECORD_KEY);
  } catch {
    // 同上：没有存储就没有记录可清。
  }
}
