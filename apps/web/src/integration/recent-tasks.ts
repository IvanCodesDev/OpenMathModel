/**
 * 侧栏「最近任务」：建模任务 + 首页普通对话，各自可重命名 / 归档 / 删除。
 *
 * 两个数据源合并后按最近有动静排序：
 * - **建模任务**来自控制面两张列表（运行按创建时间倒序 + 项目清单），客户端
 *   联结出「项目 → 最新一次运行」；操作全部走真实接口：重命名 PATCH 项目名
 *   （与工作台顶栏同源），归档从默认列表隐藏（可经 archived=true 找回），
 *   删除为服务端级联清除且不可恢复。
 * - **普通对话**（接待判定分流到首页对话的那条链）不建项目、不起运行，服务端
 *   没有可列的记录，改由 tasks/chat-sessions 的本机目录提供；重命名 / 归档 /
 *   删除都落在本机，删除连带清掉对话正文。
 *
 * 未登录或任务请求失败时保留页面模板的演示条目，不打扰当前页面。
 * 视觉复用现有 .recent-link 行与模态对话框样式。
 */

import { fetchMe } from "../auth/api";
import { t } from "../i18n/locale";
import {
  deleteChatSession,
  listChatSessions,
  renameChatSession,
  setChatSessionArchived,
} from "../tasks/chat-sessions";
import { clearConversationLog } from "../tasks/conversation-log";
import { forgetLastTask } from "../tasks/last-task-record";
import { modelingWorkspaceApi } from "./modeling-workspace-api";
import { buildChatUrl, buildRunningUrl } from "./task-start-state";

/** 侧栏一行；chat 条目没有项目与运行，id 是本机会话 id、runId 恒为空。 */
interface RecentItem {
  kind: "task" | "chat";
  id: string;
  runId: string;
  name: string;
  status: string;
  /** 合并排序用的时间戳（毫秒）。 */
  time: number;
}

/** 展示条数上限：列表区在侧栏内自行滚动，不再受模板三条的限制。 */
const MAX_ITEMS = 20;

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

/** 行首图标：对话固定气泡，任务按运行状态区分（进行中额外亮 unread-dot）。 */
function itemIcon(item: RecentItem): string {
  if (item.kind === "chat") return "chat-circle";
  if (item.status === "COMPLETED") return "check";
  if (item.status === "FAILED") return "warning";
  if (item.status === "CANCELLED") return "x";
  return "circle-half";
}

const ACTIVE_STATUSES = new Set(["QUEUED", "RUNNING", "WAITING_APPROVAL"]);

// ── 筛选（搜索框旁的筛选按钮，接管模板的 data-action="sidebar-filter"） ──

const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

type RecentFilter = "all" | "active" | "done" | "archived";

const FILTER_OPTIONS: Array<{ id: RecentFilter; label: string }> = [
  { id: "all", label: "全部任务" },
  { id: "active", label: "进行中" },
  { id: "done", label: "已完成" },
  { id: "archived", label: "已归档" },
];

let currentFilter: RecentFilter = "all";

/** 按项目最新一次运行的状态归桶：进行中 = 未到终态（含暂停）。 */
function matchesFilter(status: string): boolean {
  if (currentFilter === "active") return !TERMINAL_STATUSES.has(status);
  if (currentFilter === "done") return status === "COMPLETED";
  return true; // all / archived 不按状态过滤；archived 由项目清单决定
}

// ── 数据 ─────────────────────────────────────────────────────────

/** null = 未登录或网络失败（保留模板演示条目）；[] = 已登录但还没有任务。 */
async function fetchTaskItems(): Promise<RecentItem[] | null> {
  try {
    const [runs, projects] = await Promise.all([
      modelingWorkspaceApi.listTaskRuns(100),
      modelingWorkspaceApi.listProjects({ archived: currentFilter === "archived", limit: 100 }),
    ]);
    const names = new Map(projects.items.map(project => [project.id, project.name]));
    const items: RecentItem[] = [];
    const seen = new Set<string>();
    for (const run of runs.items) {
      const name = names.get(run.project_id);
      // 不在当前项目清单（默认=未归档，筛已归档=归档）里的运行自然被跳过
      if (name === undefined || seen.has(run.project_id)) continue;
      seen.add(run.project_id);
      if (!matchesFilter(run.status)) continue;
      items.push({
        kind: "task",
        id: run.project_id,
        runId: run.id,
        name,
        status: run.status,
        // 与对话条目同口径按「最近有动静」排序：执行中的任务因此留在最前
        time: Date.parse(run.updated_at || run.created_at) || 0,
      });
      if (items.length >= MAX_ITEMS) break;
    }
    return items;
  } catch {
    return null;
  }
}

