import { createReadStream } from "node:fs";
import { mkdir, readdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import type { IncomingMessage, ServerResponse } from "node:http";
import { dirname, resolve, sep } from "node:path";
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
const mcmPaperCacheRoot = resolve(
  workspaceRoot,
  "datasets/raw/sources/github/Jackksonns-MCM-ICM-Outstanding-Papers/papers",
);
const mcmPaperSourceRevision = "d29267cb9e993419749e6111981b30a44183fdf8";
// 第二批赛事（国赛、华数杯、亚太赛、MathorCup、泰迪杯）的论文由采集脚本落到原始层：
// 主办方只发布整包压缩文件，没有单篇公网地址，因此这里只从磁盘读，不做按需回源。
const archivePaperRoot = resolve(workspaceRoot, "datasets/raw/sources/paper-archives");
const archivePaperRoutePrefix = "archive";
// 各镜像域名的 DNS 解析在部分开发网络下会间歇失效，官方备用域一并列入候选。
const jsDelivrHosts = ["cdn.jsdelivr.net", "gcore.jsdelivr.net", "testingcf.jsdelivr.net"];
// LaTeX 排版的中文论文用 Fandol 等 CID 字体，字形要靠预定义 CMap 才能取到；缺了这批
// .bcmap，pdf.js 会在 translateFont 阶段失败，整页中文渲染成空白（拉丁字母照常显示）。
// 未内嵌的 Times/Helvetica 则需要 standard_fonts。两份资源都随 pdfjs-dist 发布，
// 这里在开发期直接从 node_modules 提供，构建时原样拷进产物。
const pdfjsAssetRoutePrefix = "/pdfjs/";
const pdfjsAssetDirectories = { cmaps: "cmaps", "standard_fonts": "standard_fonts" } as const;
const pdfjsPackageRoot = dirname(
  createRequire(import.meta.url).resolve("pdfjs-dist/package.json"),
);

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

// raw.githubusercontent.com 在部分代理/运营商网络下会超时或被劫持，浏览器直连拿到的
// 残缺字节流会让 pdf.js 把论文渲染成乱码页。因此中转优先走 jsDelivr 的同提交字节镜像，
// 下载成功即写入本地缓存，此后该论文的所有请求（含 Range 分段）都由磁盘原样提供。
function mirrorUrls(repository: string, revision: string, sourcePath: string): string[] {
  return [
    ...jsDelivrHosts.map(host => `https://${host}/gh/${repository}@${revision}/${sourcePath}`),
    `https://raw.githubusercontent.com/${repository}/${revision}/${sourcePath}`,
  ];
}

function paperUpstreamUrls(year: string, problemGroup: string, filename: string): string[] {
  const sourceDirectories = [problemGroup];
  if (year === "2021") sourceDirectories.push("获数模之星提名奖（12篇）");
  return sourceDirectories.flatMap(sourceDirectory => {
    const sourcePath = [
      "国赛论文",
      `${year}年优秀论文`,
      sourceDirectory,
      filename,
    ].map(segment => encodeURIComponent(segment)).join("/");
    return mirrorUrls("zhanwen/MathModel", paperSourceRevision, sourcePath);
  });
}

// 美赛论文（Jackksonns 快照）：2016 年起按题组分目录，2013–2015 年直接挂在年份下
// （路由里用 X 占位题组）。
function mcmPaperUpstreamUrls(year: string, problemGroup: string, filename: string): string[] {
  const segments = problemGroup === "X" ? [year, filename] : [year, problemGroup, filename];
  const sourcePath = segments.map(segment => encodeURIComponent(segment)).join("/");
  return mirrorUrls("Jackksonns/MCM-ICM-Outstanding-Papers", mcmPaperSourceRevision, sourcePath);
}

async function fetchPaperBytes(sourceUrl: string): Promise<Buffer | null> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const upstream = await fetch(sourceUrl, {
        redirect: "follow",
        signal: AbortSignal.timeout(60000),
      });
      if (upstream.status === 404) return null;
      if (!upstream.ok) {
        if (upstream.status !== 429 && upstream.status < 500) return null;
        await new Promise(resolveDelay => setTimeout(resolveDelay, 350 * (attempt + 1)));
        continue;
      }
      const body = Buffer.from(await upstream.arrayBuffer());
      // 上游被劫持时常见返回 HTML 报错页；魔数不符就丢弃，坏字节绝不落缓存。
      if (body.subarray(0, 5).toString("latin1") !== "%PDF-") return null;
      return body;
    } catch {
      await new Promise(resolveDelay => setTimeout(resolveDelay, 350 * (attempt + 1)));
    }
  }
  return null;
}

const paperDownloads = new Map<string, Promise<void>>();

