import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const apiOrigin = process.env.OMM_API_PROXY_TARGET ?? "http://127.0.0.1:8000";
let proxyTarget = apiOrigin;
let apiUrl;
let healthUrl;
const children = new Set();
let stopping = false;
let stopPromise;
let requestedExitCode = 0;
let resolveLifecycle;
const lifecycle = new Promise(resolveDone => {
  resolveLifecycle = resolveDone;
});

function pythonExecutable() {
  const candidates = process.platform === "win32"
    ? [
        join(repositoryRoot, ".venv", "Scripts", "python.exe"),
        join(repositoryRoot, "backend", "api", ".venv", "Scripts", "python.exe"),
      ]
    : [
        join(repositoryRoot, ".venv", "bin", "python"),
        join(repositoryRoot, "backend", "api", ".venv", "bin", "python"),
      ];
  return candidates.find(existsSync);
}

function localApiBinding() {
  if (apiUrl.protocol !== "http:" || apiUrl.username || apiUrl.password) return null;
  if (apiUrl.pathname !== "/" || apiUrl.search || apiUrl.hash) return null;
  const hostname = apiUrl.hostname.replace(/^\[|\]$/g, "");
  if (!["127.0.0.1", "localhost", "::1"].includes(hostname)) return null;
  const port = Number(apiUrl.port || "80");
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) return null;
  return { host: hostname === "localhost" ? "127.0.0.1" : hostname, port };
}

async function apiHealthy() {
  try {
    const response = await fetch(healthUrl, { signal: AbortSignal.timeout(900) });
    if (!response.ok) return false;
    const payload = await response.json();
    return payload?.status === "ok";
  } catch {
    return false;
  }
}

function childExitCode(code, signal) {
  if (typeof code === "number") return code;
  return signal ? 1 : 0;
}

function start(command, args, options = {}, policy = {}) {
  const child = spawn(command, args, {
    cwd: repositoryRoot,
    detached: process.platform !== "win32",
    env: process.env,
    stdio: "inherit",
    ...options,
  });
  children.add(child);
  child.once("error", error => {
    children.delete(child);
    if (stopping) return;
    console.error(`\n[dev] ${command} 启动失败：${error.message}`);
    void stop(1);
  });
  child.once("exit", (code, signal) => {
    children.delete(child);
    if (stopping) return;
    const rawExitCode = childExitCode(code, signal);
    const exitCode = rawExitCode === 0 && policy.zeroExitIsError ? 1 : rawExitCode;
    if (exitCode !== 0) {
      console.error(`\n[dev] ${command} 异常退出（code=${code ?? "null"}, signal=${signal ?? "none"}）`);
    }
    void stop(exitCode);
  });
  return child;
}

async function waitForApi(apiChild) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (await apiHealthy()) return;
    if (apiChild.exitCode !== null || apiChild.signalCode !== null) {
      throw new Error(`API 进程提前退出（code=${apiChild.exitCode ?? "null"}）`);
    }
    await new Promise(resolveDelay => setTimeout(resolveDelay, 250));
  }
  throw new Error(`API 健康检查超时：${healthUrl}`);
}

