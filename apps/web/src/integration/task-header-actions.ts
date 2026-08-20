/**
 * 任务执行页顶栏「附件 / 更多操作」的真实化。
 *
 * 模板顶栏的回形针数字、附件弹窗与三点菜单原是演示内容（写死的三个文件名与
 * 不落动作的菜单项）。绑定真实运行后：
 * - 回形针徽标显示随任务上传的真实附件数，没有附件时整个按钮隐藏；
 * - 附件弹窗按工作台视图列出真实文件（名称/大小），可下载的行点击即下载；
 * - 「重命名 / 复制 / 归档」全部是真实功能：重命名与归档 PATCH 项目（与侧栏
 *   「最近任务」同一接口），复制把当前任务链接写入剪贴板。
 *
 * 仅当工作台控制器绑定了真实运行时由它接管这些点击；演示态仍走模板层的
 * 演示弹层，互不打扰。视觉全部复用既有类（.menu / .modal / .focused-attachment）。
 */

import type { ModelingWorkspaceView } from "@openmathmodel/contracts";
import { copyTextToClipboard } from "../diagnostics/system-diagnostics";
import { t } from "../i18n/locale";
import { modelingWorkspaceApi } from "./modeling-workspace-api";
import { hydrateRecentTasks } from "./recent-tasks";
import { buildRunningUrl } from "./task-start-state";

type WorkspaceArtifact = ModelingWorkspaceView["artifacts"][number];

const escapeHtml = (value: string): string =>
  value.replace(/[&<>"']/g, character => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" } as Record<string, string>
  )[character] ?? character);

const icon = (name: string): string => `<i class="ph ph-${name}" aria-hidden="true"></i>`;

function showToast(message: string, duration = 2200): void {
  document.querySelector(".toast")?.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  window.setTimeout(() => node.remove(), duration);
}

function formatBytes(value: number | null): string {
  if (value === null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

/** 随任务上传的附件：项目级上传没有产出节点；解析失败/被清理的不算。 */
function uploadsOf(view: ModelingWorkspaceView): WorkspaceArtifact[] {
  return view.artifacts.filter(
    artifact => artifact.status === "READY" && artifact.producer_node === null,
  );
}

/** 顶栏回形针徽标：真实附件数；没有附件时隐藏按钮（display 需内联覆盖模板 flex）。 */
export function renderHeaderAttachments(root: HTMLElement, view: ModelingWorkspaceView): void {
  const button = root.querySelector<HTMLButtonElement>('[data-action="files"]');
  if (!button) return;
  const uploads = uploadsOf(view);
  button.dataset.workspaceControlled = "true";
  button.style.display = uploads.length ? "" : "none";
  if (!uploads.length) return;
  button.setAttribute("aria-label", `${t("查看附件")} (${uploads.length})`);
  button.innerHTML = `${icon("paperclip")} ${uploads.length}`;
}

function attachmentRowHtml(artifact: WorkspaceArtifact): string {
  const rawExt = artifact.name.includes(".") ? artifact.name.split(".").pop() ?? "" : "";
  const ext = rawExt.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 4);
  const badge = `<span class="attachment-file-icon ${ext}">${escapeHtml(ext || "doc")}</span>`;
  const copy = `<span><strong>${escapeHtml(artifact.name)}</strong><small>${formatBytes(artifact.size_bytes)}</small></span>`;
  if (artifact.download_url) {
    return `<a class="focused-attachment" href="${escapeHtml(artifact.download_url)}" download
      aria-label="${t("下载")} ${escapeHtml(artifact.name)}">${badge}${copy}${icon("download-simple")}</a>`;
  }
  return `<div class="focused-attachment">${badge}${copy}</div>`;
}

/** 附件弹窗：与演示弹窗同一 .modal 外观，内容换成当前任务的真实附件。 */
export function openAttachmentsDialog(view: ModelingWorkspaceView): void {
  document.querySelector(".task-attachments-dialog")?.remove();
  const uploads = uploadsOf(view);
  const body = uploads.length
    ? uploads.map(attachmentRowHtml).join("")
    : `<p class="dialog-note">${t("本任务没有上传附件")}</p>`;
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop task-attachments-dialog";
  backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
    <h2>${t("附件")}</h2>${body}
    <div class="modal-actions"><button type="button" data-modal-cancel>${t("关闭")}</button></div>
  </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  backdrop.querySelector("[data-modal-cancel]")?.addEventListener("click", close);
  backdrop.addEventListener("click", event => {
    if (event.target === backdrop) close();
  });
}

// ── 「更多操作」菜单：重命名 / 复制 / 归档 ─────────────────────────

function closeMenu(): void {
  document.querySelector(".menu.task-header-menu")?.remove();
}

function openMenu(anchor: HTMLElement, items: string[], onPick: (choice: string) => void): void {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "menu task-header-menu";
  menu.innerHTML = items
    .map(item => `<button type="button" data-menu-value="${escapeHtml(item)}">${t(item)}</button>`)
    .join("");
  document.body.appendChild(menu);
  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.min(rect.left, window.innerWidth - 190)}px`;
  menu.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - items.length * 38 - 16)}px`;

  const dispose = () => {
    closeMenu();
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKeydown, true);
    window.removeEventListener("resize", dispose);
  };
  const onOutside = (event: PointerEvent) => {
    if (!(event.target instanceof Node) || !menu.contains(event.target)) dispose();
  };
  const onKeydown = (event: KeyboardEvent) => {
    if (event.key === "Escape") dispose();
  };
  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onKeydown, true);
  window.addEventListener("resize", dispose);

  menu.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLElement>("[data-menu-value]");
    if (!button) return;
    dispose();
    onPick(button.dataset.menuValue ?? "");
  });
}

