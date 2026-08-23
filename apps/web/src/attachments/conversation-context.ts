/**
 * 把输入框附件折算成随对话消息发送的上下文块（ADR-0010 批次三）。
 *
 * 文本类附件直接用浏览器解析结果；浏览器解不动的（图片、扫描件、旧格式）
 * 现场调用服务端即席解析（含可选 VL），并把权威结果如实回写附件卡片——
 * 用户在发送瞬间就能看到「等待服务端解析」变成真实状态。
 */

import { localFirstEnabled } from "../preferences/privacy-preferences";
import { parseAttachmentOnServer, type AdhocParseResult } from "./adhoc-parse";
import { formatBytes } from "./limits";
import type { ParseOutcome } from "./parse";
import type { Attachment, AttachmentStore } from "./store";

/** 单附件并入对话的正文上限与总预算：对话上下文比草稿摘要（4000）宽裕。 */
const PER_ATTACHMENT_CAP = 8000;
const TOTAL_BUDGET = 20000;

/** 服务端状态映射到卡片状态：unsupported 不是失败，按「未提取到文字」展示原因。 */
function outcomeFromServer(result: AdhocParseResult): ParseOutcome {
  const status = result.status === "unsupported" ? "empty"
    : result.status === "failed" ? "failed"
      : result.status;
  return {
    status,
    text: result.text,
    characters: result.characters,
    metrics: [],
    notice: result.detail ?? (result.status === "ready" ? `服务端已解析（${result.engine}）` : undefined),
    ...(result.images ? { images: result.images } : {}),
  };
}

export interface ConversationAttachmentContext {
  /** 并入用户消息的上下文块；无附件时为空串 */
  block: string;
  names: string[];
}

async function resolveText(store: AttachmentStore, attachment: Attachment): Promise<string> {
  const local = attachment.parse?.text?.trim() ?? "";
  // 「敏感文件优先本地处理」（数据与隐私）开启时，浏览器能解析的内容不再上传；
  // 关闭时改用服务端权威解析（OCR 与内嵌图统计更完整），本地结果仅作兜底。
  if (local && localFirstEnabled()) return local;
  // 浏览器没抽到文字（图片/扫描件/旧格式/解析被关）→ 服务端即席解析兜底。
  const parsed = await parseAttachmentOnServer(attachment.file);
  if (!parsed) {
    // 服务端没能在预算内给出结果（超时/未登录/异常）。「已排队交由服务端识别」
    // 是入队时的占位说明，此刻已不属实——如实改写卡片与上下文，免得模型向
    // 用户转述一个并不存在的队列。
    if (!local) {
      store.update(attachment.id, {
        parse: {
          ...(attachment.parse ?? { text: "", characters: 0, metrics: [] }),
          status: "failed",
          notice: "服务端解析超时或暂不可用，本次消息未能读取该附件内容，可稍后重试",
        },
        phase: "parsed",
      });
    }
    return local;
  }
  store.update(attachment.id, { parse: outcomeFromServer(parsed), phase: "parsed" });
  return parsed.text.trim() || local;
}

export async function collectConversationAttachments(
  store: AttachmentStore,
  passthroughIds?: ReadonlySet<string>,
): Promise<ConversationAttachmentContext> {
  await store.settled();
  const items = store.list();
  if (items.length === 0) return { block: "", names: [] };

  let budget = TOTAL_BUDGET;
  const sections: string[] = [];
  const names: string[] = [];
  for (const item of items) {
    names.push(item.file.name);
    if (passthroughIds?.has(item.id)) {
      // 原图已直通视觉模型（ADR-0010）：跳过分钟级 OCR，上下文只留元信息；
      // 卡片如实改写，替换「已排队交由服务端识别」的占位说明。
      sections.push(
        `【附件：${item.file.name}】（${item.descriptor.label}，${formatBytes(item.file.size)}）\n`
        + "（原图已随本条消息直接提供给当前视觉模型，请直接读图作答）",
      );
      store.update(item.id, {
        parse: {
          ...(item.parse ?? { text: "", characters: 0, metrics: [] }),
          status: "ready",
          notice: "原图已直通视觉模型，无需 OCR",
        },
        phase: "parsed",
      });
      continue;
    }
    const text = await resolveText(store, item);
    const parse = store.list().find(candidate => candidate.id === item.id)?.parse;
    const meta = [item.descriptor.label];
    if (parse?.characters) meta.push(`${parse.characters.toLocaleString("zh-CN")} 字`);
    if (parse?.images) meta.push(`${parse.images} 张图`);
    const capped = text.slice(0, Math.max(0, Math.min(PER_ATTACHMENT_CAP, budget)));
    budget -= capped.length;
    const body = capped || `（未能提取文本${parse?.notice ? `：${parse.notice}` : ""}）`;
    sections.push(`【附件：${item.file.name}】（${meta.join("，")}）\n${body}`);
  }
  return {
    block: `用户随本条消息提供了 ${items.length} 个附件，内容如下：\n\n${sections.join("\n\n")}`,
    names,
  };
}
