/**
 * 论文导出时把正文图件随文件带走的纯数据整形（不碰 DOM，node --test 直接断言）。
 *
 * 编辑器里的插图是同源产物下载链接（/api/v1/artifacts/{id}/download，Cookie 鉴权），
 * 原样写进导出文件后在用户电脑上就是死链。所以导出前抓取每张图的字节，再按格式各自打包：
 *
 * - Word (.docx)：图件作为 word/media/ 部件内嵌（paper-docx）；
 * - HTML / PDF：data: URL 直接内联，单文件自足（paper-html）；
 * - LaTeX：源码与 figures/ 目录一起打成 .zip，\includegraphics 指向目录里的文件（paper-latex）。
 */

export interface EmbeddedImage {
  /** 导出包内的文件名（figures/ 下），来自正文 data-figure-name，缺失时按序号造。 */
  name: string;
  mediaType: string;
  bytes: Uint8Array;
  /** 原始像素尺寸（页面上 naturalWidth / naturalHeight）；缺省时由各格式从文件头读取。 */
  width?: number;
  height?: number;
}

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

/** 从文件头读像素尺寸（PNG / JPEG / GIF / BMP）；读不出返回 null。 */
export function imagePixelSize(image: EmbeddedImage): { width: number; height: number } | null {
  if (image.width && image.height) return { width: image.width, height: image.height };
  const bytes = image.bytes;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) {
    return { width: view.getUint32(16), height: view.getUint32(20) };
  }
  if (bytes.length >= 10 && bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46) {
    return { width: view.getUint16(6, true), height: view.getUint16(8, true) };
  }
  if (bytes.length >= 26 && bytes[0] === 0x42 && bytes[1] === 0x4d) {
    return { width: Math.abs(view.getInt32(18, true)), height: Math.abs(view.getInt32(22, true)) };
  }
  if (bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8) {
    // JPEG：逐段跳到 SOFn（C0–CF，除去 C4 / C8 / CC），其后 5 字节起是高、宽
    let offset = 2;
    while (offset + 9 < bytes.length) {
      if (bytes[offset] !== 0xff) { offset += 1; continue; }
      const marker = bytes[offset + 1];
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { width: view.getUint16(offset + 7), height: view.getUint16(offset + 5) };
      }
      if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { offset += 2; continue; }
      offset += 2 + view.getUint16(offset + 2);
    }
  }
  return null;
}
