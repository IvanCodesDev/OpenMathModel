/**
 * 「启动时恢复上次任务」的启动引导。
 *
 * “启动”指本标签页会话的第一次加载（应用内导航是整页跳转，每次都会执行
 * main.tsx，靠 sessionStorage 标记区分首次与后续）：首次加载落在首页、开关
 * 开启且存在有效记录时，直接替换跳转到运行工作台；之后用户主动回首页不再拦。
 */

import { buildRunningUrl } from "../integration/task-start-state";
import { restoreLastTaskEnabled } from "../preferences/task-preferences";
import { savedLastTask } from "./last-task-record";

const ATTEMPT_KEY = "openmathmodel.restoreAttempted";

/** 返回 true 表示已发起跳转，调用方应跳过本次渲染。 */
export function restoreLastTaskOnStartup(): boolean {
  if (window.location.pathname.replace(/(.)\/$/, "$1") !== "/") return false;

  try {
    if (sessionStorage.getItem(ATTEMPT_KEY)) return false;
    // 无论本次是否真的跳转，本会话只尝试一次；
    // 中途在设置里打开开关不该让下一次点击首页突然被劫持。
    sessionStorage.setItem(ATTEMPT_KEY, "1");
  } catch {
    // 会话存储不可用时无法区分首次加载，宁可不恢复也不能每次进首页都跳走。
    return false;
  }

  if (!restoreLastTaskEnabled()) return false;
  const record = savedLastTask();
  if (!record) return false;

  // replace 而不是 href：不给历史栈留下首页记录，回退不会陷进重定向循环。
  window.location.replace(buildRunningUrl(record.run_id, record.project_id));
  return true;
}
