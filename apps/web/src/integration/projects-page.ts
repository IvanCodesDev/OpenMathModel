/**
 * 「我的项目」页：全量项目管理台（切片②：服务端聚合分页）。
 *
 * 与侧栏「最近任务」分工：侧栏是最近 20 条的快速切换器，本页是全量项目
 * 治理入口——搜索、状态筛选、分页、新建、重命名、归档与删除。
 *
 * 数据 = GET /v1/projects?include=stats：服务端一次聚合出每个项目的最新
 * 运行投影（阶段、更新时间、行跳转取自它）与产物计数；Tabs（archived/state
 * 参数）、搜索（q 参数）与分页（limit/offset/total）全部在服务端完成。
 * 实验 / 论文计数列仍显示「—」，等各页正文契约（Phase 1）落地后回填。
 *
 * 未登录或请求失败时保留模板演示表格与其原有交互，不打扰当前页面。
 */

import type { Project } from "@openmathmodel/contracts";
import { t } from "../i18n/locale";
import { clearConversationLog } from "../tasks/conversation-log";
import { forgetLastTask } from "../tasks/last-task-record";
import { modelingWorkspaceApi } from "./modeling-workspace-api";
import { hydrateRecentTasks } from "./recent-tasks";
import { buildRunningUrl } from "./task-start-state";

type ProjectStats = NonNullable<Project["stats"]>;
type LatestRun = NonNullable<ProjectStats["latest_run"]>;

interface ProjectItem {
  project: Project;
  /** 该项目最新一次运行的轻量投影；还没有运行的项目为 null。 */
  run: LatestRun | null;
  /** 产物计数；服务端未返回 stats 时为 null（计数列显示「—」）。 */
  artifactCount: number | null;
}

type ProjectsTab = "all" | "active" | "done" | "archived";

const PAGE_SIZES = [10, 20, 50];
/** 页码窗口之外用省略号收敛，避免页多时按钮铺满一行。 */
const MAX_PLAIN_PAGES = 7;

const TAB_BY_LABEL: Record<string, ProjectsTab> = {
  全部: "all",
  进行中: "active",
  已完成: "done",
  已归档: "archived",
};

/** current_node → 阶段名；契约要求消费方容忍未知节点名（兜底「进行中」）。 */
const NODE_STAGE_LABELS: Record<string, string> = {
  CREATED: "已创建",
  PROBLEM_ANALYSIS: "问题分析",
  DATA_PREPARATION: "数据处理",
  MODEL_PLANNING: "建模方案",
  EXPERIMENTING: "实验验证",
  VALIDATING: "结果验证",
  PAPER_WRITING: "论文撰写",
  COMPLETED: "已完成",
};

let currentTab: ProjectsTab = "all";
let searchQuery = "";
let currentPage = 1;
let pageSize = 20;
let items: ProjectItem[] = [];
let totalCount = 0;
let fetchSeq = 0;

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

function truncate(value: string, max: number): string {
  const characters = Array.from(value.replace(/\s+/g, " ").trim());
  return characters.length > max ? `${characters.slice(0, max).join("")}…` : characters.join("");
}

/** 与模板演示数据同格式：2026-08-20 14:32。 */
function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (part: number): string => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function stageLabel(run: LatestRun | null): string {
  if (!run) return "未开始";
  if (run.status === "COMPLETED") return "已完成";
  if (run.status === "FAILED") return "已失败";
  if (run.status === "CANCELLED") return "已取消";
  return NODE_STAGE_LABELS[run.current_node] ?? "进行中";
}

// ── 数据 ─────────────────────────────────────────────────────────

/** Tabs → 列表查询参数：归档走 archived，进行中/已完成走 state 归桶。 */
function tabQuery(): { archived?: boolean; state?: "active" | "done" } {
  if (currentTab === "archived") return { archived: true };
  if (currentTab === "active") return { state: "active" };
  if (currentTab === "done") return { state: "done" };
  return {};
}