function downloadPaperToCache(sourceUrls: string[], filePath: string): Promise<void> {
  const inFlight = paperDownloads.get(filePath);
  if (inFlight) return inFlight;
  const task = (async () => {
    for (const sourceUrl of sourceUrls) {
      const body = await fetchPaperBytes(sourceUrl);
      if (!body) continue;
      await mkdir(dirname(filePath), { recursive: true });
      const temporaryPath = `${filePath}.download-${process.pid}-${Date.now()}`;
      await writeFile(temporaryPath, body);
      try {
        await rename(temporaryPath, filePath);
      } catch (error) {
        // Windows 上并发落盘可能在 rename 处冲突；已有完整文件时以先写入者为准。
        await unlink(temporaryPath).catch(() => undefined);
        const existing = await stat(filePath).catch(() => null);
        if (!existing?.isFile()) throw error;
      }
      return;
    }
    throw new Error(`论文上游镜像均不可用：${filePath}`);
  })().finally(() => paperDownloads.delete(filePath));
  paperDownloads.set(filePath, task);
  return task;
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
  const isArchive = segments[0] === archivePaperRoutePrefix;
  const isMcm = segments[0] === "mcm";
  const competition = isArchive ? segments[1] : "";
  const [year, problemGroup, filename] = isArchive
    ? segments.slice(2)
    : isMcm ? segments.slice(1) : segments;
  const expectedSegments = isArchive ? 5 : isMcm ? 4 : 3;
  if (
    segments.length !== expectedSegments
    || (isArchive && !/^[a-z][a-z-]{1,20}$/.test(competition))
    || !/^\d{4}$/.test(year ?? "")
    || !(isMcm || isArchive ? /^[A-FTX]$/i : /^[A-F]$/i).test(problemGroup ?? "")
    || !filename
    || !/^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$/i.test(filename)
  ) {
    res.writeHead(404).end();
    return;
  }

  const cacheRoot = isArchive
    ? resolve(archivePaperRoot, competition)
    : isMcm ? mcmPaperCacheRoot : paperCacheRoot;
  // 采集脚本对无题组目录的 2013–2015 年美赛论文使用 "_" 作为本地缓存目录。
  const cacheGroup = isMcm && problemGroup.toUpperCase() === "X" ? "_" : problemGroup.toUpperCase();
  const filePath = resolve(cacheRoot, year, cacheGroup, filename);
  if (!filePath.startsWith(`${cacheRoot}${sep}`) || (isArchive && !filePath.startsWith(`${archivePaperRoot}${sep}`))) {
    res.writeHead(404).end();
    return;
  }

  try {
    let info = await stat(filePath).catch(() => null);
    if (!info?.isFile() && isArchive) {
      // 这批论文没有可回源的单篇地址，缺文件说明采集脚本还没跑过。
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" })
        .end(`论文尚未落到本地原始层：请先运行 datasets/recipes 中对应的采集脚本（${competition}）`);
      return;
    }
    if (!info?.isFile()) {
      const upstreamUrls = isMcm
        ? mcmPaperUpstreamUrls(year, problemGroup.toUpperCase(), filename)
        : paperUpstreamUrls(year, problemGroup.toUpperCase(), filename);
      await downloadPaperToCache(upstreamUrls, filePath);
      info = await stat(filePath);
    }
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
  } catch {
    res.writeHead(502).end();
  }
}

async function servePdfjsAsset(res: ServerResponse, pathname: string): Promise<void> {
  const [directory, filename] = pathname.slice(pdfjsAssetRoutePrefix.length).split("/");
  // 名字不合法就直接 404：让它落到 SPA 兜底会把 index.html 当字体喂给 pdf.js。
  if (!(directory in pdfjsAssetDirectories) || !filename || !/^[\w.-]+$/.test(filename)) {
    res.writeHead(404).end();
    return;
  }
  const filePath = resolve(pdfjsPackageRoot, directory, filename);
  try {
    const body = await readFile(filePath);
    res.writeHead(200, {
      "Cache-Control": "public, max-age=86400",
      "Content-Length": body.byteLength,
      "Content-Type": filename.endsWith(".bcmap") ? "application/octet-stream" : "font/otf",
    });
    res.end(body);
  } catch {
    res.writeHead(404).end();
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
    if (pathname.startsWith(pdfjsAssetRoutePrefix)) {
      void servePdfjsAsset(res, pathname);
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
    // 构建产物必须自带同一批 CMap 与标准字体，否则线上打开中文论文又会白页。
    async generateBundle() {
      for (const directory of Object.values(pdfjsAssetDirectories)) {
        const sourceDirectory = resolve(pdfjsPackageRoot, directory);
        for (const filename of await readdir(sourceDirectory)) {
          this.emitFile({
            type: "asset",
            fileName: `pdfjs/${directory}/${filename}`,
            source: await readFile(resolve(sourceDirectory, filename)),
          });
        }
      }
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
    // 固定独立端口，避免与本机其他 Vite 项目在默认 5173 上相互挤占。
    port: 5183,
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
