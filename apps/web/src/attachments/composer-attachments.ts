/**
 * 把附件能力挂到现有的 `.composer` 输入框上。
 *
 * 三个入口共用同一个附件集合：拖拽（含整个文件夹）、粘贴、点击工具栏的“+”。
 * 只在 composer 内部追加两个槽位——附件列表和拖拽提示层——不改动输入框既有的
 * 结构、工具栏顺序和发送按钮。
 */

import { ACCEPT_ATTRIBUTE } from "./formats";
import { MAX_FILE_COUNT, formatBytes } from "./limits";
import type { ParseStatus } from "./parse";
import { createAttachmentStore, type Attachment, type AttachmentStore } from "./store";

const registry = new WeakMap<Element, AttachmentStore>();
let windowGuardBound = false;

/** 拖拽层级计数：子元素间移动会连续触发 enter/leave，靠计数才不会闪。 */
interface DragState {
  depth: number;
}

function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

function readDirectory(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walkEntry(entry: FileSystemEntry, collected: File[], depth: number): Promise<void> {
  if (collected.length >= MAX_FILE_COUNT || depth > 4) return;
  if (entry.isFile) {
    const file = await new Promise<File | null>(resolve => {
      (entry as FileSystemFileEntry).file(resolve, () => resolve(null));
    });
    if (file) collected.push(file);
    return;
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  // readEntries 每次最多返回一批，必须反复读到空批次才算读完整个目录。
  for (;;) {
    const batch = await readDirectory(reader).catch(() => []);
    if (batch.length === 0) break;
    for (const child of batch) await walkEntry(child, collected, depth + 1);
  }
}

/**
 * 从一次 drop 里取出全部文件。DataTransfer 在事件回调返回后就失效，
 * 因此 entries 和 files 都必须在第一个 await 之前同步取出来。
 */
async function collectDroppedFiles(transfer: DataTransfer): Promise<File[]> {
  const flat = Array.from(transfer.files);
  const entries = Array.from(transfer.items)
    .filter(item => item.kind === "file")
    .map(item => item.webkitGetAsEntry?.() ?? null)
    .filter((entry): entry is FileSystemEntry => entry !== null);
  if (entries.length === 0) return flat;

  const collected: File[] = [];
  for (const entry of entries) await walkEntry(entry, collected, 0);
  return collected.length > 0 ? collected : flat;
}

const PARSE_STATUS_TEXT: Record<ParseStatus, string> = {
  ready: "已解析",
  partial: "已部分解析",
  "server-pending": "等待服务端解析",
  empty: "未提取到文字",
  failed: "解析失败",
};

function summarize(attachment: Attachment): { state: string; meta: string; notice?: string } {
  const { descriptor, file, parse } = attachment;
  const facts = [descriptor.label, formatBytes(file.size)];

  if (attachment.phase === "parsing") {
    return { state: "parsing", meta: [...facts, "正在解析…"].join(" · ") };
  }
  if (attachment.phase === "uploading") {
    return { state: "uploading", meta: [...facts, "正在上传…"].join(" · ") };
  }
  if (attachment.phase === "upload-failed") {
    return { state: "failed", meta: facts.join(" · "), notice: attachment.uploadError ?? "上传失败" };
  }

  facts.push(...(parse?.metrics ?? []).map(metric => metric.value));
  if (parse && parse.characters > 0) facts.push(`已提取 ${parse.characters.toLocaleString("zh-CN")} 字`);
  else if (parse) facts.push(PARSE_STATUS_TEXT[parse.status]);
  if (attachment.phase === "uploaded") facts.push("已上传");

  const state = attachment.phase === "uploaded" ? "uploaded"
    : parse?.status === "failed" ? "failed"
      : parse?.status === "server-pending" || parse?.status === "empty" ? "pending"
        : "ready";
  return { state, meta: facts.join(" · "), notice: parse?.notice };
}

function buildCard(attachment: Attachment): HTMLElement {
  const { state, meta, notice } = summarize(attachment);
  const card = document.createElement("div");
  card.className = "composer-attachment";
  card.dataset.attachmentId = attachment.id;
  card.dataset.state = state;

  const icon = document.createElement("i");
  icon.className = `ph ph-${attachment.descriptor.icon} composer-attachment-icon`;
  icon.setAttribute("aria-hidden", "true");

  const copy = document.createElement("span");
  copy.className = "composer-attachment-copy";
  const name = document.createElement("strong");
  name.className = "composer-attachment-name";
  name.textContent = attachment.file.name;
  name.title = attachment.file.name;
  const detail = document.createElement("small");
  detail.className = "composer-attachment-meta";
  detail.textContent = meta;
  copy.append(name, detail);
  if (notice) {
    const hint = document.createElement("small");
    hint.className = "composer-attachment-notice";
    hint.textContent = notice;
    copy.append(hint);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "composer-attachment-remove";
  remove.dataset.attachmentRemove = attachment.id;
  remove.setAttribute("aria-label", `移除附件 ${attachment.file.name}`);
  remove.innerHTML = '<i class="ph ph-x" aria-hidden="true"></i>';

  card.append(icon, copy, remove);
  return card;
}

function buildDropzone(): HTMLElement {
  const zone = document.createElement("div");
  zone.className = "composer-dropzone";
  zone.dataset.composerDropzone = "";
  zone.setAttribute("aria-hidden", "true");
  zone.innerHTML = '<i class="ph ph-upload-simple" aria-hidden="true"></i>'
    + "<strong>松开即可添加文件</strong>"
    + "<small>支持 PDF、Word、Excel、PowerPoint、Markdown、CSV、图片与压缩包</small>";
  return zone;
}

function buildTray(): HTMLElement {
  const tray = document.createElement("div");
  tray.className = "composer-attachments";
  tray.dataset.composerAttachments = "";
  tray.hidden = true;
  tray.innerHTML = '<div class="composer-attachment-list" data-composer-attachment-list></div>'
    + '<p class="composer-attachment-status" data-composer-attachment-status role="status" hidden></p>';
  return tray;
}

/** 拖到窗口任意位置都别让浏览器直接打开文件，否则用户会丢掉正在写的任务描述。 */
function bindWindowGuard(): void {
  if (windowGuardBound) return;
  windowGuardBound = true;
  const block = (event: DragEvent): void => {
    if (!hasFiles(event)) return;
    if (event.target instanceof Element && event.target.closest(".composer")) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "none";
  };
  window.addEventListener("dragover", block);
  window.addEventListener("drop", block);
}

function mountOne(composer: HTMLElement): void {
  if (composer.dataset.attachmentsBound === "true") return;
  composer.dataset.attachmentsBound = "true";

  const store = createAttachmentStore();
  registry.set(composer, store);

  const tray = buildTray();
  const tools = composer.querySelector(".composer-tools");
  composer.insertBefore(tray, tools);
  composer.append(buildDropzone());

  const list = tray.querySelector<HTMLElement>("[data-composer-attachment-list]");
  const status = tray.querySelector<HTMLElement>("[data-composer-attachment-status]");
  const fileInput = composer.querySelector<HTMLInputElement>("input[type=\"file\"]");
  if (fileInput) fileInput.accept = ACCEPT_ATTRIBUTE;

  const render = (): void => {
    const attachments = store.list();
    // 聊天页的输入框是定高绝对定位的，挂上附件后要靠这个类切成自适应高度。
    composer.classList.toggle("has-attachments", attachments.length > 0);
    tray.hidden = attachments.length === 0 && (status?.hidden ?? true);
    list?.replaceChildren(...attachments.map(buildCard));
  };

  const report = (message: string, kind: "status" | "error"): void => {
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
    status.dataset.kind = kind;
    if (message) tray.hidden = false;
  };

  const intake = (files: readonly File[]): void => {
    if (files.length === 0) return;
    const rejected = store.add(files);
    if (rejected.length === 0) report("", "status");
    else report(rejected.map(item => `${item.name}：${item.reason}`).join("；"), "error");
    render();
  };

  store.subscribe(render);

  const state: DragState = { depth: 0 };
  const setDragging = (active: boolean): void => {
    composer.classList.toggle("is-dropping", active);
  };

  composer.addEventListener("dragenter", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    state.depth += 1;
    setDragging(true);
  });
  composer.addEventListener("dragover", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
  composer.addEventListener("dragleave", event => {
    if (!hasFiles(event)) return;
    state.depth = Math.max(0, state.depth - 1);
    if (state.depth === 0) setDragging(false);
  });
  composer.addEventListener("drop", event => {
    if (!hasFiles(event) || !event.dataTransfer) return;
    event.preventDefault();
    state.depth = 0;
    setDragging(false);
    void collectDroppedFiles(event.dataTransfer).then(intake);
  });

  composer.addEventListener("paste", event => {
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length === 0) return;
    // 剪贴板同时带文字和文件时以文字为准，避免复制一段话就误传截图。
    if (event.clipboardData?.getData("text/plain").trim()) return;
    event.preventDefault();
    intake(files);
  });

  fileInput?.addEventListener("change", () => {
    intake(Array.from(fileInput.files ?? []));
    // 清空原生选择，移除后再选同一个文件才能重新触发 change。
    fileInput.value = "";
  });

  composer.addEventListener("click", event => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>("[data-attachment-remove]")
      : null;
    if (!target?.dataset.attachmentRemove) return;
    event.preventDefault();
    event.stopPropagation();
    store.remove(target.dataset.attachmentRemove);
    report("", "status");
    render();
  });
}

/** 每次切屏调用：为当前页面里新渲染出来的输入框补挂附件能力。 */
export function mountComposerAttachments(): void {
  bindWindowGuard();
  document.querySelectorAll<HTMLElement>(".composer").forEach(mountOne);
}

/** 取指定区域内输入框的附件集合，供任务创建流程读取解析结果。 */
export function attachmentsWithin(root: ParentNode): AttachmentStore | undefined {
  const composer = root.querySelector(".composer");
  return composer ? registry.get(composer) : undefined;
}
