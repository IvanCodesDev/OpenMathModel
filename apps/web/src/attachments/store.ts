/**
 * 输入框附件集合的状态机。
 *
 * 一个附件的生命周期是：加入 → 浏览器解析 → 随任务上传 → 服务端权威解析。
 * 这里只管前两段和上传结果的回写，解析本身交给 parse.ts，上传交给 upload.ts。
 */

import { autoParseAttachmentsEnabled } from "../preferences/task-preferences";
import { describeFormat, type FormatDescriptor } from "./formats";
import { MAX_FILE_BYTES, MAX_FILE_COUNT, MAX_TOTAL_BYTES, formatBytes } from "./limits";
import { parseAttachment, type ParseOutcome } from "./parse";

export type AttachmentPhase = "parsing" | "parsed" | "uploading" | "uploaded" | "upload-failed";

export interface Attachment {
  readonly id: string;
  readonly file: File;
  readonly descriptor: FormatDescriptor;
  phase: AttachmentPhase;
  parse?: ParseOutcome;
  artifactId?: string;
  uploadError?: string;
}

export interface RejectedFile {
  name: string;
  reason: string;
}

export interface AttachmentStore {
  list(): readonly Attachment[];
  add(files: readonly File[]): RejectedFile[];
  remove(id: string): void;
  clear(): void;
  update(id: string, patch: Partial<Omit<Attachment, "id" | "file" | "descriptor">>): void;
  subscribe(listener: () => void): () => void;
  /** 等待所有在途解析结束；上传前要先拿到解析摘要。 */
  settled(): Promise<void>;
}

/** 同名同大小同修改时间视为同一个文件，避免重复拖拽刷出一堆副本。 */
function identityOf(file: File): string {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

export function createAttachmentStore(): AttachmentStore {
  const items = new Map<string, Attachment>();
  const identities = new Map<string, string>();
  const listeners = new Set<() => void>();
  const pending = new Set<Promise<void>>();

  const notify = (): void => listeners.forEach(listener => listener());

  const totalBytes = (): number =>
    Array.from(items.values()).reduce((sum, item) => sum + item.file.size, 0);

  function startParsing(attachment: Attachment): void {
    // 设置里关掉「自动解析上传文件」时不在浏览器里抽取内容：直接占位成
    // 等待服务端解析，settled() 无在途任务，上传与草稿流程照常走。
    if (!autoParseAttachmentsEnabled()) {
      attachment.parse = {
        status: "server-pending",
        text: "",
        characters: 0,
        metrics: [],
        notice: "已按设置关闭自动解析，文件将在上传后由服务端解析",
      };
      attachment.phase = "parsed";
      return;
    }
    const work = parseAttachment(attachment.file).then(outcome => {
      // 解析是异步的，期间用户可能已经把这个附件删了。
      const current = items.get(attachment.id);
      if (!current) return;
      current.parse = outcome;
      current.phase = "parsed";
      notify();
    });
    pending.add(work);
    void work.finally(() => pending.delete(work));
  }

  return {
    list: () => Array.from(items.values()),

    add(files) {
      const rejected: RejectedFile[] = [];
      let accepted = 0;
      let projectedBytes = totalBytes();

      for (const file of files) {
        const identity = identityOf(file);
        if (identities.has(identity)) {
          rejected.push({ name: file.name, reason: "已在附件列表中" });
          continue;
        }
        if (items.size + accepted >= MAX_FILE_COUNT) {
          rejected.push({ name: file.name, reason: `最多同时添加 ${MAX_FILE_COUNT} 个文件` });
          continue;
        }
        if (file.size === 0) {
          rejected.push({ name: file.name, reason: "文件是空的" });
          continue;
        }
        if (file.size > MAX_FILE_BYTES) {
          rejected.push({ name: file.name, reason: `超过单文件 ${formatBytes(MAX_FILE_BYTES)} 上限` });
          continue;
        }
        if (projectedBytes + file.size > MAX_TOTAL_BYTES) {
          rejected.push({ name: file.name, reason: `超过合计 ${formatBytes(MAX_TOTAL_BYTES)} 上限` });
          continue;
        }

        const attachment: Attachment = {
          id: crypto.randomUUID(),
          file,
          descriptor: describeFormat(file.name, file.type),
          phase: "parsing",
        };
        items.set(attachment.id, attachment);
        identities.set(identity, attachment.id);
        projectedBytes += file.size;
        accepted += 1;
        startParsing(attachment);
      }

      if (accepted > 0 || rejected.length > 0) notify();
      return rejected;
    },

    remove(id) {
      const attachment = items.get(id);
      if (!attachment) return;
      items.delete(id);
      identities.delete(identityOf(attachment.file));
      notify();
    },

    clear() {
      if (items.size === 0) return;
      items.clear();
      identities.clear();
      notify();
    },

    update(id, patch) {
      const attachment = items.get(id);
      if (!attachment) return;
      Object.assign(attachment, patch);
      notify();
    },

    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    async settled() {
      // 解析过程中还能继续拖文件进来，要一直等到没有在途任务为止。
      while (pending.size > 0) await Promise.all(Array.from(pending));
    },
  };
}
