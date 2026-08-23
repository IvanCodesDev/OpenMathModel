/**
 * 视觉模型的图片直通（ADR-0010 直通阶梯切片一）。
 *
 * 生效模型具备视觉能力时，托盘里的位图附件以 base64 原图随 /api/chat 消息
 * 直发给模型，跳过分钟级的服务端 OCR；超限或视觉接口不收的格式回落 OCR
 * 文本通道。选取逻辑是纯函数，便于 node 单测；编码依赖浏览器 File API。
 */

import { fileExtension } from "./formats";
import type { Attachment } from "./store";

/** 后端 ChatImageModel 白名单的镜像：四种主流位图（bmp/tiff 各家视觉 API 不收）。 */
const MEDIA_TYPE_BY_EXTENSION: Readonly<Record<string, string>> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
};

/** 单图原始字节上限 ≈4MB：各协议单图/整请求上限的保守交集（后端 CHAT_IMAGE_MAX_BASE64 同源）。 */
export const MAX_PASSTHROUGH_IMAGE_BYTES = 4 * 1024 * 1024;
/** 每条消息最多直通的图片数（与后端 CHAT_IMAGE_MAX_COUNT 对齐）。 */
export const MAX_PASSTHROUGH_IMAGE_COUNT = 4;

/** 与后端 ChatImageModel 对齐的直通图片载荷。 */
export interface ChatImagePayload {
  media_type: string;
  /** 纯 base64 内容（不带 data: URL 前缀） */
  data: string;
  name: string;
}

export interface PassthroughSkip {
  attachment: Attachment;
  reason: string;
}

export interface PassthroughPlan {
  /** 将以原图直通的图片附件 */
  send: Attachment[];
  /** 是图片但不直通的附件与原因，仍走 OCR 文本通道 */
  skipped: PassthroughSkip[];
}

/** 直通用的媒体类型按扩展名判定：托盘文件常来自压缩包，浏览器 MIME 不可靠。 */
export function passthroughMediaType(attachment: Attachment): string {
  return MEDIA_TYPE_BY_EXTENSION[fileExtension(attachment.file.name)] ?? "";
}

/** 从托盘附件中选出可直通的图片；非图片附件不出现在结果里。 */
export function planImagePassthrough(items: readonly Attachment[]): PassthroughPlan {
  const plan: PassthroughPlan = { send: [], skipped: [] };
  for (const item of items) {
    if (item.descriptor.artifactKind !== "figure") continue;
    if (!passthroughMediaType(item)) {
      plan.skipped.push({ attachment: item, reason: "视觉接口不支持该图片格式，改走 OCR 文字识别" });
      continue;
    }
    if (item.file.size > MAX_PASSTHROUGH_IMAGE_BYTES) {
      plan.skipped.push({ attachment: item, reason: "图片超过 4MB 直通上限，改走 OCR 文字识别" });
      continue;
    }
    if (plan.send.length >= MAX_PASSTHROUGH_IMAGE_COUNT) {
      plan.skipped.push({
        attachment: item,
        reason: `单条消息最多直通 ${MAX_PASSTHROUGH_IMAGE_COUNT} 张图，改走 OCR 文字识别`,
      });
      continue;
    }
    plan.send.push(item);
  }
  return plan;
}

/** File → 纯 base64。分块过 btoa，避免大图一次性展开成超长参数列表。 */
async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const CHUNK = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + CHUNK));
  }
  return btoa(binary);
}

export async function encodePassthroughImages(
  attachments: readonly Attachment[],
): Promise<ChatImagePayload[]> {
  return Promise.all(
    attachments.map(async attachment => ({
      media_type: passthroughMediaType(attachment),
      data: await fileToBase64(attachment.file),
      name: attachment.file.name,
    })),
  );
}
