/**
 * 论文编辑器「导出」菜单的编排：抓图 → 读成统一文档模型 → 按格式生成 → 直接下载。
 *
 * Word / LaTeX / HTML 全在浏览器里生成；PDF 交给服务端无头浏览器按 A4 打印（ADR-0025），
 * 前端拿到产物直接下载，不弹打印框。服务端不可用（没装 Edge / Chrome、未关联项目、请求失败）
 * 时才退回浏览器打印，并说明原因。
 */

import { strToU8, zipSync } from "fflate";
import { t } from "../i18n/locale";
import { DOCX_MEDIA_TYPE, buildDocx } from "./paper-docx";
import { type PaperDocument, readPaperDocument, stripControlChars } from "./paper-document";
import { type EmbeddedImage, bytesToBase64, exportFileName } from "./paper-export-images";
import { paperDocumentToHtml } from "./paper-html";
import { latexImageName, paperDocumentToLatex } from "./paper-latex";

export const PAPER_EXPORT_CHOICES = ["导出 Word (.docx)", "导出 PDF", "导出 LaTeX (.zip)", "导出 HTML"] as const;
export type PaperExportChoice = (typeof PAPER_EXPORT_CHOICES)[number];

export type Notify = (message: string, durationMs?: number) => void;

/** 带数字 / 原因的提示：界面翻译只认整句，所以先按模板查词典，再填占位符。 */
function fill(template: string, values: Record<string, string | number>): string {
  return t(template).replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? ""));
}

type Katex = typeof import("katex").default;

let katexModule: Katex | null = null;
async function loadKatex(): Promise<Katex> {
  katexModule ??= (await import("katex")).default;
  return katexModule;
}

/** 文件名：去掉 Windows 不允许的字符，过长截断。 */
export function exportBaseName(title: string): string {
  const clean = stripControlChars(title).replace(/[\\/:*?"<>|]+/g, "_").replace(/\s+/g, " ").trim().slice(0, 80);
  return clean || "论文草稿";
}

function download(blob: Blob, filename: string): void {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 4000);
}

/**
 * 导出前抓取正文里每张图的字节：编辑器里的插图是同源产物下载链接（Cookie 鉴权），原样写进
 * 文件到了用户电脑上就是死链。返回正文副本（流式光标等编辑态节点已摘掉）与图件清单，
 * 副本里每个 <img> 记下自己在清单里的序号；抓不到的图保留原链接、不阻断导出。
 */
export async function collectPaperImages(page: HTMLElement): Promise<{ clone: HTMLElement; images: EmbeddedImage[] }> {
  // 副本放在 <template> 的惰性文档里：后面读结构时浏览器不会去真加载图片
  const template = document.createElement("template");
  template.innerHTML = `<div>${page.innerHTML}</div>`;
  const clone = template.content.firstElementChild as HTMLElement;
  clone.querySelectorAll(".editor-stream-caret").forEach(node => node.remove());
  const sources = [...page.querySelectorAll("img")];
  const targets = [...clone.querySelectorAll("img")];
  const images: EmbeddedImage[] = [];
  const used = new Set<string>();
  for (let index = 0; index < targets.length; index += 1) {
    const target = targets[index];
    const source = sources[index];
    const src = target.getAttribute("src") || "";
    if (!src) continue;
    try {
      const response = await fetch(src, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const blob = await response.blob();
      const mediaType = blob.type || "image/png";
      images.push({
        name: exportFileName(target.dataset.figureName || target.alt || "", images.length + 1, mediaType, used),
        mediaType,
        bytes: new Uint8Array(await blob.arrayBuffer()),
        width: source?.naturalWidth || undefined,
        height: source?.naturalHeight || undefined,
      });
      target.dataset.exportIndex = String(images.length - 1);
    } catch {
      // 抓不到（网络 / 权限）就保留原链接，模型里记成缺图占位
    }
  }
  return { clone, images };
}

/** Word 只认 PNG / JPEG / GIF / BMP：其余格式（SVG、WebP…）先在画布上转成 PNG。 */
async function rasterizeForWord(image: EmbeddedImage): Promise<EmbeddedImage> {
  if (/^image\/(png|jpe?g|gif|bmp)$/i.test(image.mediaType)) return image;
  const url = URL.createObjectURL(new Blob([image.bytes as BlobPart], { type: image.mediaType }));
  try {
    const element = new Image();
    element.src = url;
    await element.decode();
    const width = element.naturalWidth || 1200;
    const height = element.naturalHeight || 800;
    const scale = Math.min(2, 2400 / width);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d");
    if (!context) return image;
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(element, 0, 0, canvas.width, canvas.height);
    const png = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/png"));
    if (!png) return image;
    return {
      name: image.name.replace(/\.[A-Za-z0-9]{1,5}$/, "") + ".png",
      mediaType: "image/png",
      bytes: new Uint8Array(await png.arrayBuffer()),
      width,
      height,
    };
  } catch {
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

let katexCssCache: Promise<string> | null = null;

/**
 * KaTeX 样式表，字体全部内联成 data URL（只留 woff2）：导出的 HTML 离线打开、服务端打印时网络
 * 全部拦截，都要靠它把公式排对。抓字体失败时退回不带字体的样式（公式仍可读，只是字形不对）。
 */
function inlineKatexCss(): Promise<string> {
  katexCssCache ??= (async () => {
    const css = (await import("katex/dist/katex.min.css?inline")).default as string;
    const faces = [...css.matchAll(/@font-face\s*\{[^}]*\}/g)].map(match => match[0]);
    const inlined = await Promise.all(faces.map(async face => {
      const href = /url\(\s*["']?([^"')]+\.woff2)["']?\s*\)/.exec(face)?.[1];
      if (!href) return [face, face] as const;
      try {
        const response = await fetch(new URL(href, window.location.href), { credentials: "same-origin" });
        if (!response.ok) throw new Error(String(response.status));
        const bytes = new Uint8Array(await response.arrayBuffer());
        const source = `src:url(data:font/woff2;base64,${bytesToBase64(bytes)}) format("woff2")`;
        return [face, face.replace(/src\s*:[^;}]+/, source)] as const;
      } catch {
        return [face, face] as const;
      }
    }));
    return inlined.reduce((text, [before, after]) => text.replace(before, after), css);
  })().catch(error => {
    katexCssCache = null;
    throw error;
  });
  return katexCssCache;
}

/** 文档模型 → 自足 HTML（KaTeX 排版 + 内联字体）：「导出 HTML」与服务端打印 PDF 共用。 */
export async function buildPaperHtml(doc: PaperDocument): Promise<string> {
  const katex = await loadKatex();
  let katexCss = "";
  try {
    katexCss = await inlineKatexCss();
  } catch {
    // 样式加载失败：公式退化为浏览器默认字体，正文照常导出
  }
  return paperDocumentToHtml(doc, {
    katexCss,
    renderMath: (tex, display) => {
      try {
        return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "htmlAndMathml" });
      } catch {
        return `<code>${tex.replace(/[&<>]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[char]!)}</code>`;
      }
    },
  });
}

