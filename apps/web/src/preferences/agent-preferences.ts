/**
 * 设置中心「高级」三个 Agent 行为开关的运行时读取器。
 *
 * 开关本体由设置面板渲染和落盘（openmathmodelSettings），这里只负责读。
 * 「自动重试失败请求」在面板里的初始状态是开启，因此从未保存过设置时同样
 * 视为开启，只有显式保存过 false 才关闭；其余两项的初始状态都是关闭，
 * 只有显式保存过 true 才开启——与 desktopNotifications 的默认关一致。
 */

const SETTINGS_KEY = "openmathmodelSettings";

function savedSettings(): Record<string, unknown> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** 自动重试失败请求：网络错误与 429 限流按退避重试，最多 3 次。 */
export function autoRetryEnabled(): boolean {
  return savedSettings().retryRequest !== false;
}

/** 外部操作前请求确认：随任务创建参数下发，外部副作用操作先暂停等确认。 */
export function confirmExternalEnabled(): boolean {
  return savedSettings().confirmExternal === true;
}

/** 开发者模式：控制台输出每个请求的诊断行，错误文案附带请求 ID。 */
export function developerModeEnabled(): boolean {
  return savedSettings().developerMode === true;
}