/** null = 未登录或网络失败（保留模板演示表格）。 */
async function fetchPage(): Promise<{ items: ProjectItem[]; total: number } | null> {
  try {
    const page = await modelingWorkspaceApi.listProjects({
      ...tabQuery(),
      include: "stats",
      q: searchQuery || undefined,
      limit: pageSize,
      offset: (currentPage - 1) * pageSize,
    });
    return {
      items: page.items.map(project => ({
        project,
        run: project.stats?.latest_run ?? null,
        artifactCount: project.stats?.artifact_count ?? null,
      })),
      total: page.total,
    };
  } catch {
    return null;
  }
}

function itemById(projectId: string): ProjectItem | null {
  return items.find(item => item.project.id === projectId) ?? null;
}

async function refetch(): Promise<void> {
  const seq = ++fetchSeq;
  let fetched = await fetchPage();
  if (seq !== fetchSeq) return;
  // 删除或筛选后当前页可能越界：退回最后一页再拉一次
  if (fetched !== null && fetched.items.length === 0 && fetched.total > 0 && currentPage > 1) {
    currentPage = Math.max(1, Math.ceil(fetched.total / pageSize));
    fetched = await fetchPage();
    if (seq !== fetchSeq) return;
  }
  if (fetched === null) {
    showToast(t("项目列表加载失败，请稍后再试"));
    return;
  }
  items = fetched.items;
  totalCount = fetched.total;
  render();
}

/** 项目维护会同时改变侧栏「最近任务」的数据源，两处一起刷新。 */
async function afterMutation(): Promise<void> {
  void hydrateRecentTasks();
  await refetch();
}

// ── 渲染 ─────────────────────────────────────────────────────────

function rowHtml(item: ProjectItem): string {
  const stage = stageLabel(item.run);
  const updated = formatDateTime(item.run?.updated_at ?? item.project.updated_at);
  const subtitleSource = item.run?.goal ?? item.project.description ?? "";
  const subtitle = subtitleSource ? escapeHtml(truncate(subtitleSource, 60)) : t("尚未发起运行");
  const runAttribute = item.run ? ` data-run-id="${escapeHtml(item.run.id)}"` : "";
  // 文件列 = 服务端聚合的产物计数；实验/论文列等 Phase 1 正文契约后回填。
  const files = item.artifactCount === null ? "—" : String(item.artifactCount);
  // data-stage 保持中文原值：它是 CSS 配色选择器，不参与界面语言切换。
  return `<tr data-project-row data-project-id="${escapeHtml(item.project.id)}"${runAttribute} tabindex="0">
    <td class="project-name"><div class="table-doc">${icon("file-text")}<div><strong>${escapeHtml(item.project.name)}</strong><span>${subtitle}</span></div></div></td>
    <td><span class="stage-pill" data-stage="${escapeHtml(stage)}">${t(stage)}</span></td>
    <td>${updated}</td>
    <td>${files}</td><td>—</td><td>—</td>
    <td><button type="button" class="row-menu-button" data-project-menu aria-label="${t("项目选项")}" title="${t("项目选项")}">${icon("dots-three")}</button></td>
  </tr>`;
}

function emptyRowHtml(): string {
  const message = searchQuery
    ? t("没有匹配的项目")
    : currentTab === "all"
      ? t("还没有项目，去首页发起第一个建模任务")
      : t("该筛选下暂无项目");
  return `<tr class="projects-empty-row"><td colspan="7">${message}</td></tr>`;
}

function pageWindow(pageCount: number): Array<number | "gap"> {
  if (pageCount <= MAX_PLAIN_PAGES) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }
  const picked = [...new Set([1, currentPage - 1, currentPage, currentPage + 1, pageCount])]
    .filter(page => page >= 1 && page <= pageCount)
    .sort((left, right) => left - right);
  const slots: Array<number | "gap"> = [];
  let previous = 0;
  for (const page of picked) {
    if (previous > 0 && page - previous > 1) slots.push("gap");
    slots.push(page);
    previous = page;
  }
  return slots;
}

