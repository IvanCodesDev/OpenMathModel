/**
 * 设置中心「诊断」的执行层：网络探测、系统信息采集与诊断报告导出。
 *
 * 所有结论都来自真实请求——探测 /api/health、/api/system 与 /api/account/me，
 * 记录延迟与状态；不做任何假装成功的展示。UI 渲染留在设置中心，本模块只产出数据。
 */

export type CheckStatus = "ok" | "warn" | "fail";

export interface DiagnosticCheck {
  name: string;
  status: CheckStatus;
  detail: string;
}

export interface BackendInfo {
  name: string;
  version: string;
  python: string;
  database: string;
  runner_enabled: boolean;
}

const PROBE_TIMEOUT_MS = 5000;

interface ProbeResult {
  ok: boolean;
  status: number;
  latencyMs: number;
  payload: unknown;
  error?: string;
}

async function probe(path: string): Promise<ProbeResult> {
  const started = performance.now();
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    const latencyMs = Math.round(performance.now() - started);
    const payload: unknown = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, latencyMs, payload };
  } catch (error) {
    const latencyMs = Math.round(performance.now() - started);
    const isTimeout = error instanceof DOMException && error.name === "TimeoutError";
    return {
      ok: false,
      status: 0,
      latencyMs,
      payload: null,
      error: isTimeout ? `超过 ${PROBE_TIMEOUT_MS / 1000} 秒无响应` : "无法连接",
    };
  }
}

function parseBackendInfo(payload: unknown): BackendInfo | null {
  const record = payload as Partial<BackendInfo> | null;
  if (!record || typeof record.version !== "string" || typeof record.database !== "string") return null;
  return {
    name: typeof record.name === "string" ? record.name : "OpenMathModel API",
    version: record.version,
    python: typeof record.python === "string" ? record.python : "?",
    database: record.database,
    runner_enabled: record.runner_enabled === true,
  };
}

export interface DiagnosticsOutcome {
  checks: DiagnosticCheck[];
  backend: BackendInfo | null;
  healthLatencyMs: number | null;
}

export async function runNetworkDiagnostics(): Promise<DiagnosticsOutcome> {
  const [health, system, me] = await Promise.all([
    probe("/api/health"),
    probe("/api/system"),
    probe("/api/account/me"),
  ]);

  const checks: DiagnosticCheck[] = [];

  const healthy = health.ok && (health.payload as { status?: string } | null)?.status === "ok";
  checks.push({
    name: "API 服务",
    status: healthy ? "ok" : "fail",
    detail: healthy
      ? `正常 · ${health.latencyMs}ms`
      : health.error ?? `HTTP ${health.status}`,
  });

  const backend = system.ok ? parseBackendInfo(system.payload) : null;
  checks.push({
    name: "后端信息",
    status: backend ? "ok" : system.status === 404 ? "warn" : "fail",
    detail: backend
      ? `${backend.name} v${backend.version} · ${backend.database} · Python ${backend.python}`
      : system.status === 404
        ? "后端版本较旧，未提供 /api/system"
        : system.error ?? `HTTP ${system.status}`,
  });

  checks.push({
    name: "登录状态",
    status: me.status === 200 ? "ok" : me.status === 401 ? "warn" : "fail",
    detail: me.status === 200
      ? "已登录"
      : me.status === 401
        ? "未登录（接口正常）"
        : me.error ?? `HTTP ${me.status}`,
  });

  return { checks, backend, healthLatencyMs: healthy ? health.latencyMs : null };
}

function formatTimestamp(date: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offset = `UTC${sign}${Math.abs(offsetMinutes) / 60}`;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}（${offset}）`;
}

/** 组装诊断报告文本；复制系统信息与导出报告共用同一份事实。 */
export function buildDiagnosticReport(outcome: DiagnosticsOutcome): string {
  const lines: string[] = [];
  lines.push("OpenMathModel 诊断报告");
  lines.push(`生成时间: ${formatTimestamp(new Date())}`);
  lines.push("");
  lines.push("[前端环境]");
  lines.push(`页面地址: ${window.location.href}`);
  lines.push(`浏览器: ${navigator.userAgent}`);
  lines.push(`语言: ${navigator.language}`);
  lines.push(`视口: ${window.innerWidth}×${window.innerHeight} @${window.devicePixelRatio}x`);
  lines.push(`网络状态: ${navigator.onLine ? "在线" : "离线"}`);
  lines.push("");
  lines.push("[后端环境]");
  if (outcome.backend) {
    lines.push(`服务: ${outcome.backend.name} v${outcome.backend.version}`);
    lines.push(`数据库: ${outcome.backend.database}`);
    lines.push(`Python: ${outcome.backend.python}`);
    lines.push(`工作流推进线程: ${outcome.backend.runner_enabled ? "启用" : "关闭"}`);
  } else {
    lines.push("未能获取（见下方诊断结果）");
  }
  if (outcome.healthLatencyMs !== null) {
    lines.push(`API 延迟: ${outcome.healthLatencyMs}ms（/api/health）`);
  }
  lines.push("");
  lines.push("[诊断结果]");
  const marks: Record<CheckStatus, string> = { ok: "通过", warn: "提示", fail: "异常" };
  outcome.checks.forEach(check => {
    lines.push(`${marks[check.status]} · ${check.name}: ${check.detail}`);
  });
  return `${lines.join("\n")}\n`;
}

export function downloadDiagnosticReport(text: string): void {
  const stamp = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  const name = `openmathmodel-diagnostics-${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}${pad(stamp.getDate())}-${pad(stamp.getHours())}${pad(stamp.getMinutes())}.txt`;
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** 返回是否复制成功；失败交给调用方提示用户改用导出。 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
