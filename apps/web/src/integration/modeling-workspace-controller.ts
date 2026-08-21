import type { ModelingWorkspaceView } from "@openmathmodel/contracts";
import { mountComposerAttachments } from "../attachments/composer-attachments";
import { currentLocale, t } from "../i18n/locale";
import { configureConversation } from "./agent-chat";
import { notifyRunStatusChange } from "../notifications/desktop-notifications";
import { saveHistoryEnabled } from "../preferences/privacy-preferences";
import { forgetLastTask, rememberLastTask } from "../tasks/last-task-record";
import type { ScreenId } from "../types/screens";
import {
  modelingWorkspaceApi,
  WORKSPACE_EVENT_TYPES,
  WorkspaceApiError,
} from "./modeling-workspace-api";
import { hydrateRecentTasks } from "./recent-tasks";
import { hideTaskTodos, renderTaskTodos, type TaskTodoItem } from "./task-todo-panel";
import {
  openAttachmentsDialog,
  openTaskHeaderMenu,
  renderHeaderAttachments,
} from "./task-header-actions";
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

// ── AI 对话微动效（动效语言参考 aicss.dev 的 Streaming Text / To-do List 组件，
//    按产品黑灰白体系与现有 DOM 重写实现；全部尊重 prefers-reduced-motion）──

function prefersReducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

const streamingTexts = new WeakMap<HTMLElement, { text: string; timer?: number }>();