function footerHtml(total: number, pageCount: number): string {
  const numbers = pageWindow(pageCount).map(slot => slot === "gap"
    ? '<span class="page-gap">…</span>'
    : `<button type="button" class="page-button ${slot === currentPage ? "active" : ""}" data-projects-page="${slot}" aria-current="${slot === currentPage ? "page" : "false"}">${slot}</button>`).join("");
  const options = PAGE_SIZES.map(size =>
    `<button type="button" role="option" data-select-option="${size}" aria-selected="${size === pageSize}"><span>${size} ${t("条/页")}</span>${icon("check")}</button>`).join("");
  return `<span>${t("共")} ${total} ${t("项")}</span><div class="pagination">
    <button type="button" class="page-button" data-projects-page="prev" ${currentPage <= 1 ? "disabled" : ""} aria-label="${t("上一页")}">‹</button>
    ${numbers}
    <button type="button" class="page-button" data-projects-page="next" ${currentPage >= pageCount ? "disabled" : ""} aria-label="${t("下一页")}">›</button>
    <div class="settings-custom-select page-size-select" data-page-size-select data-select-menu>
      <button type="button" class="settings-select-trigger" data-select-trigger aria-haspopup="listbox" aria-expanded="false" aria-label="${t("每页条数")}"><span data-select-label>${pageSize} ${t("条/页")}</span>${icon("caret-down")}</button>
      <div class="settings-select-menu" role="listbox" aria-label="${t("每页条数")}">${options}</div>
    </div>
  </div>`;
}

function render(): void {
  const tbody = document.querySelector<HTMLElement>(".project-table tbody");
  const footer = document.querySelector<HTMLElement>(".project-footer");
  if (!tbody || !footer) return;
  const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  tbody.innerHTML = items.length > 0 ? items.map(rowHtml).join("") : emptyRowHtml();
  footer.innerHTML = footerHtml(totalCount, pageCount);
}

// ── 行为 ─────────────────────────────────────────────────────────

function openItem(item: ProjectItem): void {
  if (item.run) {
    window.location.href = buildRunningUrl(item.run.id, item.project.id);
    return;
  }
  showToast(t("该项目还没有运行记录"));
}

// ── 浮动菜单与对话框（视觉复用侧栏「最近任务」的样式类） ────────

function closeMenu(): void {
  document.querySelector(".recent-menu")?.remove();
}

function presentMenu(anchor: HTMLElement, menu: HTMLElement): () => void {
  document.body.appendChild(menu);

  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
  const top = rect.bottom + height + 6 > window.innerHeight ? rect.top - height - 6 : rect.bottom + 6;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;

  const dispose = () => {
    menu.remove();
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKeydown, true);
    document.removeEventListener("scroll", dispose, true);
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
  document.addEventListener("scroll", dispose, true);
  window.addEventListener("resize", dispose);
  return dispose;
}

function openMenu(anchor: HTMLElement, item: ProjectItem): void {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "recent-menu";
  const archiveEntry = currentTab === "archived"
    ? `<button type="button" data-menu-action="unarchive">${icon("arrow-counter-clockwise")}<span>${t("取消归档")}</span></button>`
    : `<button type="button" data-menu-action="archive">${icon("archive")}<span>${t("归档")}</span></button>`;
  menu.innerHTML = `
    <button type="button" data-menu-action="open">${icon("arrow-square-out")}<span>${t("打开")}</span></button>
    <button type="button" data-menu-action="history">${icon("clock-counter-clockwise")}<span>${t("运行历史")}</span></button>
    <button type="button" data-menu-action="rename">${icon("pencil-simple")}<span>${t("重命名")}</span></button>
    ${archiveEntry}
    <button type="button" class="danger" data-menu-action="delete">${icon("trash")}<span>${t("删除")}</span></button>`;
  const dispose = presentMenu(anchor, menu);

  menu.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLElement>("[data-menu-action]");
    if (!button) return;
    const action = button.dataset.menuAction;
    dispose();
    if (action === "open") openItem(item);
    if (action === "history") void openRunHistoryDialog(item);
    if (action === "rename") openRenameDialog(item);
    if (action === "archive") void archiveItem(item);
    if (action === "unarchive") void unarchiveItem(item);
    if (action === "delete") openDeleteDialog(item);
  });
}