// ── PDF：服务端打印 ──────────────────────────────────────────────────────────

interface PaperExportRecord {
  id: string;
  status: "QUEUED" | "RUNNING" | "READY" | "FAILED" | "UNSUPPORTED";
  artifact_id?: string | null;
  detail?: string | null;
}

/** 论文页 URL 上的项目与运行（/workspace/paper-editor?project_id=…&run_id=…）。 */
function currentRunContext(): { projectId: string | null; runId: string | null } {
  const params = new URL(window.location.href).searchParams;
  const projectId = params.get("project_id") ?? "";
  const runId = params.get("run_id") ?? "";
  return {
    projectId: /^proj_[0-9a-f]{32}$/.test(projectId) ? projectId : null,
    runId: /^run_[0-9a-f]{32}$/.test(runId) ? runId : null,
  };
}

class PdfServiceUnavailable extends Error {}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { message?: string };
    return payload.message || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function requestServerPdf(html: string, title: string, notify: Notify): Promise<void> {
  const { projectId, runId } = currentRunContext();
  if (!projectId) throw new PdfServiceUnavailable(t("当前论文没有关联项目"));
  notify("正在生成 PDF，约需几秒……", 8000);
  const created = await fetch("/api/v1/paper-exports", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ project_id: projectId, run_id: runId, format: "pdf", title: title.slice(0, 300), source_html: html }),
  });
  if (created.status === 409) {
    // 同一用户同时只排一个导出：明说原因，不悄悄退回打印
    throw new Error(await readError(created));
  }
  if (!created.ok) throw new PdfServiceUnavailable(await readError(created));
  let record = (await created.json()) as PaperExportRecord;
  const deadline = Date.now() + 120_000;
  while (record.status === "QUEUED" || record.status === "RUNNING") {
    if (Date.now() > deadline) throw new PdfServiceUnavailable(t("服务端生成超时"));
    await new Promise(resolve => setTimeout(resolve, 700));
    const polled = await fetch(`/api/v1/paper-exports/${encodeURIComponent(record.id)}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!polled.ok) throw new PdfServiceUnavailable(await readError(polled));
    record = (await polled.json()) as PaperExportRecord;
  }
  if (record.status !== "READY" || !record.artifact_id) {
    throw new PdfServiceUnavailable(record.detail || t("服务端未能生成 PDF"));
  }
  const link = document.createElement("a");
  link.href = `/api/v1/artifacts/${encodeURIComponent(record.artifact_id)}/download`;
  link.download = `${exportBaseName(title)}.pdf`;
  link.click();
}

/** 退路：浏览器打印（会弹打印框，用户在里面选「另存为 PDF」）。 */
async function printFallback(html: string, notify: Notify, reason: string): Promise<void> {
  const preview = window.open("", "_blank");
  if (!preview) {
    notify(fill("{reason}；浏览器又拦截了打印窗口，请允许弹出窗口后重试", { reason }), 6000);
    return;
  }
  preview.document.write(html);
  preview.document.close();
  preview.focus();
  const pending = [...preview.document.images].filter(img => !img.complete).map(img => new Promise(resolve => {
    img.addEventListener("load", resolve, { once: true });
    img.addEventListener("error", resolve, { once: true });
  }));
  await Promise.race([Promise.all(pending), new Promise(resolve => setTimeout(resolve, 4000))]);
  notify(fill("{reason}，已改用浏览器打印：在打印窗口选择「另存为 PDF」", { reason }), 6000);
  setTimeout(() => preview.print(), 300);
}

// ── 入口 ────────────────────────────────────────────────────────────────────

/** 按菜单选项导出编辑器正文。 */
export async function exportPaper(page: HTMLElement, choice: PaperExportChoice, notify: Notify): Promise<void> {
  const title = page.querySelector("h1")?.textContent?.trim() || "论文草稿";
  const base = exportBaseName(title);
  const { clone, images } = await collectPaperImages(page);

  if (choice === "导出 Word (.docx)") {
    const wordImages = await Promise.all(images.map(rasterizeForWord));
    const doc = readPaperDocument(clone, wordImages, title);
    const katex = await loadKatex();
    const bytes = buildDocx(doc, {
      texToMathml: (tex, display) => katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: false }),
    });
    download(new Blob([bytes as BlobPart], { type: DOCX_MEDIA_TYPE }), `${base}.docx`);
    notify(images.length ? fill("已导出 Word 文档（含 {count} 张图，公式可在 Word 里编辑）", { count: images.length }) : "已导出 Word 文档");
    return;
  }

  const doc = readPaperDocument(clone, images, title);

  if (choice === "导出 LaTeX (.zip)") {
    const paths = images.map((image, index) => `figures/${latexImageName(image.name, index, image.mediaType)}`);
    const tex = paperDocumentToLatex(doc, { imagePath: index => paths[index] ?? null });
    const files: Record<string, Uint8Array> = { "main.tex": strToU8(tex) };
    images.forEach((image, index) => { files[paths[index]] = image.bytes; });
    download(new Blob([zipSync(files, { level: 6 }) as BlobPart], { type: "application/zip" }), `${base}.zip`);
    notify(images.length
      ? fill("已导出 LaTeX 工程（main.tex + {count} 张图）：解压后 xelatex 编译，或整包上传 Overleaf", { count: images.length })
      : "已导出 LaTeX 工程（main.tex）：xelatex 编译，或整包上传 Overleaf", 4000);
    return;
  }

  const html = await buildPaperHtml(doc);
  if (choice === "导出 HTML") {
    download(new Blob([html], { type: "text/html;charset=utf-8" }), `${base}.html`);
    notify(images.length ? fill("已导出 HTML 文件（含 {count} 张图）", { count: images.length }) : "已导出 HTML 文件");
    return;
  }

  try {
    await requestServerPdf(html, title, notify);
    notify("PDF 已生成，正在下载");
  } catch (error) {
    if (error instanceof PdfServiceUnavailable) {
      await printFallback(html, notify, fill("服务端暂时无法生成 PDF（{reason}）", { reason: error.message }));
      return;
    }
    notify(error instanceof Error ? error.message : "PDF 导出失败，请稍后重试", 5000);
  }
}