/**
 * 本机的首页对话条目。「进行中 / 已完成」两个桶按运行状态归类，对话没有
 * 运行也就没有状态，只在「全部」与「已归档」里出现。
 */
async function fetchChatItems(): Promise<RecentItem[]> {
  if (currentFilter === "active" || currentFilter === "done") return [];
  const me = await fetchMe().catch(() => null);
  if (!me) return [];
  return listChatSessions(me.user.id, { archived: currentFilter === "archived" }).map(session => ({
    kind: "chat" as const,
    id: session.id,
    runId: "",
    name: session.title,
    status: "CHAT",
    time: session.updated_at,
  }));
}

// ── 渲染 ─────────────────────────────────────────────────────────

function itemUrl(item: RecentItem): string {
  return item.kind === "chat" ? buildChatUrl(item.id) : buildRunningUrl(item.runId, item.id);
}

function renderItems(host: HTMLElement, items: RecentItem[]): void {
  const rows = items.map(item => {
    const dot = item.kind === "task" && ACTIVE_STATUSES.has(item.status) ? '<b class="unread-dot"></b>' : "";
    const options = t(item.kind === "chat" ? "对话选项" : "任务选项");
    return `<a class="recent-link has-action" href="${escapeHtml(itemUrl(item))}"
      data-recent-item data-kind="${item.kind}" data-item-id="${escapeHtml(item.id)}" data-run-id="${escapeHtml(item.runId)}" data-name="${escapeHtml(item.name)}">
      ${icon(itemIcon(item))}<span>${escapeHtml(item.name)}</span>${dot}
      <button type="button" class="recent-action" data-recent-action aria-label="${escapeHtml(options)}" title="${escapeHtml(options)}">${icon("dots-three")}</button>
    </a>`;
  });
  const filterLabel = FILTER_OPTIONS.find(option => option.id === currentFilter)?.label ?? "";
  const title = currentFilter === "all"
    ? t("最近任务")
    : `${t("最近任务")} · ${t(filterLabel)}`;
  const empty = currentFilter === "all" ? t("暂无最近任务") : t("该筛选下暂无任务");
  const body = rows.length > 0 ? rows.join("") : `<div class="recent-empty">${empty}</div>`;
  host.innerHTML = `<div class="recent-title">${title}</div>${body}`;
}

// ── 浮动菜单 ─────────────────────────────────────────────────────

function closeMenu(): void {
  document.querySelector(".recent-menu")?.remove();
}

/** 浮动菜单通用壳：定位到锚点下方（放不下改上方），外点/Esc/滚动/缩放收起。 */
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
  // 列表区可滚动后，菜单锚点会随滚动位移：滚动即收起，避免菜单悬空。
  document.addEventListener("scroll", dispose, true);
  window.addEventListener("resize", dispose);
  return dispose;
}

function openMenu(anchor: HTMLElement, item: RecentItem): void {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "recent-menu";
  // 看已归档时，归档位换成取消归档（恢复回默认列表）
  const archiveEntry = currentFilter === "archived"
    ? `<button type="button" data-menu-action="unarchive">${icon("arrow-counter-clockwise")}<span>${t("取消归档")}</span></button>`
    : `<button type="button" data-menu-action="archive">${icon("archive")}<span>${t("归档")}</span></button>`;
  menu.innerHTML = `
    <button type="button" data-menu-action="rename">${icon("pencil-simple")}<span>${t("重命名")}</span></button>
    ${archiveEntry}
    <button type="button" class="danger" data-menu-action="delete">${icon("trash")}<span>${t("删除")}</span></button>`;
  const dispose = presentMenu(anchor, menu);

  menu.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLElement>("[data-menu-action]");
    if (!button) return;
    const action = button.dataset.menuAction;
    dispose();
    if (action === "rename") openRenameDialog(item);
    if (action === "archive") void archiveItem(item);
    if (action === "unarchive") void unarchiveItem(item);
    if (action === "delete") openDeleteDialog(item);
  });
}

// ── 对话框（复用账户面板的 modal 样式） ──────────────────────────

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