function openDialog(title: string, bodyHtml: string): HTMLElement {
  document.querySelector(".recent-dialog")?.remove();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop recent-dialog";
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

async function runDialogAction(
  backdrop: HTMLElement,
  button: HTMLButtonElement,
  work: () => Promise<void>,
): Promise<void> {
  const errorBox = backdrop.querySelector<HTMLElement>("[data-dialog-error]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = t("处理中…");
  try {
    await work();
    backdrop.remove();
  } catch (error) {
    if (errorBox) {
      errorBox.textContent = error instanceof Error ? error.message : t("操作失败，请稍后再试");
      errorBox.style.display = "block";
    }
    button.disabled = false;
    button.textContent = original;
  }
}

function openCreateDialog(): void {
  const backdrop = openDialog(
    t("新建项目"),
    `
    <label>${t("项目名称")}</label><input name="name" maxlength="200" placeholder="${t("输入项目名称")}">
    <label>${t("项目说明")}</label><textarea name="description" maxlength="2000" placeholder="${t("简单描述建模目标")}"></textarea>
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("创建")}</button></div>`,
  );
  const submit = backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]");
  const commit = () => {
    if (!submit) return;
    void runDialogAction(backdrop, submit, async () => {
      const name = backdrop.querySelector<HTMLInputElement>("[name=name]")?.value.trim() ?? "";
      if (!name) throw new Error(t("项目名称不能为空"));
      const description = backdrop.querySelector<HTMLTextAreaElement>("[name=description]")?.value.trim() ?? "";
      await modelingWorkspaceApi.createProject({ name, description: description || null });
      showToast(t("项目已创建"));
      await afterMutation();
    });
  };
  submit?.addEventListener("click", commit);
  backdrop.querySelector<HTMLInputElement>("[name=name]")?.addEventListener("keydown", event => {
    if (event.key === "Enter") commit();
  });
}

function openRenameDialog(item: ProjectItem): void {
  const backdrop = openDialog(
    t("重命名项目"),
    `
    <label>${t("项目名称")}</label><input name="name" maxlength="200" value="${escapeHtml(item.project.name)}">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("保存")}</button></div>`,
  );
  const submit = backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]");
  const commit = () => {
    if (!submit) return;
    void runDialogAction(backdrop, submit, async () => {
      const name = backdrop.querySelector<HTMLInputElement>("[name=name]")?.value.trim() ?? "";
      if (!name) throw new Error(t("项目名称不能为空"));
      if (name !== item.project.name) await modelingWorkspaceApi.updateProject(item.project.id, { name });
      showToast(t("项目已重命名"));
      await afterMutation();
    });
  };
  submit?.addEventListener("click", commit);
  backdrop.querySelector<HTMLInputElement>("[name=name]")?.addEventListener("keydown", event => {
    if (event.key === "Enter") commit();
  });
}

/** 单次拉取的运行历史上限；超过时提示只显示最近 50 次（后端 le=200，够用）。 */
const RUN_HISTORY_LIMIT = 50;

