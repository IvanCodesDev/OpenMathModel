import type { ModelingWorkspaceView } from "@openmathmodel/contracts";
import { mountComposerAttachments } from "../attachments/composer-attachments";
import { currentLocale, t } from "../i18n/locale";
import { notifyRunStatusChange } from "../notifications/desktop-notifications";
import { forgetLastTask, rememberLastTask } from "../tasks/last-task-record";
import type { ScreenId } from "../types/screens";
import {
  modelingWorkspaceApi,
  WORKSPACE_EVENT_TYPES,
  WorkspaceApiError,
} from "./modeling-workspace-api";
import { mountTaskStartFlow } from "./task-start-controller";

const ACTIVE_RUN_KEY = "openmathmodel.activeRunId";
const ACTIVE_PROJECT_KEY = "openmathmodel.activeProjectId";
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;
const MODELING_SCREENS = new Set<ScreenId>([
  "running",
  "data",
  "model",
  "experiments",
  "editor",
  "complete",
]);
const ROUTE_BY_GO = {
  running: "/task/running",
  data: "/workspace/data",
  model: "/workspace/model-plan",
  experiments: "/workspace/experiments",
  editor: "/workspace/paper-editor",
  complete: "/task/complete",
} as const;
const STATUS_LABELS: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "进行中",
  WAITING_APPROVAL: "待确认",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  CANCELLED: "已取消",
};
const PAGE_STATUS_LABELS: Record<string, string> = {
  PENDING: "待开始",
  RUNNING: "进行中",
  WAITING_APPROVAL: "待确认",
  PAUSED: "已暂停",
  SUCCEEDED: "完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

let activeCleanup: (() => void) | undefined;

function activeRunId(): string | null {
  const params = new URL(window.location.href).searchParams;
  if (params.get("demo") === "1") return null;
  if (params.has("run_id")) {
    const queryRunId = params.get("run_id") ?? "";
    if (!RUN_ID_PATTERN.test(queryRunId)) return null;
    sessionStorage.setItem(ACTIVE_RUN_KEY, queryRunId);
    return queryRunId;
  }
  const saved = sessionStorage.getItem(ACTIVE_RUN_KEY);
  return saved && RUN_ID_PATTERN.test(saved) ? saved : null;
}

function runAwareUrl(path: string, runId: string, projectId?: string): string {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("run_id", runId);
  if (projectId) url.searchParams.set("project_id", projectId);
  return `${url.pathname}${url.search}${url.hash}`;
}

function navigate(path: string, runId: string, projectId?: string): void {
  window.location.href = runAwareUrl(path, runId, projectId);
}

function replaceText(element: Element | null, value: string): void {
  if (element) element.textContent = value;
}

function pageStatusForDisplay(
  view: ModelingWorkspaceView,
  page: ModelingWorkspaceView["pages"][number],
): ModelingWorkspaceView["pages"][number]["status"] {
  // 兼容尚未升级工作台投影的旧 API：排队态在界面上始终保持“待开始”。
  if (view.run_status === "QUEUED" && page.key === view.active_page && page.status === "RUNNING") {
    return "PENDING";
  }
  return page.status;
}

function renderStatus(root: HTMLElement, screen: ScreenId, view: ModelingWorkspaceView): void {
  root.dataset.runId = view.run_id;
  root.dataset.projectId = view.project_id;
  root.dataset.activeNode = view.active_node;
  root.dataset.activePage = view.active_page;
  root.dataset.runStatus = view.run_status;
  const viewedPage = view.pages.find(item => item.key === screen);
  root.dataset.stageStatus = viewedPage ? pageStatusForDisplay(view, viewedPage) : "PENDING";
  root.dataset.workspaceSource = "api";
  root.dataset.integrationState = "ready";
  sessionStorage.setItem(ACTIVE_RUN_KEY, view.run_id);
  sessionStorage.setItem(ACTIVE_PROJECT_KEY, view.project_id);
  // 每次真实渲染都刷新“最近使用的任务”，供「启动时恢复上次任务」下次开机读取。
  rememberLastTask(view.run_id, view.project_id);

  root.querySelectorAll<HTMLElement>('[data-bind="project-name"], .focused-task-name span, .task-toolbar h2')
    .forEach(element => replaceText(element, view.project_name));
  root.querySelectorAll<HTMLElement>(".run-status").forEach(element => {
    element.classList.toggle("complete", view.run_status === "COMPLETED");
    element.replaceChildren(Object.assign(document.createElement("b"), { ariaHidden: "true" }));
    element.append(` ${STATUS_LABELS[view.run_status] ?? view.run_status}`);
  });

  const stagePane = root.querySelector<HTMLElement>(".focused-stage-pane, .modeling-stage-pane");
  if (stagePane) {
    stagePane.dataset.workspacePage = screen;
    stagePane.dataset.stageStatus = root.dataset.stageStatus;
  }
  view.pages.forEach(page => {
    root.querySelectorAll<HTMLElement>(`[data-workspace-page="${page.key}"], [data-focused-stage="${page.key}"]`)
      .forEach(element => { element.dataset.stageStatus = pageStatusForDisplay(view, page); });
  });
}

function createStepRow(
  page: ModelingWorkspaceView["pages"][number],
  currentStep: string,
  displayStatus: ModelingWorkspaceView["pages"][number]["status"],
  screen: ScreenId,
): HTMLDivElement {
  const row = document.createElement("div");
  const done = displayStatus === "SUCCEEDED";
  const active = ["RUNNING", "WAITING_APPROVAL", "PAUSED", "FAILED"].includes(displayStatus);
  const isViewedPage = page.key === screen;
  row.className = `focused-step${active ? " current" : ""}`;
  row.dataset.stagePage = page.key;
  row.dataset.stageStatus = displayStatus;

  const dot = document.createElement("span");
  dot.className = `focused-step-dot${done ? " done" : ""}`;
  if (done) dot.innerHTML = '<i class="ph ph-check-circle" aria-hidden="true"></i>';
  const label = document.createElement("span");
  label.textContent = active ? currentStep : page.label;
  const status = document.createElement("time");
  status.textContent = PAGE_STATUS_LABELS[displayStatus] ?? displayStatus;
  const chevron = document.createElement("i");
  chevron.setAttribute("aria-hidden", "true");
  if (isViewedPage) {
    // 当前正在查看的页面：只标注，不提供跳转。
    row.setAttribute("aria-current", "page");
    chevron.className = `ph ph-caret-${active ? "up" : "down"} chev`;
  } else {
    // 时间线兼作阶段导航：纯导航跳转（复用统一的 [data-go] 处理，携带运行身份），
    // 不触发任何 /actions，符合 ADR-0007 的错页语义。
    row.dataset.go = page.key;
    row.tabIndex = 0;
    row.setAttribute("role", "link");
    row.setAttribute("aria-label", `前往${page.label}`);
    chevron.className = "ph ph-caret-right chev";
  }
  row.append(dot, label, status, chevron);
  return row;
}

function createRunningStepNodes(
  page: ModelingWorkspaceView["pages"][number],
  currentStep: string,
  summary: string,
  displayStatus: ModelingWorkspaceView["pages"][number]["status"],
): Node[] {
  const done = displayStatus === "SUCCEEDED";
  const active = ["RUNNING", "WAITING_APPROVAL", "PAUSED", "FAILED"].includes(displayStatus);
  const row = document.createElement("div");
  row.className = `progress-step${active ? " open" : ""}`;
  row.tabIndex = 0;
  row.dataset.stagePage = page.key;
  row.dataset.stageStatus = displayStatus;
  row.setAttribute("aria-expanded", String(active));

  const dot = document.createElement("span");
  dot.className = `step-dot${done ? " done" : ""}`;
  if (done) dot.innerHTML = '<i class="ph ph-check" aria-hidden="true"></i>';
  const label = document.createElement("span");
  label.textContent = active ? currentStep : page.label;
  const status = document.createElement("span");
  status.className = "step-time";
  status.textContent = PAGE_STATUS_LABELS[displayStatus] ?? displayStatus;
  const chevron = document.createElement("i");
  chevron.className = "ph ph-caret-down chev";
  chevron.setAttribute("aria-hidden", "true");
  row.append(dot, label, status, chevron);

  const details = document.createElement("div");
  details.className = "step-details";
  details.textContent = active ? summary : `${page.label}阶段${done ? "已完成" : "等待执行"}。`;
  return [row, details];
}

function actionForScreen(
  screen: ScreenId,
  view: ModelingWorkspaceView,
): ModelingWorkspaceView["agent"]["action"] {
  if (screen !== view.active_page) {
    const activePage = view.pages.find(page => page.key === view.active_page);
    return {
      kind: "navigate",
      label: `前往${activePage?.label ?? "当前阶段"}`,
      target_route: view.suggested_route,
      approval_id: null,
      option_id: null,
    };
  }
  const currentRoute = ROUTE_BY_GO[screen as keyof typeof ROUTE_BY_GO];
  if (view.agent.action.kind === "navigate" && view.agent.action.target_route === currentRoute) {
    return {
      kind: "none",
      label: view.agent.state === "QUEUED" ? "等待任务开始" : "Agent 正在执行",
      target_route: null,
      approval_id: null,
      option_id: null,
    };
  }
  if (screen === "model" && view.agent.action.kind === "approve") {
    return {
      ...view.agent.action,
      label: "确认 Agent 当前方案并继续",
    };
  }
  return view.agent.action;
}

function agentSummaryForScreen(screen: ScreenId, view: ModelingWorkspaceView): string {
  if (screen === "model" && view.agent.action.kind === "approve") {
    return `${view.agent.summary} 右侧方案正文目前是演示模板，本次操作确认的是 Agent 当前后端方案。`;
  }
  return view.agent.summary;
}

function renderAgent(root: HTMLElement, screen: ScreenId, view: ModelingWorkspaceView): void {
  const list = root.querySelector<HTMLElement>(".focused-activity-list");
  if (list) {
    list.replaceChildren(...view.pages.map(page => (
      createStepRow(page, view.agent.current_step, pageStatusForDisplay(view, page), screen)
    )));
  }

  const runningList = root.querySelector<HTMLElement>(".activity-list[data-agent-steps]");
  if (runningList) {
    runningList.replaceChildren(...view.pages.flatMap(page => (
      createRunningStepNodes(
        page,
        view.agent.current_step,
        view.agent.summary,
        pageStatusForDisplay(view, page),
      )
    )));
  }

  const copy = root.querySelector<HTMLElement>(".focused-agent-copy");
  if (copy) {
    const title = document.createElement("strong");
    title.textContent = view.agent.title;
    const paragraph = document.createElement("p");
    paragraph.textContent = agentSummaryForScreen(screen, view);
    copy.replaceChildren(title, paragraph);
    copy.dataset.agentState = view.agent.state;
  }


  const runningCopy = root.querySelector<HTMLElement>(".modeling-agent-copy[data-agent-summary]");
  if (runningCopy) {
    const title = document.createElement("strong");
    title.textContent = view.agent.title;
    const paragraph = document.createElement("p");
    paragraph.textContent = agentSummaryForScreen(screen, view);
    runningCopy.replaceChildren(title, paragraph);
    runningCopy.dataset.agentState = view.agent.state;
  }

  // 页面可能同时存在演示 CTA（真实运行时被 CSS 隐藏）与真实模式 CTA（data-live-only），
  // 统一写入同一后端动作，点击处理按就近的 [data-agent-cta] 生效。
  const action = actionForScreen(screen, view);
  root.querySelectorAll<HTMLButtonElement>("[data-agent-cta]").forEach(cta => {
    cta.removeAttribute("data-go");
    cta.dataset.agentAction = action.kind;
    cta.textContent = action.label;
    cta.disabled = action.kind === "none";
  });
}

function formatBytes(value: number | null): string {
  if (value === null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

function artifactKinds(screen: ScreenId, panel: string | undefined): Set<string> | null {
  if (screen === "experiments") {
    if (panel === "charts") return new Set(["figure"]);
    if (panel === "results-table") return new Set(["table", "dataset"]);
    if (panel === "run-log") return new Set(["log"]);
    if (panel === "model-code") return new Set(["code", "model"]);
  }
  if (screen === "complete") {
    if (panel === "paper-package") return new Set(["paper", "report"]);
    if (panel === "data-code-package") return new Set(["dataset", "code", "model"]);
    if (panel === "delivery-record") return new Set(["log", "other"]);
    if (panel === "final-summary") return null;
  }
  return new Set();
}

function createArtifactRow(
  artifact: ModelingWorkspaceView["artifacts"][number],
): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "deliverable";
  row.dataset.artifactId = artifact.id;
  const name = document.createElement("span");
  name.className = "deliverable-name";
  const icon = document.createElement("i");
  icon.className = "ph ph-file";
  icon.setAttribute("aria-hidden", "true");
  name.append(icon, artifact.name);
  const mediaType = document.createElement("span");
  const statusLabel = {
    PENDING: "待就绪",
    READY: "可下载",
    STALE: "已过期",
    DELETED: "已删除",
  }[artifact.status];
  mediaType.textContent = `${artifact.kind.toUpperCase()} · ${statusLabel}`;
  const size = document.createElement("span");
  size.textContent = formatBytes(artifact.size_bytes);
  const download = document.createElement("button");
  download.type = "button";
  download.className = "open-file";
  if (artifact.status === "READY" && artifact.download_url) {
    download.dataset.artifactDownload = artifact.download_url;
    download.setAttribute("aria-label", `下载 ${artifact.name}`);
  } else {
    download.disabled = true;
    download.setAttribute("aria-label", `${artifact.name}${statusLabel}`);
  }
  download.innerHTML = '<i class="ph ph-download-simple" aria-hidden="true"></i>';
  row.dataset.artifactStatus = artifact.status;
  row.append(name, mediaType, size, download);
  return row;
}

function createEmptyArtifactRow(): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "deliverable";
  row.dataset.artifactEmpty = "true";
  const name = document.createElement("span");
  name.className = "deliverable-name";
  name.textContent = "该阶段尚未发布产物";
  row.append(name, Object.assign(document.createElement("span"), { textContent: "—" }), Object.assign(document.createElement("span"), { textContent: "—" }), document.createElement("span"));
  return row;
}

function renderArtifacts(root: HTMLElement, screen: ScreenId, view: ModelingWorkspaceView): void {
  const pageArtifactIds = new Set(
    screen === "complete"
      ? view.artifacts.map(artifact => artifact.id)
      : view.pages.find(page => page.key === screen)?.artifact_ids ?? [],
  );
  root.querySelectorAll<HTMLElement>("[data-workspace-panel]").forEach(panel => {
    const list = panel.querySelector<HTMLElement>(".deliverables");
    if (!list) return;
    const allowed = artifactKinds(screen, panel.dataset.workspacePanel);
    if (allowed?.size === 0) return;
    const pageArtifacts = view.artifacts.filter(artifact => pageArtifactIds.has(artifact.id));
    const artifacts = allowed === null
      ? pageArtifacts
      : pageArtifacts.filter(artifact => allowed.has(artifact.kind));
    list.querySelectorAll(":scope > .deliverable").forEach(row => row.remove());
    list.append(...(artifacts.length ? artifacts.map(createArtifactRow) : [createEmptyArtifactRow()]));
    list.dataset.workspaceSource = "api";
  });

  // 这一行由变量拼接而成，DOM 翻译器匹配不到整段，逐段过词典。
  const runStatusLabel = t(STATUS_LABELS[view.run_status] ?? view.run_status);
  const updatedAt = new Date(view.updated_at).toLocaleString(
    currentLocale() === "en-US" ? "en-US" : "zh-CN",
  );
  replaceText(
    root.querySelector(".focused-run-meta"),
    `${t("运行 ID")}: ${view.run_id} | ${t("状态")}: ${runStatusLabel} | ${t("更新时间")}: ${updatedAt}`,
  );
}

function decorateNavigation(root: HTMLElement, view: ModelingWorkspaceView): void {
  root.querySelectorAll<HTMLAnchorElement>("a[href]").forEach(link => {
    const url = new URL(link.href, window.location.origin);
    if (!Object.values(ROUTE_BY_GO).some(path => path === url.pathname)) return;
    link.href = runAwareUrl(url.pathname, view.run_id, view.project_id);
  });
}

function renderWorkspace(root: HTMLElement, screen: ScreenId, view: ModelingWorkspaceView): void {
  root.querySelectorAll<HTMLElement>("[data-workspace-loading]").forEach(element => {
    delete element.dataset.workspaceLoading;
    if (element instanceof HTMLButtonElement) element.disabled = false;
  });
  renderStatus(root, screen, view);
  renderAgent(root, screen, view);
  renderArtifacts(root, screen, view);
  decorateNavigation(root, view);
  if (screen === "editor") {
    root.querySelectorAll<HTMLButtonElement>('[data-action="continue-paper"]').forEach(button => {
      const ready = view.run_status === "COMPLETED";
      button.disabled = !ready;
      button.textContent = ready ? "完成交付" : "Agent 正在撰写";
      button.dataset.workspaceControlled = "true";
    });
  }
  root.querySelectorAll<HTMLButtonElement>('[data-action="download-all"]').forEach(button => {
    button.textContent = "导出文件清单";
    button.dataset.workspaceControlled = "true";
  });
}

function renderError(root: HTMLElement, error: unknown): void {
  root.dataset.integrationState = "error";
  const copy = root.querySelector<HTMLElement>(
    ".focused-agent-copy, .modeling-agent-copy[data-agent-summary]",
  );
  if (!copy) return;
  const message = error instanceof WorkspaceApiError
    ? error.message
    : error instanceof Error
      ? error.message
      : "建模状态同步失败";
  const title = document.createElement("strong");
  title.textContent = "状态同步需要处理";
  const paragraph = document.createElement("p");
  paragraph.textContent = message;
  copy.replaceChildren(title, paragraph);
}

function downloadArtifactManifest(view: ModelingWorkspaceView): void {
  const rows = view.artifacts.map(artifact => [
    artifact.name,
    artifact.kind.toUpperCase(),
    artifact.status,
    formatBytes(artifact.size_bytes),
    artifact.download_url ?? t("不可下载"),
  ].join("\t"));
  const content = [t("文件名称\t类型\t状态\t大小\t下载地址"), ...rows].join("\n");
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${view.project_name}${t("-交付文件清单.txt")}`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function mountModelingWorkspace(screen: ScreenId): void {
  // 必须早于 mountTaskStartFlow：任务创建流程要从输入框的附件集合里取解析结果。
  mountComposerAttachments();
  mountTaskStartFlow(screen);
  activeCleanup?.();
  activeCleanup = undefined;
  if (!MODELING_SCREENS.has(screen)) return;

  const root = document.querySelector<HTMLElement>("[data-modeling-shell]");
  const runId = activeRunId();
  if (!root || !runId) {
    if (root) root.dataset.integrationState = "demo";
    return;
  }

  const abortController = new AbortController();
  let currentView: ModelingWorkspaceView | undefined;
  let currentScreen: ScreenId = screen;
  let eventSource: EventSource | undefined;
  let refreshTimer: number | undefined;
  let reconnectTimer: number | undefined;
  let lastSequence: number | undefined;
  let streamEnded = false;
  let disposed = false;
  let actionPending = false;
  let actionToken: string | undefined;
  let actionFingerprint: string | undefined;

  // ── 合并工作台（B 方案）：五个阶段面板同存于一个页面，阶段间跳转是软切换 ──
  const WORKSPACE_STAGE_SET = new Set<ScreenId>(["data", "model", "experiments", "editor", "complete"]);
  const stageForPath = (path: string): ScreenId | undefined => (
    (Object.entries(ROUTE_BY_GO) as [ScreenId, string][]).find(([, route]) => route === path)?.[0]
  );

  const switchStage = (stage: ScreenId): void => {
    if (!currentView || stage === currentScreen) return;
    currentScreen = stage;
    // 面板显隐、URL pushState 与顶栏铬件由模板层 showWorkspaceStage 完成（事件解耦，避免模块循环依赖）。
    document.dispatchEvent(new CustomEvent("omm:show-stage", {
      detail: {
        stage,
        url: runAwareUrl(ROUTE_BY_GO[stage as keyof typeof ROUTE_BY_GO], currentView.run_id, currentView.project_id),
      },
    }));
    renderWorkspace(root, currentScreen, currentView);
  };

  const navigateTo = (path: string): void => {
    if (!currentView) return;
    const stage = stageForPath(path);
    if (stage && WORKSPACE_STAGE_SET.has(stage) && WORKSPACE_STAGE_SET.has(currentScreen)) {
      switchStage(stage);
      return;
    }
    navigate(path, currentView.run_id, currentView.project_id);
  };

  // 浏览器前进/后退（popstate）由模板层换面板后广播 stage-shown，这里同步投影渲染。
  const onStageShown = (event: Event): void => {
    const stage = (event as CustomEvent<{ stage?: ScreenId }>).detail?.stage;
    if (!stage || stage === currentScreen || !WORKSPACE_STAGE_SET.has(stage)) return;
    currentScreen = stage;
    if (currentView) renderWorkspace(root, currentScreen, currentView);
  };
  document.addEventListener("omm:stage-shown", onStageShown);

  const refresh = async (showError = true): Promise<void> => {
    try {
      const view = await modelingWorkspaceApi.get(runId, abortController.signal);
      if (disposed) return;
      // 首屏快照没有“上一个状态”，此时不提醒：用户刚打开页面，不该被历史状态打扰。
      notifyRunStatusChange({
        runId: view.run_id,
        projectId: view.project_id,
        projectName: view.project_name,
        previous: currentView?.run_status,
        current: view.run_status,
      });
      currentView = view;
      if (view.latest_event_sequence !== null) {
        lastSequence = Math.max(lastSequence ?? 0, view.latest_event_sequence);
      }
      renderWorkspace(root, currentScreen, view);
    } catch (error) {
      if (disposed || (error instanceof DOMException && error.name === "AbortError")) return;
      if (error instanceof WorkspaceApiError && error.status === 404) {
        // 运行已不存在（如本地库重置）时清除活动身份，其余流程页可回到演示态自愈；
        // 当前页仍按错误语义原位提示。启动恢复记录一并清掉，下次开机不再跳向死链。
        sessionStorage.removeItem(ACTIVE_RUN_KEY);
        sessionStorage.removeItem(ACTIVE_PROJECT_KEY);
        forgetLastTask(runId);
      }
      if (showError) renderError(root, error);
    }
  };

  const scheduleRefresh = (): void => {
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => void refresh(false), 80);
  };

  const connectEvents = (): void => {
    if (disposed || streamEnded) return;
    const url = new URL(`/api/v1/task-runs/${encodeURIComponent(runId)}/events`, window.location.origin);
    if (lastSequence) url.searchParams.set("after", String(lastSequence));
    eventSource = new EventSource(`${url.pathname}${url.search}`);
    const onWorkspaceEvent = (event: Event): void => {
      const sequence = Number((event as MessageEvent).lastEventId);
      if (Number.isSafeInteger(sequence) && sequence > 0) {
        lastSequence = Math.max(lastSequence ?? 0, sequence);
      }
      scheduleRefresh();
    };
    WORKSPACE_EVENT_TYPES.forEach(type => eventSource?.addEventListener(type, onWorkspaceEvent));
    eventSource.addEventListener("stream.end", () => {
      streamEnded = true;
      scheduleRefresh();
      eventSource?.close();
    });
    eventSource.addEventListener("error", () => {
      eventSource?.close();
      if (disposed || streamEnded) return;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(connectEvents, 600);
    });
  };

  const toggleRunningStep = (row: HTMLElement): void => {
    row.classList.toggle("open");
    row.setAttribute("aria-expanded", String(row.classList.contains("open")));
  };

  const onClick = (event: MouseEvent): void => {
    const target = event.target instanceof Element ? event.target : null;
    const runningStep = target?.closest<HTMLElement>(".activity-list[data-agent-steps] .progress-step");
    if (runningStep) {
      event.preventDefault();
      event.stopPropagation();
      toggleRunningStep(runningStep);
      return;
    }

    const controlledTarget = target?.closest<HTMLElement>(
      '[data-go], [data-agent-cta], [data-action="continue-paper"], [data-action="download-all"]',
    );
    if (controlledTarget && !currentView) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    const download = target?.closest<HTMLElement>("[data-artifact-download]");
    if (download?.dataset.artifactDownload) {
      event.preventDefault();
      event.stopPropagation();
      window.location.href = download.dataset.artifactDownload;
      return;
    }

    const downloadAll = target?.closest<HTMLButtonElement>('[data-action="download-all"]');
    if (downloadAll && currentView) {
      event.preventDefault();
      event.stopPropagation();
      downloadArtifactManifest(currentView);
      return;
    }

    const completePaper = target?.closest<HTMLButtonElement>('[data-action="continue-paper"]');
    if (completePaper && currentView) {
      event.preventDefault();
      event.stopPropagation();
      if (currentView.run_status === "COMPLETED") {
        navigateTo(ROUTE_BY_GO.complete);
      }
      return;
    }

    const cta = target?.closest<HTMLButtonElement>("[data-agent-cta]");
    if (cta && currentView) {
      event.preventDefault();
      event.stopPropagation();
      if (actionPending) return;
      const action = actionForScreen(currentScreen, currentView);
      if (action.kind === "navigate" && action.target_route) {
        navigateTo(action.target_route);
        return;
      }
      if (!["approve", "pause", "resume", "retry"].includes(action.kind)) return;
      if (action.kind === "approve" && !action.option_id) {
        renderError(root, new Error("请先在当前方案页选择要采用的方案"));
        return;
      }
      actionPending = true;
      cta.disabled = true;
      cta.textContent = "正在提交…";
      let actionFailed = false;
      const fingerprint = JSON.stringify({
        action: action.kind,
        approval_id: action.approval_id,
        option_id: action.option_id,
      });
      if (!actionToken || actionFingerprint !== fingerprint) {
        actionToken = crypto.randomUUID().replaceAll("-", "");
        actionFingerprint = fingerprint;
      }
      void modelingWorkspaceApi.act(runId, {
        action: action.kind as "approve" | "pause" | "resume" | "retry",
        approval_id: action.approval_id,
        option_id: action.option_id,
        client_token: actionToken,
      }, abortController.signal).then(
        async () => {
          actionToken = undefined;
          actionFingerprint = undefined;
          await refresh();
          // 阶段推进体验：方案确认（非退回重做）后直接进入“实验与验证”页跟随执行。
          // 这是用户显式确认动作的延续；ADR-0007 禁止的是“加载时自动跳页”，不适用于此。
          if (
            action.kind === "approve"
            && action.option_id !== "reject"
            && currentScreen === "model"
            && currentView
          ) {
            navigateTo(ROUTE_BY_GO.experiments);
          }
        },
        error => {
          actionFailed = true;
          if (disposed || (error instanceof DOMException && error.name === "AbortError")) return;
          renderError(root, error);
        },
      ).finally(() => {
        actionPending = false;
        if (!currentView) return;
        const currentAction = actionForScreen(currentScreen, currentView);
        cta.disabled = currentAction.kind === "none";
        cta.textContent = currentAction.label;
        if (!actionFailed) renderAgent(root, currentScreen, currentView);
      });
      return;
    }

    const goTarget = target?.closest<HTMLElement>("[data-go]");
    const go = goTarget?.dataset.go as keyof typeof ROUTE_BY_GO | undefined;
    if (go && ROUTE_BY_GO[go] && currentView) {
      event.preventDefault();
      event.stopPropagation();
      navigateTo(ROUTE_BY_GO[go]);
    }
  };

  const onKeyDown = (event: KeyboardEvent): void => {
    if (!['Enter', ' '].includes(event.key)) return;
    const target = event.target instanceof Element ? event.target : null;
    const row = target?.closest<HTMLElement>(".activity-list[data-agent-steps] .progress-step");
    if (row) {
      event.preventDefault();
      toggleRunningStep(row);
      return;
    }
    // 时间线阶段行的键盘导航（与点击同语义：纯导航，带运行身份）。
    const stepLink = target?.closest<HTMLElement>(".focused-activity-list [data-go]");
    const go = stepLink?.dataset.go as keyof typeof ROUTE_BY_GO | undefined;
    if (go && ROUTE_BY_GO[go] && currentView) {
      event.preventDefault();
      navigateTo(ROUTE_BY_GO[go]);
    }
  };

  root.addEventListener("click", onClick, true);
  root.addEventListener("keydown", onKeyDown, true);
  const cleanup = (): void => {
    if (disposed) return;
    disposed = true;
    abortController.abort();
    eventSource?.close();
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    document.removeEventListener("omm:stage-shown", onStageShown);
    root.removeEventListener("click", onClick, true);
    root.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("pagehide", cleanup);
  };
  activeCleanup = cleanup;
  window.addEventListener("pagehide", cleanup, { once: true });

  root.dataset.integrationState = "loading";
  root.querySelectorAll<HTMLElement>(
    '[data-go], [data-agent-cta], [data-action="continue-paper"], [data-action="download-all"]',
  ).forEach(element => {
    element.dataset.workspaceLoading = "true";
    if (element instanceof HTMLButtonElement) element.disabled = true;
  });
  void refresh().then(() => {
    if (!disposed && currentView) connectEvents();
  });
}