/** 提交动作的忙态与错误呈现（对话框内联，不打断流程）。 */
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

function openRenameDialog(item: RecentItem): void {
  const chat = item.kind === "chat";
  const backdrop = openDialog(
    t(chat ? "重命名对话" : "重命名任务"),
    `
    <label>${t(chat ? "对话名称" : "任务名称")}</label><input name="name" maxlength="200" value="${escapeHtml(item.name)}">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("保存")}</button></div>`,
  );
  const submit = backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]");
  const commit = () => {
    if (!submit) return;
    void runDialogAction(backdrop, submit, async () => {
      const name = backdrop.querySelector<HTMLInputElement>("[name=name]")?.value.trim() ?? "";
      if (!name) throw new Error(t(chat ? "对话名称不能为空" : "任务名称不能为空"));
      if (name !== item.name) {
        if (chat) renameChatSession(item.id, name);
        else await modelingWorkspaceApi.updateProject(item.id, { name });
      }
      showToast(t(chat ? "对话已重命名" : "任务已重命名"));
      void hydrateRecentTasks();
    });
  };
  submit?.addEventListener("click", commit);
  backdrop.querySelector<HTMLInputElement>("[name=name]")?.addEventListener("keydown", event => {
    if (event.key === "Enter") commit();
  });
}

