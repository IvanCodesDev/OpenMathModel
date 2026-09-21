/**
 * 论文导出时把正文图件随文件带走的纯数据整形（不碰 DOM，node --test 直接断言）。
 *
 * 编辑器里的插图是同源产物下载链接（/api/v1/artifacts/{id}/download，Cookie 鉴权），
 * 原样写进导出文件后在用户电脑上就是死链——Word / 浏览器打开 .doc / .html 只会看到破图。
 * 所以导出前抓取每张图的字节，再按格式各自打包：
 *
 * - Word (.doc)：Word 不渲染 data: URL 图片，只认 MHTML（MIME 1.0 多部件「Web 档案」）——
 *   正文 HTML 与每张图各为一个部件，img src 指向图部件的 Content-Location（Word 自己
 *   「另存为 .mht」就是这个结构，扩展名写 .doc 照样识别）。
 * - HTML / 打印 PDF：data: URL 直接内联，单文件自足。
 * - LaTeX：源码与 figures/ 目录一起打成 .zip，\includegraphics 指向目录里的文件。
 */

export interface EmbeddedImage {
  /** 导出包内的文件名（figures/ 下），来自正文 data-figure-name，缺失时按序号造。 */
  name: string;
  mediaType: string;
  bytes: Uint8Array;
}

/** MHTML 各部件 Content-Location 的公共前缀：Word 只要求各部件地址一致可解析，目录本身不必存在。 */
export const MHTML_BASE = "file:///C:/OpenMathModel/";
const MHTML_BOUNDARY = "----=_NextPart_OpenMathModel_Paper";

const EXTENSION_BY_MEDIA: Record<string, string> = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/gif": ".gif",
  "image/svg+xml": ".svg",
  "image/webp": ".webp",
  "image/bmp": ".bmp",
};

/** 图件在导出包里的文件名：只留 basename、去掉路径与不安全字符，缺后缀按媒体类型补，重名加序号。 */
export function exportFileName(rawName: string, index: number, mediaType: string, used: Set<string>): string {
  const extension = EXTENSION_BY_MEDIA[mediaType.toLowerCase()] ?? "";
  let base = String(rawName ?? "").replace(/\\/g, "/").split("/").pop()?.split("?")[0]?.trim() ?? "";
  base = [...base].map(char => (char.charCodeAt(0) < 0x20 || '<>:"|*'.includes(char) ? "_" : char)).join("");
  if (!base || base === "." || base === "..") base = `figure-${index}`;
  if (extension && !/\.[A-Za-z0-9]{1,5}$/.test(base)) base += extension;
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate.toLowerCase())) {
    const dot = base.lastIndexOf(".");
    candidate = dot > 0 ? `${base.slice(0, dot)}-${suffix}${base.slice(dot)}` : `${base}-${suffix}`;
    suffix += 1;
  }
  used.add(candidate.toLowerCase());
  return candidate;
}

/** 字节 → base64（分块 btoa，避免一次 apply 上万个参数触发调用栈上限）。 */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}

export function dataUrl(image: EmbeddedImage): string {
  return `data:${image.mediaType};base64,${bytesToBase64(image.bytes)}`;
}

/** 图部件在 MHTML 里的地址；正文 HTML 的 img src 必须与它逐字一致。 */
export function mhtmlImageLocation(image: EmbeddedImage): string {
  return `${MHTML_BASE}figures/${encodeURIComponent(image.name)}`;
}

/** MIME 规定 base64 正文每行不超过 76 字符。 */
function wrap76(text: string): string {
  return text.replace(/(.{76})/g, "$1\r\n");
}

/**
 * MHTML 文档：multipart/related，首部件是正文 HTML（UTF-8 原文），其后每张图一个 base64 部件。
 * `html` 里的 img src 须已改写成 mhtmlImageLocation(image)。
 */
export function mhtmlDocument(html: string, images: readonly EmbeddedImage[]): string {
  const lines: string[] = [
    "MIME-Version: 1.0",
    `Content-Type: multipart/related; type="text/html"; boundary="${MHTML_BOUNDARY}"`,
    "",
    "",
    `--${MHTML_BOUNDARY}`,
    'Content-Type: text/html; charset="utf-8"',
    `Content-Location: ${MHTML_BASE}paper.htm`,
    "",
    html,
    "",
  ];
  for (const image of images) {
    lines.push(
      `--${MHTML_BOUNDARY}`,
      `Content-Type: ${image.mediaType}`,
      "Content-Transfer-Encoding: base64",
      `Content-Location: ${mhtmlImageLocation(image)}`,
      "",
      wrap76(bytesToBase64(image.bytes)),
      "",
    );
  }
  lines.push(`--${MHTML_BOUNDARY}--`, "");
  return lines.join("\r\n");
}

/** LaTeX 文本转义（与编辑器导出的 latexEscape 同规则；图题里没有公式）。 */
function latexEscape(text: string): string {
  return String(text)
    .replace(/\\/g, "\\textbackslash{}")
    .replace(/([%$#&_{}])/g, "\\$1")
    .replace(/~/g, "\\textasciitilde{}")
    .replace(/\^/g, "\\textasciicircum{}");
}

/** 一张插图的 LaTeX 浮动体：文件放在 figures/ 目录（随 .zip 一起交付）。 */
export function latexFigure(fileName: string, caption: string): string {
  // graphicx 对文件名里的空格与特殊字符敏感，路径整体加引号
  const path = `figures/${fileName}`.replace(/"/g, "");
  const captionLine = caption.trim() ? `\n\\caption*{${latexEscape(caption.trim())}}` : "";
  return `\\begin{figure}[htbp]\n\\centering\n\\includegraphics[width=0.8\\textwidth]{"${path}"}${captionLine}\n\\end{figure}`;
}
