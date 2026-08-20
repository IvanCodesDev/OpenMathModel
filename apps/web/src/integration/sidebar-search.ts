/**
 * 侧边栏「搜索任务」的真实搜索（原输入框是纯装饰，不绑定任何行为）。
 *
 * 数据源 = 当前账户的真实 Project 与 TaskRun 控制面接口（与建模工作台同源），
 * 输入按任务目标 / 项目名 / 状态标签在本机筛选，结果面板列出最近命中，
 * 点击直达该任务的执行页。未登录、后端不可用与无命中都有对应提示行。
 * 事件全部走 document 级委托：侧栏随页面切换整体重渲染，绑定不能挂在节点上。
 */

import { modelingWorkspaceApi, WorkspaceApiError } from "./modeling-workspace-api";
import { buildRunningUrl } from "./task-start-state";

const STATUS_LABELS: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "进行中",
  WAITING_APPROVAL: "待确认",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  CANCELLED: "已取消",
};

const RESULT_LIMIT = 8;
const CACHE_TTL_MS = 30_000;
const DEBOUNCE_MS = 180;

interface SearchEntry {
  runId: string;
  projectId: string;
  title: string;
  projectName: string;
  statusLabel: string;
  updatedAt: number;
  /** 小写检索文本：任务目标 + 项目名 + 状态标签。 */
  haystack: string;
}

let cachedAt = 0;
let cachedEntries: SearchEntry[] | null = null;
let loading: Promise<SearchEntry[]> | null = null;

async function fetchEntries(): Promise<SearchEntry[]> {
  const [projects, runs] = await Promise.all([
    modelingWorkspaceApi.listProjects(),
    modelingWorkspaceApi.listTaskRuns(200),
  ]);
  const names = new Map(projects.items.map(project => [project.id, project.name]));
  return runs.items.map(run => {
    const projectName = names.get(run.project_id) ?? "";
    const title = run.goal.trim() || projectName || "未命名任务";
    const statusLabel = STATUS_LABELS[run.status] ?? run.status;
    return {
      runId: run.id,
      projectId: run.project_id,
      title,
      projectName,
      statusLabel,
      updatedAt: Date.parse(run.updated_at || run.created_at) || 0,
      haystack: `${title} ${projectName} ${statusLabel}`.toLowerCase(),
    };
  });
}

/** 30 秒内复用已拉取的任务清单；过期或首次调用时重新请求，并发只发一次。 */
function loadEntries(): Promise<SearchEntry[]> {
  if (cachedEntries && Date.now() - cachedAt < CACHE_TTL_MS) {
    return Promise.resolve(cachedEntries);
  }
  loading ??= fetchEntries()
    .then(entries => {
      cachedEntries = entries;
      cachedAt = Date.now();
      return entries;
    })
    .finally(() => {
      loading = null;
    });
  return loading;
}

function searchRow(input: HTMLInputElement): HTMLElement | null {
  return input.closest<HTMLElement>(".sidebar-search-row");
}

function ensurePanel(row: HTMLElement): HTMLElement {
  let panel = row.querySelector<HTMLElement>(".sidebar-search-results");
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "sidebar-search-results";
    panel.setAttribute("role", "listbox");
    panel.hidden = true;
    row.append(panel);
  }
  return panel;
}

function closeAllPanels(): void {
  document.querySelectorAll<HTMLElement>(".sidebar-search-results").forEach(panel => {
    panel.hidden = true;
  });
}

function noticeRow(title: string, sub = ""): HTMLElement {
  const row = document.createElement("div");
  row.className = "sidebar-search-empty";
  const strong = document.createElement("strong");
  strong.textContent = title;
  row.append(strong);
  if (sub) {
    const span = document.createElement("span");
    span.textContent = sub;
    row.append(span);
  }
  return row;
}