async function archiveItem(item: RecentItem): Promise<void> {
  try {
    if (item.kind === "chat") setChatSessionArchived(item.id, true);
    else await modelingWorkspaceApi.updateProject(item.id, { archived: true });
    showToast(t(item.kind === "chat"
      ? "对话已归档，不再出现在最近任务中"
      : "任务已归档，不再出现在最近任务中"));
    void hydrateRecentTasks();
  } catch (error) {
    showToast(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
  }
}

async function unarchiveItem(item: RecentItem): Promise<void> {
  try {
    if (item.kind === "chat") setChatSessionArchived(item.id, false);
    else await modelingWorkspaceApi.updateProject(item.id, { archived: false });
    showToast(t(item.kind === "chat" ? "已取消归档，对话回到最近任务" : "已取消归档，任务回到最近任务"));
    void hydrateRecentTasks();
  } catch (error) {
    showToast(error instanceof Error ? error.message : t("操作失败，请稍后再试"));
  }
}

const ACTIVE_RUN_KEY = "openmathmodel.activeRunId";
const ACTIVE_PROJECT_KEY = "openmathmodel.activeProjectId";

/** 用户当前停留的页面是否正是这一条：URL 身份命中，或工作台页面经
 *  sessionStorage 恢复的身份命中（§5.1 允许 URL 不带参数）。 */
function viewingItem(item: RecentItem): boolean {
  const params = new URL(window.location.href).searchParams;
  if (item.kind === "chat") return params.get("chat") === item.id;
  if (params.get("run_id") === item.runId || params.get("project_id") === item.id) return true;
  if (params.get("demo") === "1" || params.has("run_id")) return false;
  if (!document.querySelector("[data-modeling-shell]")) return false;
  try {
    return sessionStorage.getItem(ACTIVE_RUN_KEY) === item.runId;
  } catch {
    return false;
  }
}

function openDeleteDialog(item: RecentItem): void {
  const chat = item.kind === "chat";
  const backdrop = openDialog(
    t(chat ? "删除对话" : "删除任务"),
    `
    <p class="dialog-note">${t(chat
      ? "删除后，这段对话的全部消息将从本机清除，且无法恢复。仅想隐藏可改用「归档」。"
      : "删除后，该任务的对话、执行步骤、审批记录和生成文件将全部清除，且无法恢复。仅想隐藏可改用「归档」。")}</p>
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>${t("取消")}</button><button type="button" class="primary" data-dialog-submit>${t("永久删除")}</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runDialogAction(backdrop, this, async () => {
      // 是否正在浏览被删条目要在清身份之前判定（判定会读 sessionStorage）。
      const viewing = viewingItem(item);
      if (chat) {
        // 对话只存在于本机：目录条目与正文记录一起清。
        deleteChatSession(item.id);
      } else {
        await modelingWorkspaceApi.deleteProject(item.id);
        forgetLastTask(item.runId);
        // 服务端已级联删除，本机对话记录一并清掉，不留孤儿数据。
        clearConversationLog(item.runId);
        // 活动身份指向被删任务时一并清除：其余流程页不再带着死链身份自动恢复。
        try {
          if (sessionStorage.getItem(ACTIVE_RUN_KEY) === item.runId) {
            sessionStorage.removeItem(ACTIVE_RUN_KEY);
            sessionStorage.removeItem(ACTIVE_PROJECT_KEY);
          }
        } catch {
          // 会话存储不可用时也没有身份可清
        }
      }
      if (viewing) {
        // 正在看的就是被删条目：原地停留只会继续展示已不存在的内容，直接回
        // 初始问答页。用 replace 不留历史死链，回退不会又跳回已删除的页面。
        window.location.replace("/");
        return;
      }
      showToast(t(chat ? "对话已删除" : "任务已删除"));
      void hydrateRecentTasks();
    });
  });
}

// ── 筛选按钮与筛选菜单 ───────────────────────────────────────────

/** 按钮选中态与提示随当前筛选同步；侧栏每次整体重渲染后都要重放。 */
function syncFilterButtons(): void {
  const label = FILTER_OPTIONS.find(option => option.id === currentFilter)?.label ?? "";
  document.querySelectorAll<HTMLElement>('[data-action="sidebar-filter"]').forEach(button => {
    const filtering = currentFilter !== "all";
    button.classList.toggle("is-filtering", filtering);
    button.title = filtering ? `${t("筛选")}：${t(label)}` : t("筛选");
    button.setAttribute("aria-pressed", String(filtering));
  });
}

function openFilterMenu(anchor: HTMLElement): void {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "recent-menu recent-filter-menu";
  menu.innerHTML = FILTER_OPTIONS.map(option => `
    <button type="button" data-filter-id="${option.id}" class="${option.id === currentFilter ? "is-current" : ""}">
      <span>${t(option.label)}</span>${option.id === currentFilter ? icon("check") : ""}
    </button>`).join("");
  const dispose = presentMenu(anchor, menu);

  menu.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLElement>("[data-filter-id]");
    if (!button) return;
    dispose();
    const next = button.dataset.filterId as RecentFilter;
    if (next === currentFilter) return;
    currentFilter = next;
    syncFilterButtons();
    void hydrateRecentTasks();
  });
}

let filterBound = false;

/** 接管模板的 data-action="sidebar-filter"（旧实现是无回调的演示菜单）。 */
function bindFilterButton(): void {
  if (filterBound) return;
  filterBound = true;
  document.addEventListener("click", event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest<HTMLElement>('[data-action="sidebar-filter"]');
    if (!button) return;
    openFilterMenu(button);
  });
}

// ── 挂载 ─────────────────────────────────────────────────────────

function bindHost(host: HTMLElement): void {
  if (host.dataset.recentBound) return;
  host.dataset.recentBound = "1";
  host.addEventListener("click", event => {
    const trigger = (event.target as Element).closest<HTMLElement>("[data-recent-action]");
    if (!trigger) return;
    // 操作按钮位于导航链接内部：拦截默认跳转，改为弹出操作菜单
    event.preventDefault();
    event.stopPropagation();
    const row = trigger.closest<HTMLElement>("[data-recent-item]");
    if (!row) return;
    openMenu(trigger, {
      kind: row.dataset.kind === "chat" ? "chat" : "task",
      id: row.dataset.itemId ?? "",
      runId: row.dataset.runId ?? "",
      name: row.dataset.name ?? "",
      status: "QUEUED",
      time: 0,
    });
  });
}

let hydrateSeq = 0;

/** 每次切屏后调用：把侧栏「最近任务」换成真实数据；未登录保持演示条目。 */
export async function hydrateRecentTasks(): Promise<void> {
  const host = document.querySelector<HTMLElement>(".sidebar .recent");
  if (!host) return;
  bindHost(host);
  bindFilterButton();
  syncFilterButtons();
  const seq = ++hydrateSeq;
  const [tasks, chats] = await Promise.all([fetchTaskItems(), fetchChatItems()]);
  // 快速切换筛选时旧请求可能后到：只认最新一次
  if (seq !== hydrateSeq) return;
  // 任务清单拿不到（未登录/后端不可用）且本机也没有对话：保留模板演示条目
  if (tasks === null && chats.length === 0) return;
  closeMenu();
  const items = [...(tasks ?? []), ...chats]
    .sort((a, b) => b.time - a.time)
    .slice(0, MAX_ITEMS);
  renderItems(host, items);
}
