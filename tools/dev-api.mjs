// 单独起 API（热重载版）：等价于 README「手动双终端启动」里终端 A 的那条命令。
//
// 为什么要包一层：uvicorn --reload 不加 --reload-dir 时监视整个当前目录，而沙盒把
// 实验脚本写在 backend/api/data/workspaces/**/main.py——每次 python_run 都会触发一次
// 重载，把正在执行的阶段打断（页面上表现为思考行「本次调用中断」、阶段反复重试直到
// 失败；2026-09-07 真实事故）。这里把监视范围钉死在源码目录，避免再有人手敲时漏掉。
// 附加参数原样透传给 uvicorn（例如 `npm run dev:api -- --port 8001`）。
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

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

const python = pythonExecutable();
if (!python) {
  console.error("[dev:api] 未找到 Python 虚拟环境。请先按 README 安装根目录 .venv 依赖。");
  process.exit(1);
}

const args = [
  "-m", "uvicorn", "omm_api.asgi:app",
  "--app-dir", "backend/api",
  "--reload",
  // 只监视源码：backend/api/data/（沙盒工作区、产物、目录缓存）绝不能在监视范围内
  "--reload-dir", "backend/api/omm_api",
  "--reload-dir", "agents",
  // 页面的 SSE 长连接永不排空；不设上限时 --reload 的优雅停机会无限等待
  "--timeout-graceful-shutdown", "5",
  "--port", "8000",
  ...process.argv.slice(2),
];

const child = spawn(python, args, { cwd: repositoryRoot, stdio: "inherit", env: process.env });
child.once("exit", (code, signal) => {
  process.exitCode = typeof code === "number" ? code : signal ? 1 : 0;
});
child.once("error", error => {
  console.error(`[dev:api] uvicorn 启动失败：${error.message}`);
  process.exitCode = 1;
});
for (const signal of ["SIGINT", "SIGTERM", "SIGBREAK"]) {
  process.on(signal, () => {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  });
}