function renderResults(panel: HTMLElement, entries: SearchEntry[]): void {
  panel.replaceChildren(
    ...entries.map((entry, index) => {
      const item = document.createElement("a");
      item.className = `sidebar-search-item${index === 0 ? " is-active" : ""}`;
      item.href = buildRunningUrl(entry.runId, entry.projectId);
      item.setAttribute("role", "option");
      const title = document.createElement("strong");
      title.textContent = entry.title;
      const sub = document.createElement("span");
      sub.textContent = entry.projectName
        ? `${entry.projectName} · ${entry.statusLabel}`
        : entry.statusLabel;
      item.append(title, sub);
      return item;
    }),
  );
  panel.hidden = false;
}

let searchSeq = 0;

async function runSearch(input: HTMLInputElement): Promise<void> {
  const row = searchRow(input);
  if (!row) return;
  const panel = ensurePanel(row);
  const query = input.value.trim().toLowerCase();
  if (!query) {
    panel.hidden = true;
    return;
  }
  const seq = ++searchSeq;
  if (!cachedEntries) {
    panel.replaceChildren(noticeRow("正在载入任务…"));
    panel.hidden = false;
  }
  let entries: SearchEntry[];
  try {
    entries = await loadEntries();
  } catch (error) {
    if (seq !== searchSeq) return;
    const unauthorized = error instanceof WorkspaceApiError && error.status === 401;
    panel.replaceChildren(
      unauthorized
        ? noticeRow("登录后可搜索任务", "任务清单需要登录账户后读取")
        : noticeRow("无法连接服务", "请确认后端已启动后重试"),
    );
    panel.hidden = false;
    return;
  }
  if (seq !== searchSeq) return; // 输入已更新，本次结果作废
  const terms = query.split(/\s+/).filter(Boolean);
  const matched = entries
    .filter(entry => terms.every(term => entry.haystack.includes(term)))
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, RESULT_LIMIT);
  if (matched.length === 0) {
    panel.replaceChildren(noticeRow("没有匹配的任务", "换个关键词，或先在首页创建任务"));
    panel.hidden = false;
    return;
  }
  renderResults(panel, matched);
}

function moveActive(panel: HTMLElement, step: number): void {
  const items = [...panel.querySelectorAll<HTMLElement>(".sidebar-search-item")];
  if (items.length === 0) return;
  const current = items.findIndex(item => item.classList.contains("is-active"));
  const next = ((current < 0 ? 0 : current) + step + items.length) % items.length;
  items.forEach((item, index) => item.classList.toggle("is-active", index === next));
  items[next].scrollIntoView({ block: "nearest" });
}

function asSearchInput(target: EventTarget | null): HTMLInputElement | null {
  return target instanceof HTMLInputElement && target.matches("[data-sidebar-search]")
    ? target
    : null;
}

let mounted = false;
let debounceTimer: number | undefined;

export function mountSidebarSearch(): void {
  if (mounted) return;
  mounted = true;

  document.addEventListener("input", event => {
    const input = asSearchInput(event.target);
    if (!input) return;
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      void runSearch(input);
    }, DEBOUNCE_MS);
  });

  document.addEventListener("focusin", event => {
    const input = asSearchInput(event.target);
    if (!input) return;
    // 聚焦即预取任务清单，敲第一个字时结果就能立刻出来；失败留给真正搜索时报
    void loadEntries().catch(() => undefined);
    if (input.value.trim()) void runSearch(input);
  });

  document.addEventListener("keydown", event => {
    const input = asSearchInput(event.target);
    if (!input) return;
    const panel = searchRow(input)?.querySelector<HTMLElement>(".sidebar-search-results");
    if (!panel || panel.hidden) return;
    if (event.key === "Escape") {
      panel.hidden = true;
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(panel, event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter") {
      const active =
        panel.querySelector<HTMLAnchorElement>(".sidebar-search-item.is-active") ??
        panel.querySelector<HTMLAnchorElement>(".sidebar-search-item");
      if (active) {
        event.preventDefault();
        window.location.assign(active.href);
      }
    }
  });

  document.addEventListener("click", event => {
    const target = event.target;
    if (target instanceof Element && target.closest(".sidebar-search-row")) return;
    closeAllPanels();
  });
}
