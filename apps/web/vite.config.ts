import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const workspaceRoot = fileURLToPath(new URL("../..", import.meta.url));
const apiTarget = process.env.OMM_API_PROXY_TARGET ?? "http://127.0.0.1:8000";
const paperCacheRoot = resolve(
  workspaceRoot,
  "datasets/raw/sources/github/zhanwen-MathModel/papers",
);
const paperRoutePrefix = "/paper-files/";
const paperSourceRevision = "cd5be91735ebf11d5ee52eb170e86a6d07131977";

function sendGuestAccount(res: ServerResponse): void {
  const body = JSON.stringify({ code: "UNAUTHENTICATED", message: "请先登录" });
  res.writeHead(401, {
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(body);
}

async function serveAccountMe(req: IncomingMessage, res: ServerResponse): Promise<void> {
  if (!/(?:^|;\s*)omm_session=/.test(req.headers.cookie ?? "")) {
    sendGuestAccount(res);
    return;
  }

  try {
    const upstream = await fetch(new URL("/api/account/me", apiTarget), {
      headers: {
        accept: req.headers.accept ?? "application/json",
        cookie: req.headers.cookie ?? "",
        "user-agent": req.headers["user-agent"] ?? "OpenMathModel-Vite",
      },
      signal: AbortSignal.timeout(1200),
    });
    const body = Buffer.from(await upstream.arrayBuffer());
    res.writeHead(upstream.status, {
      "Cache-Control": upstream.headers.get("cache-control") ?? "no-store",
      "Content-Length": body.byteLength,
      "Content-Type": upstream.headers.get("content-type") ?? "application/json; charset=utf-8",
    });
    res.end(body);
  } catch {
    // 单独启动 Web 或浏览器保留了过期 Cookie 时，降级为访客态，不把连接失败刷进 Vite 错误日志。
    sendGuestAccount(res);
  }
}

function parseRange(rangeHeader: string | undefined, size: number): { start: number; end: number } | null {
  if (!rangeHeader) return { start: 0, end: size - 1 };
  const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
  if (!match || (!match[1] && !match[2])) return null;

  let start: number;
  let end: number;
  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) return null;
    start = Math.max(0, size - suffixLength);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
  }
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || start >= size) {
    return null;
  }
  return { start, end: Math.min(end, size - 1) };
}

async function relayRemotePaper(
  req: IncomingMessage,
  res: ServerResponse,
  year: string,
  problemGroup: string,
  filename: string,
): Promise<void> {
  const sourceDirectories = [problemGroup];
  if (year === "2021") sourceDirectories.push("获数模之星提名奖（12篇）");

  for (const sourceDirectory of sourceDirectories) {
    const sourcePath = [
      "国赛论文",
      `${year}年优秀论文`,
      sourceDirectory,
      filename,
    ].map(segment => encodeURIComponent(segment)).join("/");
    const sourceUrl = `https://raw.githubusercontent.com/zhanwen/MathModel/${paperSourceRevision}/${sourcePath}`;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const upstream = await fetch(sourceUrl, {
          headers: req.headers.range ? { Range: req.headers.range } : undefined,
          method: req.method,
          redirect: "follow",
          signal: AbortSignal.timeout(30000),
        });
        if (upstream.status === 404) break;
        if (!upstream.ok || (req.method !== "HEAD" && !upstream.body)) {
          if (attempt < 2 && (upstream.status === 429 || upstream.status >= 500)) {
            await new Promise(resolveDelay => setTimeout(resolveDelay, 350 * (attempt + 1)));
            continue;
          }
          break;
        }

        const headers: Record<string, string> = {
          "Accept-Ranges": upstream.headers.get("accept-ranges") ?? "bytes",
          "Cache-Control": "public, max-age=3600",
          "Content-Type": "application/pdf",
        };
        for (const name of ["content-length", "content-range"] as const) {
          const value = upstream.headers.get(name);
          if (value) headers[name] = value;
        }
        res.writeHead(upstream.status, headers);
        if (req.method === "HEAD" || !upstream.body) {
          res.end();
          return;
        }

        const reader = upstream.body.getReader();
        res.on("close", () => void reader.cancel());
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!res.write(value)) {
            await new Promise<void>(resolveDrain => res.once("drain", resolveDrain));
          }
        }
        res.end();
        return;
      } catch {
        if (attempt < 2) {
          await new Promise(resolveDelay => setTimeout(resolveDelay, 350 * (attempt + 1)));
        }
      }
    }
  }
  res.writeHead(502).end();
}

async function serveCachedPaper(
  req: IncomingMessage,
  res: ServerResponse,
  pathname: string,
  next: () => void,
): Promise<void> {
  if (!pathname.startsWith(paperRoutePrefix)) {
    next();
    return;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end();
    return;
  }

  let segments: string[];
  try {
    segments = pathname.slice(paperRoutePrefix.length).split("/").map(decodeURIComponent);
  } catch {
    res.writeHead(400).end();
    return;
  }
  const [year, problemGroup, filename] = segments;
  if (
    segments.length !== 3
    || !/^\d{4}$/.test(year ?? "")
    || !/^[A-F]$/i.test(problemGroup ?? "")
    || !filename
    || !/\.pdf$/i.test(filename)
  ) {
    res.writeHead(404).end();
    return;
  }

  const filePath = resolve(paperCacheRoot, year, problemGroup.toUpperCase(), filename);
  if (!filePath.startsWith(`${paperCacheRoot}${sep}`)) {
    res.writeHead(404).end();
    return;
  }

  try {
    const info = await stat(filePath);
    if (!info.isFile()) throw new Error("Not a file");
    const range = parseRange(req.headers.range, info.size);
    if (!range) {
      res.writeHead(416, { "Content-Range": `bytes */${info.size}` }).end();
      return;
    }
    const isPartial = Boolean(req.headers.range);
    const contentLength = range.end - range.start + 1;
    res.writeHead(isPartial ? 206 : 200, {
      "Accept-Ranges": "bytes",
      "Cache-Control": "public, max-age=3600",
      "Content-Length": contentLength,
      "Content-Type": "application/pdf",
      ...(isPartial ? { "Content-Range": `bytes ${range.start}-${range.end}/${info.size}` } : {}),
    });
    if (req.method === "HEAD") {
      res.end();
      return;
    }
    const stream = createReadStream(filePath, range);
    stream.on("error", error => res.destroy(error));
    stream.pipe(res);
    return;
  } catch {
    await relayRemotePaper(req, res, year, problemGroup.toUpperCase(), filename);
  }
}

function localDevelopmentResources(): Plugin {
  const middleware = (
    req: IncomingMessage,
    res: ServerResponse,
    next: () => void,
  ): void => {
    const pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    // /me 在开发服务器内完成可用性降级；其余 API 继续交给标准代理。
    if (req.method === "GET" && pathname === "/api/account/me") {
      void serveAccountMe(req, res);
      return;
    }
    void serveCachedPaper(req, res, pathname, next);
  };
  return {
    name: "openmathmodel-local-development-resources",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

const apiProxy = {
  "/api": {
    target: apiTarget,
    changeOrigin: false,
  },
};

export default defineConfig({
  plugins: [localDevelopmentResources(), react()],
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
  build: {
    sourcemap: true,
    rollupOptions: {
      output: {
        // 数据与第三方库分别独立成 chunk：改代码时用户不必重新下载 2MB 赛题库，
        // KaTeX 只在打开方法库公式时才被请求。
        manualChunks(id) {
          if (id.includes("node_modules/katex")) return "katex";
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          return undefined;
        },
      },
    },
  },
});