/** 「运行历史」：一题多次运行的入口——历次运行按时间倒序，点击带 run_id 跳执行页。 */
async function openRunHistoryDialog(item: ProjectItem): Promise<void> {
  const backdrop = openDialog(
    t("运行历史"),
    `
    <p class="dialog-note">${escapeHtml(truncate(item.project.name, 40))}</p>
    <div class="run-history-list" data-run-history><div class="run-history-hint">${t("加载中…")}</div></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("关闭")}</button></div>`,
  );
  const host = backdrop.querySelector<HTMLElement>("[data-run-history]");
  if (!host) return;
  try {
    const page = await modelingWorkspaceApi.listTaskRuns(RUN_HISTORY_LIMIT, item.project.id);
    if (!backdrop.isConnected) return; // 等待期间对话框已被关闭
    if (page.items.length === 0) {
      host.innerHTML = `<div class="run-history-hint">${t("该项目还没有运行记录")}</div>`;
      return;
    }
    const rows = page.items.map(run => {
      const stage = stageLabel(run);
      return `<button type="button" class="run-history-row" data-history-run="${escapeHtml(run.id)}">
        <span class="stage-pill" data-stage="${escapeHtml(stage)}">${t(stage)}</span>
        <span class="run-history-main"><strong>${escapeHtml(truncate(run.goal, 42))}</strong><span>${formatDateTime(run.created_at)}</span></span>
        ${icon("arrow-square-out")}
      </button>`;
    }).join("");
    const overflow = page.total > page.items.length
      ? `<div class="run-history-hint">${t("仅显示最近 50 次运行")}</div>`
      : "";
    host.innerHTML = rows + overflow;
    host.addEventListener("click", event => {
      const row = (event.target as Element).closest<HTMLElement>("[data-history-run]");
      if (!row) return;
      window.location.href = buildRunningUrl(row.dataset.historyRun ?? "", item.project.id);
    });
  } catch {
    if (!backdrop.isConnected) return;
    host.innerHTML = `<div class="run-history-hint">${t("运行历史加载失败，请稍后再试")}</div>`;
  }
}