/** 打字机式文本更新：同一元素文本未变化时不动；变化时逐字浮现。 */
function setStreamingText(element: HTMLElement, text: string): void {
  const previous = streamingTexts.get(element);
  if (previous?.text === text) return;
  if (previous?.timer !== undefined) window.clearInterval(previous.timer);
  if (prefersReducedMotion() || text.length > 420) {
    element.textContent = text;
    streamingTexts.set(element, { text });
    return;
  }
  let shown = 0;
  element.textContent = "";
  const timer = window.setInterval(() => {
    shown += 3;
    element.textContent = text.slice(0, shown);
    if (shown >= text.length) {
      window.clearInterval(timer);
      streamingTexts.set(element, { text });
    }
  }, 12);
  streamingTexts.set(element, { text, timer });
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

function parseIso(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/** 耗时展示：10 秒内一位小数，一分钟内整秒，更长用分+秒。 */
function formatElapsed(ms: number): string {
  const clamped = Math.max(0, ms);
  if (clamped < 10_000) return `${(clamped / 1000).toFixed(1)}s`;
  if (clamped < 60_000) return `${Math.round(clamped / 1000)}s`;
  const minutes = Math.floor(clamped / 60_000);
  const seconds = Math.round((clamped % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
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
  // 每次真实渲染都刷新“最近使用的任务”，供「启动时恢复上次任务」下次开机读取；
  // 「保存任务历史」（数据与隐私）关闭时不留本机记录。
  if (saveHistoryEnabled()) rememberLastTask(view.run_id, view.project_id);

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

/**
 * 思考/规划阶段：题意解析（问题分析页）真正完成之前都算规划中。此时执行
 * 计划面板只显示「正在思考并规划…」的思考态；首阶段落定（成功/失败）或
 * 后续阶段已启动时，说明计划已经产生，面板才揭示计划列表。
 * 运行离开 QUEUED/RUNNING（暂停、待审批、终态）时需要展示状态，同样揭示。
 */
function isPlanningPhase(view: ModelingWorkspaceView): boolean {
  if (view.run_status === "QUEUED") return true;
  if (view.run_status !== "RUNNING") return false;
  return view.pages.every(page => (
    page.key === "running"
      ? page.status === "PENDING" || page.status === "RUNNING"
      : page.status === "PENDING"
  ));
}

// ── 活动流：思考/工具/叙述交替的细粒度过程（数据 = run.log 等领域事件） ────

const STAGE_BY_PROMPT: Record<string, string> = {
  "problem_analysis.default": "题意解析",
  "model_planning.default": "建模方案",
};

interface AgentStreamState {
  host: HTMLElement | null;
  seen: Set<number>;
  /** 待落定的行（等待确认等）：key → 行元素与服务端开始时间 */
  pending: Map<string, { element: HTMLElement; sinceServerMs: number | null }>;
}

const streamByRoot = new WeakMap<HTMLElement, AgentStreamState>();

function streamState(root: HTMLElement): AgentStreamState {
  let state = streamByRoot.get(root);
  if (!state) {
    state = { host: null, seen: new Set(), pending: new Map() };
    streamByRoot.set(root, state);
  }
  return state;
}

/** 首条 Agent 消息是否已「封口」：开场分析结束且计划相位到达 revealed
 *  （root.dataset.planPhase 由 renderAgent 维护）。封口后新的运行事件不再
 *  挤回首气泡，而是按时间顺序流向对话末尾（见 resolveStreamHost）。 */
function firstAssistantMessage(root: HTMLElement, scroll: HTMLElement): { block: HTMLElement | null; sealed: boolean } {
  const block = scroll.querySelector<HTMLElement>(".assistant-block:not(.follow-up-reply)");
  if (!block) return { block: null, sealed: false };
  const pending = block.dataset.openingState === "pending";
  return { block, sealed: !pending && root.dataset.planPhase === "revealed" };
}

/** 对话末尾的执行轨迹块：与首条 Agent 消息同构（署名 + 可展开折叠头 + 活动流）。
 *  尾部已是轨迹块则继续续写；被用户消息或对话回复隔断后，新事件另起一块，
 *  保证执行过程与对话在页面上严格按发生顺序交替。 */
function tailTraceHost(scroll: HTMLElement, identitySource: HTMLElement): HTMLElement | null {
  const tail = scroll.lastElementChild;
  if (tail instanceof HTMLElement && tail.classList.contains("agent-activity-block")) {
    return tail.querySelector<HTMLElement>(".agent-stream");
  }
  const block = document.createElement("div");
  block.className = "assistant-block follow-up-reply agent-activity-block";
  const identity = identitySource.querySelector<HTMLElement>(".assistant-id");
  if (identity) block.append(identity.cloneNode(true));
  const header = document.createElement("button");
  header.type = "button";
  header.className = "activity-summary";
  header.dataset.action = "toggle-activity";
  header.setAttribute("aria-expanded", "true");
  header.innerHTML = `<i class="ph ph-eye-slash" aria-hidden="true"></i> ${t("收起执行步骤")} <i class="ph ph-caret-up" aria-hidden="true"></i>`;
  const host = document.createElement("div");
  host.className = "agent-stream";
  block.append(header, host);
  scroll.append(block);
  return host;
}

/** 解析本条过程行的落点：
 *  - 聚焦布局（无对话流）或首条消息未封口：锚定在摘要之后——开场分析结束前
 *    保持隐藏，揭示时与计划一起放行（先思考 → 再计划 → 后过程）；
 *  - 首条消息封口后：写入对话末尾的执行轨迹块，与后续对话按时间交替。 */
function resolveStreamHost(root: HTMLElement, state: AgentStreamState): HTMLElement | null {
  const scroll = root.querySelector<HTMLElement>(".chat-scroll");
  if (scroll) {
    const { block, sealed } = firstAssistantMessage(root, scroll);
    if (block && sealed) return tailTraceHost(scroll, block);
  }
  if (state.host?.isConnected) return state.host;
  const anchor = root.querySelector<HTMLElement>("[data-agent-summary]");
  if (!anchor) return null;
  const host = document.createElement("div");
  host.className = "agent-stream";
  // 开场分析尚未落定时不显示过程行：renderAgent 在揭示时统一放行
  if (anchor.closest<HTMLElement>(".assistant-block")?.dataset.openingState === "pending") {
    host.hidden = true;
  }
  anchor.insertAdjacentElement("afterend", host);
  state.host = host;
  return host;
}

function streamAppend(root: HTMLElement, node: HTMLElement): void {
  const host = resolveStreamHost(root, streamState(root));
  if (!host) return;
  const scroll = root.querySelector<HTMLElement>(".chat-scroll, .focused-agent-scroll");
  const stick = scroll
    ? scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120
    : false;
  node.classList.add("stream-in");
  host.append(node);
  if (stick && scroll) scroll.scrollTop = scroll.scrollHeight;
}

function streamNarration(root: HTMLElement, text: string): void {
  const paragraph = document.createElement("p");
  paragraph.className = "stream-narration";
  paragraph.textContent = text;
  streamAppend(root, paragraph);
}

interface StreamRowOptions {
  key?: string;
  icon: string;
  title: string;
  elapsedMs?: number;
  /** 等待中的行：服务端开始时间（落定时求差），并实时走秒 */
  waitingSinceMs?: number | null;
  detail?: string;
  mono?: boolean;
}

function streamRow(root: HTMLElement, options: StreamRowOptions): void {
  const state = streamState(root);
  const item = document.createElement("div");
  item.className = `stream-item${options.waitingSinceMs !== undefined ? " is-waiting" : ""}`;
  item.innerHTML = `
    <div class="stream-row">
      <i class="ph ph-${options.icon}" aria-hidden="true"></i>
      <span class="stream-title"></span>
      <time class="stream-elapsed"></time>
    </div>`;
  item.querySelector<HTMLElement>(".stream-title")!.textContent = options.title;
  const timeCell = item.querySelector<HTMLElement>(".stream-elapsed")!;
  if (options.waitingSinceMs !== undefined) {
    const localSince = Date.now();
    timeCell.dataset.elapsedSince = String(localSince);
    timeCell.textContent = formatElapsed(0);
    if (options.key) {
      state.pending.set(options.key, { element: item, sinceServerMs: options.waitingSinceMs ?? null });
    }
  } else if (options.elapsedMs !== undefined) {
    timeCell.textContent = formatElapsed(options.elapsedMs);
  }
  if (options.detail) {
    const row = item.querySelector<HTMLElement>(".stream-row")!;
    row.classList.add("is-expandable");
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.setAttribute("aria-expanded", "false");
    row.insertAdjacentHTML("beforeend", '<i class="ph ph-caret-down stream-chevron" aria-hidden="true"></i>');
    const detail = document.createElement("div");
    detail.className = `stream-detail${options.mono ? " is-mono" : ""}`;
    detail.hidden = true;
    const pre = document.createElement("pre");
    pre.textContent = options.detail;
    detail.append(pre);
    item.append(detail);
    const toggle = (): void => {
      detail.hidden = !detail.hidden;
      row.setAttribute("aria-expanded", String(!detail.hidden));
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  }
  streamAppend(root, item);
}

/** 等待中的行落定：优先用服务端时间差，取不到再退回本地走秒值。 */
function settleStreamRow(root: HTMLElement, key: string, endedServerMs: number | null): void {
  const state = streamState(root);
  const entry = state.pending.get(key);
  if (!entry) return;
  state.pending.delete(key);
  entry.element.classList.remove("is-waiting");
  const timeCell = entry.element.querySelector<HTMLElement>(".stream-elapsed");
  if (timeCell) {
    if (entry.sinceServerMs !== null && endedServerMs !== null) {
      timeCell.textContent = formatElapsed(endedServerMs - entry.sinceServerMs);
    }
    delete timeCell.dataset.elapsedSince;
  }
  const icon = entry.element.querySelector<HTMLElement>(".stream-row > i");
  if (icon) icon.className = "ph-fill ph-check-circle";
}

/** 一条领域事件 → 活动流内容；step.* 归上方时间线，不在这里重复。 */
function ingestStreamEvent(
  root: HTMLElement,
  event: { sequence?: number; type?: string; payload?: Record<string, unknown>; created_at?: string },
): void {
  const sequence = Number(event.sequence);
  const state = streamState(root);
  if (!Number.isFinite(sequence) || state.seen.has(sequence)) return;
  state.seen.add(sequence);
  const payload = event.payload ?? {};
  const eventMs = parseIso(event.created_at ?? null);

  switch (event.type) {
    case "run.node_changed": {
      const label = String(payload.label ?? payload.to ?? "");
      if (label) streamNarration(root, `进入「${label}」阶段。`);
      return;
    }
    case "run.status_changed": {
      const reason = String(payload.reason ?? "").trim();
      if (reason && reason !== "任务开始") streamNarration(root, `${reason}。`);
      return;
    }
    case "run.log": {
      const kind = String(payload.kind ?? "");
      if (kind === "thinking") {
        streamRow(root, {
          icon: "sparkle",
          title: `深度思考${STAGE_BY_PROMPT[String(payload.prompt_id)] ? ` · ${STAGE_BY_PROMPT[String(payload.prompt_id)]}` : ""}`,
          elapsedMs: Number(payload.elapsed_ms) || 0,
          detail: String(payload.text ?? ""),
        });
        return;
      }
      if (kind === "llm_call") {
        // 对话页不显示模型/接口等信息（与聊天回复的既有政策一致）：llm_call 过程事件
        // 不进活动流；用量透明度只体现在设置中心的本机用量记录。早退避免落入下方
        // 通用 run.log 兜底把 payload（含模型名）原样展示出来。
        return;
      }
      // 其他 run.log（未来的工具调用等）：原样以等宽详情展示
      streamRow(root, {
        icon: "terminal-window",
        title: String(payload.tool ?? payload.message ?? "运行日志"),
        detail: JSON.stringify(payload, null, 2),
        mono: true,
        elapsedMs: Number(payload.elapsed_ms) || undefined,
      });
      return;
    }
    case "approval.requested": {
      streamRow(root, {
        key: `approval:${String(payload.approval_id ?? sequence)}`,
        icon: "hand-palm",
        title: `等待确认：${String(payload.title ?? "") || "需要人工确认"}`,
        waitingSinceMs: eventMs,
      });
      return;
    }
    case "approval.resolved": {
      settleStreamRow(root, `approval:${String(payload.approval_id ?? "")}`, eventMs);
      return;
    }
    case "artifact.published": {
      const name = String(payload.name ?? payload.kind ?? "文件");
      streamRow(root, {
        icon: "file-arrow-down",
        title: `写入 ${name}`,
        detail: [`名称：${name}`, payload.kind ? `类型：${String(payload.kind)}` : "", payload.uri ? `位置：${String(payload.uri)}` : ""].filter(Boolean).join("\n"),
        mono: true,
      });
      return;
    }
    default:
      return;
  }
}

function renderAgent(root: HTMLElement, screen: ScreenId, view: ModelingWorkspaceView): void {
  const planning = isPlanningPhase(view);

  // 规划阶段通知页面层开启「开场思考」回复（与聊天消息同构的真实模型调用）。
  // 每次快照刷新都会走到这里，用 dataset 标记保证每个运行只广播一次。
  if (planning && root.dataset.openingAnnounced !== view.run_id) {
    root.dataset.openingAnnounced = view.run_id;
    document.dispatchEvent(new CustomEvent("omm:run-planning", { detail: { runId: view.run_id } }));
  }

  // 时序：先思考、后回应。开场分析（真实模型回复）结束前，首气泡内的摘要、
  // CTA 与已到达的执行过程行一起保持隐藏，结束后按「思考 → 摘要 → 过程」的
  // 顺序放行。页面层在回复落定时把 data-opening-state 置为 done 并广播
  // omm:opening-analysis-done。
  const stepsBlock = root.querySelector<HTMLElement>(".chat-scroll .assistant-block:not(.follow-up-reply)");
  const openingPending = stepsBlock?.dataset.openingState === "pending";
  const planParts = stepsBlock
    ? [...stepsBlock.querySelectorAll<HTMLElement>("[data-agent-summary], [data-agent-cta], .agent-stream")]
    : [];
  planParts.forEach(part => { part.hidden = Boolean(openingPending); });

  // 计划相位（planning → revealed）：执行计划面板的揭示时机与活动流的
  // 首气泡封口判定共用这一标记；运行中途刷新时首次渲染即 revealed，不重播动画。
  root.dataset.planPhase = planning || openingPending ? "planning" : "revealed";

  // 阶段计划不再画进聊天消息：输入框上方的可折叠「执行计划」面板承载，
  // 阶段数量与名称完全来自服务端 pages 投影。
  renderTaskTodos(root, {
    runId: view.run_id,
    planning: planning || Boolean(openingPending),
    items: view.pages.map(page => {
      const display = pageStatusForDisplay(view, page);
      const status: TaskTodoItem["status"] = display === "SUCCEEDED"
        ? "done"
        : display === "FAILED"
          ? "failed"
          : display === "RUNNING" || display === "WAITING_APPROVAL" || display === "PAUSED"
            ? "active"
            : "pending";
      return { key: page.key, label: page.label, status };
    }),
  });

  // 摘要区保持稳定的 title/paragraph 元素：文本未变时不重建，变化时打字机浮现，
  // 避免每次 SSE 刷新都重放动画。
  const renderCopy = (host: HTMLElement | null): void => {
    if (!host) return;
    // 规划阶段不显示「任务正在排队」占位摘要：思考态已由步骤区扫光行
    // 与开场思考回复承担，这里只清空演示残留。
    if (planning) {
      host.replaceChildren();
      host.dataset.agentState = view.agent.state;
      return;
    }
    let title = host.querySelector<HTMLElement>("strong[data-agent-title]");
    let paragraph = host.querySelector<HTMLElement>("p[data-agent-text]");
    if (!title || !paragraph) {
      title = document.createElement("strong");
      title.dataset.agentTitle = "true";
      paragraph = document.createElement("p");
      paragraph.dataset.agentText = "true";
      host.replaceChildren(title, paragraph);
    }
    title.textContent = view.agent.title;
    setStreamingText(paragraph, agentSummaryForScreen(screen, view));
    host.dataset.agentState = view.agent.state;
  };
  renderCopy(root.querySelector<HTMLElement>(".focused-agent-copy"));
  renderCopy(root.querySelector<HTMLElement>(".modeling-agent-copy[data-agent-summary]"));

  // 页面可能同时存在演示 CTA（真实运行时被 CSS 隐藏）与真实模式 CTA（data-live-only），
  // 统一写入同一后端动作，点击处理按就近的 [data-agent-cta] 生效。
  // 规划阶段整体隐藏：此时没有可执行动作，「等待任务开始」占位不再显示。
  const action = actionForScreen(screen, view);
  root.querySelectorAll<HTMLButtonElement>("[data-agent-cta]").forEach(cta => {
    cta.removeAttribute("data-go");
    cta.dataset.agentAction = action.kind;
    cta.textContent = action.label;
    cta.disabled = action.kind === "none";
    cta.hidden = planning || Boolean(openingPending);
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
  renderHeaderAttachments(root, view);
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
    // 演示态不携带任何运行身份：对话上下文一并解绑，避免残留上一任务。
    configureConversation(null);
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
  let conversationConfigured = false;

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

  // 开场分析落定：立即重渲染放行计划部分，不等下一次 SSE 刷新
  const onOpeningDone = (): void => {
    if (!disposed && currentView) renderWorkspace(root, currentScreen, currentView);
  };
  document.addEventListener("omm:opening-analysis-done", onOpeningDone);

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
      // 问题分析产出实际题目后服务端会自动重命名项目：侧栏「最近任务」跟着换名
      if (currentView && currentView.project_name !== view.project_name) {
        void hydrateRecentTasks();
      }
      currentView = view;
      // 首个快照到手即绑定对话归属：agent-chat 按 run 隔离上下文并从本机记录
      // 恢复历史；页面层随后把首条气泡换成该运行的真实题面并重建对话气泡。
      if (!conversationConfigured) {
        conversationConfigured = true;
        configureConversation(view.run_id, view.goal);
        document.dispatchEvent(new CustomEvent("omm:conversation-restore", {
          detail: { runId: view.run_id, goal: view.goal },
        }));
      }
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
        hideTaskTodos(root);
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
      const message = event as MessageEvent;
      const sequence = Number(message.lastEventId);
      if (Number.isSafeInteger(sequence) && sequence > 0) {
        lastSequence = Math.max(lastSequence ?? 0, sequence);
      }
      // SSE 帧携带完整事件体：喂给活动流（内部按 sequence 去重，重连不重复）
      try {
        ingestStreamEvent(root, JSON.parse(String(message.data)));
      } catch {
        // 单帧事件体异常只影响一行过程展示，不阻断视图刷新
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

  const onClick = (event: MouseEvent): void => {
    const target = event.target instanceof Element ? event.target : null;
    // 顶栏附件与「更多操作」：真实运行绑定后由这里接管，演示弹层不再出现。
    const headerFiles = target?.closest<HTMLElement>('[data-action="files"]');
    if (headerFiles && currentView) {
      event.preventDefault();
      event.stopPropagation();
      openAttachmentsDialog(currentView);
      return;
    }
    const headerMore = target?.closest<HTMLElement>('[data-action="more"]');
    if (headerMore && currentView) {
      event.preventDefault();
      event.stopPropagation();
      openTaskHeaderMenu(headerMore, currentView, () => void refresh(false));
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
    // 演示时间线阶段行的键盘导航（与点击同语义：纯导航，带运行身份）。
    const stepLink = target?.closest<HTMLElement>(".focused-activity-list [data-go]");
    const go = stepLink?.dataset.go as keyof typeof ROUTE_BY_GO | undefined;
    if (go && ROUTE_BY_GO[go] && currentView) {
      event.preventDefault();
      navigateTo(ROUTE_BY_GO[go]);
    }
  };

  root.addEventListener("click", onClick, true);
  root.addEventListener("keydown", onKeyDown, true);

  // 进行中阶段的耗时实时走秒（0.1s 精度由 formatElapsed 决定，500ms 刷新足够平滑）
  const elapsedTicker = window.setInterval(() => {
    root.querySelectorAll<HTMLElement>("[data-elapsed-since]").forEach(cell => {
      const since = Number(cell.dataset.elapsedSince);
      if (Number.isFinite(since)) cell.textContent = formatElapsed(Date.now() - since);
    });
  }, 500);

  const cleanup = (): void => {
    if (disposed) return;
    disposed = true;
    abortController.abort();
    eventSource?.close();
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    window.clearInterval(elapsedTicker);
    document.removeEventListener("omm:stage-shown", onStageShown);
    document.removeEventListener("omm:opening-analysis-done", onOpeningDone);
    root.removeEventListener("click", onClick, true);
    root.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("pagehide", cleanup);
  };
  activeCleanup = cleanup;
  window.addEventListener("pagehide", cleanup, { once: true });

  root.dataset.integrationState = "loading";
  root.querySelectorAll<HTMLElement>(
    '[data-go], [data-agent-cta], [data-action="continue-paper"], [data-action="download-all"], '
    + '[data-action="files"], [data-action="more"]',
  ).forEach(element => {
    element.dataset.workspaceLoading = "true";
    if (element instanceof HTMLButtonElement) element.disabled = true;
  });
  void refresh().then(() => {
    if (!disposed && currentView) connectEvents();
  });
}
