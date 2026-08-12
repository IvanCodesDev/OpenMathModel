/**
 * 设置中心「任务与文件」三个开关的运行时读取器。
 *
 * 开关本体由设置面板渲染和落盘（openmathmodelSettings），这里只负责读。
 * 三项在面板里的初始状态都是开启，因此从未保存过设置时同样视为开启，
 * 只有显式保存过 false 才关闭——与 desktopNotifications 的默认关相反。
 */

const SETTINGS_KEY = "openmathmodelSettings";

function savedSettings(): Record<string, unknown> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** 自动保存任务：编辑内容和对话草稿每 30 秒落盘。 */
export function autoSaveEnabled(): boolean {
  return savedSettings().autoSave !== false;
}

/** 启动时恢复上次任务：重新打开应用时回到最近使用的运行。 */
export function restoreLastTaskEnabled(): boolean {
  return savedSettings().restoreSession !== false;
}

/** 自动解析上传文件：加入附件后立即在浏览器里抽取内容摘要。 */
export function autoParseAttachmentsEnabled(): boolean {
  return savedSettings().autoOpenFiles !== false;
}