async function archiveItem(item: ProjectItem): Promise<void> {
  try {
    await modelingWorkspaceApi.updateProject(item.project.id, { archived: true });
    showToast(t("项目已归档，可在「已归档」筛选中找回"));
    await afterMutation();
  } catch (error) {
    showToast(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
  }
}

async function unarchiveItem(item: ProjectItem): Promise<void> {
  try {
    await modelingWorkspaceApi.updateProject(item.project.id, { archived: false });
    showToast(t("已取消归档，项目回到列表"));
    await afterMutation();
  } catch (error) {
    showToast(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
  }
}

function openDeleteDialog(item: ProjectItem): void {
  const backdrop = openDialog(
    t("删除项目"),
    `
    <p class="dialog-note">${t("删除后，该项目的全部运行、对话、执行步骤、审批记录和生成文件将全部清除，且无法恢复。仅想隐藏可改用「归档」。")}</p>
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("永久删除")}</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runDialogAction(backdrop, this, async () => {
      await modelingWorkspaceApi.deleteProject(item.project.id);
      if (item.run) {
        // 服务端已级联删除，本机的任务记录与对话日志一并清掉。
        forgetLastTask(item.run.id);
        clearConversationLog(item.run.id);
      }
      showToast(t("项目已删除"));
      await afterMutation();
    });
  });
}

// ── 接管模板交互 ─────────────────────────────────────────────────

function ensureTakeover(): void {
  const table = document.querySelector<HTMLElement>(".project-table");
  if (!table || table.dataset.projectsBound) return;
  table.dataset.projectsBound = "1";

  // 模板的演示筛选监听挂在按钮上：整体克隆替换即可摘除，DOM 槽位不变。
  const tabsHost = document.querySelector<HTMLElement>(".project-tabs");
  if (tabsHost) {
    const cloned = tabsHost.cloneNode(true) as HTMLElement;
    tabsHost.replaceWith(cloned);
    cloned.addEventListener("click", event => {
      const button = (event.target as Element).closest<HTMLElement>("[data-project-tab]");
      if (!button) return;
      cloned.querySelectorAll("[data-project-tab]").forEach(entry => entry.classList.toggle("active", entry === button));
      const next = TAB_BY_LABEL[button.dataset.projectTab ?? ""] ?? "all";
      if (next === currentTab) return;
      // Tabs 是服务端筛选（archived/state 参数），切换一律重拉。
      currentTab = next;
      currentPage = 1;
      void refetch();
    });
  }

  // 演示搜索只过滤当页文本：同样克隆替换，换成服务端全量搜索（q 参数）。
  // 防抖 250ms：中文输入与连续敲击不必每个字符打一次接口。
  const search = document.querySelector<HTMLInputElement>("[data-table-search]");
  if (search) {
    const cloned = search.cloneNode(true) as HTMLInputElement;
    search.replaceWith(cloned);
    let searchTimer = 0;
    cloned.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        const next = cloned.value.trim();
        if (next === searchQuery) return;
        searchQuery = next;
        currentPage = 1;
        void refetch();
      }, 250);
    });
  }

  // 「新建项目」的演示对话框由 document 级路由处理：在按钮上截获并阻断冒泡接管。
  document.querySelector<HTMLElement>('[data-action="new-project"]')?.addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    openCreateDialog();
  });

  const tbody = table.querySelector<HTMLElement>("tbody");
  tbody?.addEventListener("click", event => {
    const target = event.target as Element;
    const row = target.closest<HTMLElement>("[data-project-row]");
    if (!row) return;
    const item = itemById(row.dataset.projectId ?? "");
    if (!item) return;
    const menuButton = target.closest<HTMLElement>("[data-project-menu]");
    if (menuButton) {
      event.preventDefault();
      event.stopPropagation();
      openMenu(menuButton, item);
      return;
    }
    openItem(item);
  });
  tbody?.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    const row = (event.target as Element).closest<HTMLElement>("[data-project-row]");
    if (!row) return;
    event.preventDefault();
    const item = itemById(row.dataset.projectId ?? "");
    if (item) openItem(item);
  });

  // 分页与每页条数：页脚整体由 render() 重建，监听挂容器做委托。
  const footer = document.querySelector<HTMLElement>(".project-footer");
  footer?.addEventListener("click", event => {
    const target = event.target as Element;
    const pageButton = target.closest<HTMLButtonElement>("[data-projects-page]");
    if (pageButton && !pageButton.disabled) {
      const requested = pageButton.dataset.projectsPage;
      if (requested === "prev") currentPage -= 1;
      else if (requested === "next") currentPage += 1;
      else currentPage = Number(requested);
      void refetch();
      return;
    }
    const wrapper = target.closest<HTMLElement>("[data-page-size-select]");
    if (!wrapper) return;
    const option = target.closest<HTMLElement>("[data-select-option]");
    if (option) {
      const next = Number(option.dataset.selectOption);
      if (PAGE_SIZES.includes(next) && next !== pageSize) {
        pageSize = next;
        currentPage = 1;
        void refetch();
      } else {
        wrapper.classList.remove("open");
        wrapper.querySelector("[data-select-trigger]")?.setAttribute("aria-expanded", "false");
      }
      return;
    }
    if (target.closest("[data-select-trigger]")) {
      const willOpen = !wrapper.classList.contains("open");
      wrapper.classList.toggle("open", willOpen);
      wrapper.querySelector("[data-select-trigger]")?.setAttribute("aria-expanded", String(willOpen));
    }
  });
}

// ── 挂载 ─────────────────────────────────────────────────────────

/** 进入「我的项目」页后调用：把演示表格换成真实项目清单；未登录保持演示。 */
export async function hydrateProjectsPage(): Promise<void> {
  if (!document.querySelector(".project-table")) return;
  const seq = ++fetchSeq;
  const fetched = await fetchPage();
  // 快速切换或失败时不打扰当前页面（未登录/网络失败 = 保留演示表格）
  if (seq !== fetchSeq || fetched === null) return;
  items = fetched.items;
  totalCount = fetched.total;
  ensureTakeover();
  render();
}
