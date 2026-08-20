/**
 * 把输入框附件折算成任务草稿里的附件条目。
 *
 * 草稿要落 sessionStorage（通常 5MB），任务参数还会进数据库和事件流，所以这里
 * 只带受控长度的摘要：几十万字的正文留在服务端，由它解析上传后的产物得到。
 */

import type { TaskAttachmentDraft } from "../integration/task-start-state";
import { MAX_DRAFT_EXCERPT_CHARS, MAX_DRAFT_EXCERPT_TOTAL } from "./limits";
import type { Attachment } from "./store";

export type AttachmentUploadState = "none" | "uploaded" | "partial" | "metadata_only";

export function toDraftAttachments(attachments: readonly Attachment[]): TaskAttachmentDraft[] {
  let budget = MAX_DRAFT_EXCERPT_TOTAL;
  return attachments.map(attachment => {
    const { file, parse, descriptor } = attachment;
    const allowance = Math.min(MAX_DRAFT_EXCERPT_CHARS, budget);
    const excerpt = parse?.text.slice(0, allowance) ?? "";
    budget -= excerpt.length;
    return {
      name: file.name.slice(0, 300),
      size: file.size,
      type: file.type,
      last_modified: file.lastModified,
      format: descriptor.label,
      ...(parse ? { parse_status: parse.status } : {}),
      ...(parse?.characters ? { characters: parse.characters } : {}),
      ...(parse?.images ? { images: parse.images } : {}),
      ...(excerpt ? { excerpt } : {}),
      ...(attachment.artifactId ? { artifact_id: attachment.artifactId } : {}),
    };
  });
}

export function uploadStateOf(attachments: readonly Attachment[]): AttachmentUploadState {
  if (attachments.length === 0) return "none";
  const uploaded = attachments.filter(attachment => attachment.artifactId).length;
  if (uploaded === attachments.length) return "uploaded";
  return uploaded === 0 ? "metadata_only" : "partial";
}