function openDialog(title: string, bodyHtml: string): HTMLElement {
  document.querySelector(".task-header-dialog")?.remove();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop task-header-dialog";
  backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h2>${escapeHtml(title)}</h2>${bodyHtml}</div>`;
  document.body.appendChild(backdrop);
  backdrop.addEventListener("click", event => {
    if (event.target === backdrop || (event.target as Element).closest("[data-dialog-cancel]")) {
      backdrop.remove();
    }
  });
  const input = backdrop.querySelector<HTMLInputElement>("input");
  input?.focus();
  input?.select();
  return backdrop;
}

function openRenameDialog(view: ModelingWorkspaceView, onRenamed: () => void): void {
  const backdrop = openDialog(
    t("重命名任务"),
    `
    <label>${t("任务名称")}</label><input name="name" maxlength="200" value="${escapeHtml(view.project_name)}">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("保存")}</button></div>`,
  );
  const submit = backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]");
  const commit = () => {
    if (!submit || submit.disabled) return;
    const name = backdrop.querySelector<HTMLInputElement>("[name=name]")?.value.trim() ?? "";
    const errorBox = backdrop.querySelector<HTMLElement>("[data-dialog-error]");
    const fail = (message: string) => {
      if (errorBox) {
        errorBox.textContent = message;
        errorBox.style.display = "block";
      }
    };
    if (!name) {
      fail(t("任务名称不能为空"));
      return;
    }
    const original = submit.textContent;
    submit.disabled = true;
    submit.textContent = t("处理中…");
    void (async () => {
      try {
        if (name !== view.project_name) {
          await modelingWorkspaceApi.updateProject(view.project_id, { name });
        }
        backdrop.remove();
        showToast(t("任务已重命名"));
        void hydrateRecentTasks();
        onRenamed();
      } catch (error) {
        fail(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
        submit.disabled = false;
        submit.textContent = original;
      }
    })();
  };
  submit?.addEventListener("click", commit);
  backdrop.querySelector<HTMLInputElement>("[name=name]")?.addEventListener("keydown", event => {
    if (event.key === "Enter") commit();
  });
}

async function copyTaskLink(view: ModelingWorkspaceView): Promise<void> {
  const url = new URL(
    buildRunningUrl(view.run_id, view.project_id),
    window.location.origin,
  ).toString();
  const copied = await copyTextToClipboard(url);
  showToast(copied ? t("任务链接已复制") : t("复制失败，请从地址栏复制链接"));
}

async function archiveTask(view: ModelingWorkspaceView): Promise<void> {
  try {
    await modelingWorkspaceApi.updateProject(view.project_id, { archived: true });
    showToast(t("任务已归档，不再出现在最近任务中"));
    void hydrateRecentTasks();
  } catch (error) {
    showToast(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
  }
}

/**
 * 顶栏「更多操作」：与演示菜单同款三项，但全部落到真实接口。
 * onRenamed 由工作台控制器传入（重命名成功后刷新快照，让各处项目名同步）。
 */
export function openTaskHeaderMenu(
  anchor: HTMLElement,
  view: ModelingWorkspaceView,
  onRenamed: () => void,
): void {
  openMenu(anchor, ["重命名", "复制", "归档"], choice => {
    if (choice === "重命名") openRenameDialog(view, onRenamed);
    if (choice === "复制") void copyTaskLink(view);
    if (choice === "归档") void archiveTask(view);
  });
}
