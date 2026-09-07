/**
 * 演示态开关（唯一事实来源）。
 *
 * 页面默认不渲染任何虚构内容：没有真实运行时给空态，由控制器填真实数据。
 * 只有 URL 上显式带 `?demo=1` 时才启用那套样例任务夹具（共享单车调度），
 * 供截图、演示与界面回归对照使用。
 *
 * `modeling-workspace-controller` 依赖同一语义：demo=1 时不解析 run_id，
 * 工作台停留在演示态而不会去拉真实运行。
 */
export function demoMode(): boolean {
  try {
    return new URL(window.location.href).searchParams.get("demo") === "1";
  } catch {
    return false;
  }
}