function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise(resolveDone => {
    const timer = setTimeout(() => {
      child.removeListener("exit", onExit);
      resolveDone(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      resolveDone(true);
    };
    child.once("exit", onExit);
  });
}

function processGroupAlive(pid) {
  try {
    process.kill(-pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitForGroupExit(pid, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processGroupAlive(pid)) return true;
    await new Promise(resolveDelay => setTimeout(resolveDelay, 50));
  }
  return !processGroupAlive(pid);
}

async function runTaskkill(pid) {
  return new Promise(resolveDone => {
    let settled = false;
    const finish = code => {
      if (settled) return;
      settled = true;
      resolveDone(code);
    };
    try {
      const killer = spawn("taskkill", ["/pid", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      killer.once("exit", code => finish(code ?? 1));
      killer.once("error", () => finish(1));
    } catch {
      finish(1);
    }
  });
}

async function terminate(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    const taskkillCode = await runTaskkill(child.pid);
    if (taskkillCode !== 0 && child.exitCode === null) {
      try { child.kill("SIGTERM"); } catch { /* 后续存活检查统一处理 */ }
    }
    if (!(await waitForExit(child, 2_000)) && child.exitCode === null) {
      try { child.kill("SIGKILL"); } catch { /* 后续存活检查统一处理 */ }
    }
    await waitForExit(child, 1_000);
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      try { child.kill("SIGTERM"); } catch { /* 后续存活检查统一处理 */ }
    }
    await waitForExit(child, 2_000);
    if (processGroupAlive(child.pid)) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {
        try { child.kill("SIGKILL"); } catch { /* 后续存活检查统一处理 */ }
      }
      await Promise.all([waitForExit(child, 1_000), waitForGroupExit(child.pid, 1_000)]);
    }
  }
  const stillAlive = process.platform === "win32"
    ? child.exitCode === null && child.signalCode === null
    : processGroupAlive(child.pid);
  if (stillAlive) {
    requestedExitCode = Math.max(requestedExitCode, 1);
    console.error(`[dev] 未确认子进程已停止：PID ${child.pid}`);
  }
}

function stop(exitCode = 0) {
  requestedExitCode = Math.max(requestedExitCode, exitCode);
  if (stopPromise) return stopPromise;
  stopping = true;
  stopPromise = (async () => {
    await Promise.all([...children].map(terminate));
    resolveLifecycle(requestedExitCode);
    return requestedExitCode;
  })();
  return stopPromise;
}

process.once("SIGINT", () => void stop(0));
process.once("SIGTERM", () => void stop(0));
if (process.platform === "win32") process.once("SIGBREAK", () => void stop(0));

async function main() {
  try {
    apiUrl = new URL(apiOrigin);
  } catch {
    throw new Error("OMM_API_PROXY_TARGET 必须是完整的 HTTP(S) URL。");
  }
  if (!["http:", "https:"].includes(apiUrl.protocol)) {
    throw new Error("OMM_API_PROXY_TARGET 仅支持 HTTP(S) URL。");
  }
  if (apiUrl.username || apiUrl.password) {
    throw new Error("OMM_API_PROXY_TARGET 不应包含用户名或密码。");
  }
  if (apiUrl.pathname !== "/" || apiUrl.search || apiUrl.hash) {
    throw new Error("OMM_API_PROXY_TARGET 必须是无路径、查询参数和片段的 API Origin。");
  }
  healthUrl = new URL("/api/health", apiUrl);

  if (await apiHealthy()) {
    console.log(`[dev] 复用已运行的 API：${apiOrigin}`);
  } else {
    const binding = localApiBinding();
    if (!binding) {
      throw new Error(`API 代理目标未通过健康检查，且不是可自动启动的本地 HTTP 地址：${apiOrigin}`);
    }
    if (apiUrl.hostname === "localhost") {
      apiUrl.hostname = "127.0.0.1";
      proxyTarget = apiUrl.origin;
      healthUrl = new URL("/api/health", apiUrl);
      if (await apiHealthy()) {
        console.log(`[dev] 复用已运行的 API：${proxyTarget}`);
      }
    }
    if (!(await apiHealthy())) {
      const python = pythonExecutable();
      if (!python) {
        throw new Error("未找到 Python 虚拟环境。请先按 README 安装根目录 .venv 依赖。");
      }
      console.log(`[dev] 启动 API：${proxyTarget}`);
      const apiChild = start(
        python,
        [
          "-m",
          "uvicorn",
          "omm_api.asgi:app",
          "--app-dir",
          "backend/api",
          "--host",
          binding.host,
          "--port",
          String(binding.port),
        ],
        {},
        { zeroExitIsError: true },
      );
      await waitForApi(apiChild);
      console.log(`[dev] API 健康检查通过：${healthUrl}`);
    }
  }

  const npmExecPath = process.env.npm_execpath;
  if (!npmExecPath || !existsSync(npmExecPath)) {
    throw new Error("缺少 npm_execpath；请通过 `npm run dev` 启动完整开发环境。");
  }
  console.log("[dev] 启动 Web；按 Ctrl+C 同时停止本次启动的服务。");
  const npmArgs = [
    "run",
    "dev",
    "--workspace",
    "@openmathmodel/web",
    "--",
    ...process.argv.slice(2),
  ];
  start(process.execPath, [npmExecPath, ...npmArgs], {
    env: { ...process.env, OMM_API_PROXY_TARGET: proxyTarget },
    windowsHide: true,
  });

  return lifecycle;
}

try {
  process.exitCode = await main();
} catch (error) {
  console.error(`[dev] ${error instanceof Error ? error.message : String(error)}`);
  await stop(1);
  process.exitCode = await lifecycle;
}
