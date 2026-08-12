/**
 * 把输入框里的附件二进制真正送上服务端。
 *
 * 必须在项目创建之后、任务创建之前跑完：产物接口以 project_id 为归属，而
 * `auto_start` 的任务一旦创建 Agent 就开始跑，附件晚到就赶不上第一轮上下文。
 */

import type { Artifact } from "@openmathmodel/contracts";
import { WorkspaceApiError } from "../integration/modeling-workspace-api";
import type { Attachment, AttachmentStore } from "./store";

const ARTIFACT_ID_PATTERN = /^art_[0-9a-f]{32}$/;

export interface UploadReport {
  uploaded: number;
  failed: number;
  total: number;
}

async function uploadOne(
  attachment: Attachment,
  projectId: string,
  signal: AbortSignal,
): Promise<string> {
  const body = new FormData();
  body.append("file", attachment.file, attachment.file.name);
  body.append("kind", attachment.descriptor.artifactKind);

  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/artifacts`, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    body,
    signal,
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new WorkspaceApiError(response.status, (payload ?? {}) as { code?: string; message?: string });
  }
  const artifact = payload as Artifact;
  if (!ARTIFACT_ID_PATTERN.test(artifact?.id ?? "")) throw new Error("产物接口返回了无效的 artifact_id");
  return artifact.id;
}

/**
 * 逐个串行上传：并发上传大附件会把上行带宽打满，进度条反而全卡在最后一刻跳完。
 * 已经拿到 artifact_id 的跳过，失败重试不会重复上传同一个文件。
 */
export async function uploadAttachments(
  store: AttachmentStore,
  projectId: string,
  signal: AbortSignal,
  onProgress?: (done: number, total: number) => void,
): Promise<UploadReport> {
  const attachments = store.list();
  const queue = attachments.filter(attachment => !attachment.artifactId);
  let uploaded = attachments.length - queue.length;
  let failed = 0;

  for (const attachment of queue) {
    if (signal.aborted) break;
    store.update(attachment.id, { phase: "uploading", uploadError: undefined });
    try {
      const artifactId = await uploadOne(attachment, projectId, signal);
      uploaded += 1;
      store.update(attachment.id, { phase: "uploaded", artifactId });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      failed += 1;
      store.update(attachment.id, {
        phase: "upload-failed",
        uploadError: error instanceof Error ? error.message : "上传失败",
      });
    }
    onProgress?.(uploaded + failed, attachments.length);
  }

  return { uploaded, failed, total: attachments.length };
}
