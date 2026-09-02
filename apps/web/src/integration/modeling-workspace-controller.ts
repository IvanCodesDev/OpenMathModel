import type { ModelingWorkspaceView } from "@openmathmodel/contracts";
import { mountComposerAttachments } from "../attachments/composer-attachments";
import { currentLocale, t } from "../i18n/locale";
import { configureConversation } from "./agent-chat";
import {
  applyChosenOption,
  REJECT_OPTION_ID,
  shouldOfferOptions,
  type ChosenApprovalOption,
} from "./approval-options";
import { notifyRunStatusChange } from "../notifications/desktop-notifications";
import { saveHistoryEnabled } from "../preferences/privacy-preferences";
import { forgetLastTask, rememberLastTask } from "../tasks/last-task-record";
import type { ScreenId } from "../types/screens";
import { renderMarkdown } from "../text/markdown";
import { typesetMath } from "../text/math-typeset";
import {
  modelingWorkspaceApi,
  WORKSPACE_EVENT_TYPES,
  WorkspaceApiError,
} from "./modeling-workspace-api";
import { hydrateRecentTasks } from "./recent-tasks";
import { appendPaperSection, preparePaperOutline, prepareStageTabs, renderStageContent } from "./stage-content";
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

/** 用户在审批门里手选的那一项（ADR-0013 第 14 项）；null 表示沿用服务端预选。 */
let chosenApprovalOption: ChosenApprovalOption | null = null;

function withChosenOption(
  view: ModelingWorkspaceView,
  action: ModelingWorkspaceView["agent"]["action"],
): ModelingWorkspaceView["agent"]["action"] {
  return applyChosenOption(view.pending_approval, action, chosenApprovalOption);
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
    return withChosenOption(view, {
      ...view.agent.action,
      label: "确认 Agent 当前方案并继续",
    });
  }
  return withChosenOption(view, view.agent.action);
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
  "data_preparation.default": "数据准备",
  // 沙盒会话（H3）：清洗/实验的多轮写码跑码会话，prompt_id 即会话标签
  "data_cleaning.sandbox": "数据清洗",
  "model_planning.default": "建模方案",
  "experiment_code.default": "实验代码",
  "experiment_code.sandbox": "实验执行",
  "validating.default": "结果验证",
  // 整篇回退路径（总编规划失败时的单次生成）
  "paper_writing.default": "论文撰写",
  // 论文分章多轮管线按环节区分：一个阶段里的多次调用各自说清在干什么
  "paper_outline.default": "论文骨架规划",
  "paper_section.default": "论文章节写作",
  "paper_finalize.default": "论文统稿收口",
};

const STAGE_OUTPUT_LABELS: Record<string, string> = {
  PROBLEM_ANALYSIS: "题意解析",
  DATA_PREPARATION: "数据准备",
  MODEL_PLANNING: "建模方案",
  EXPERIMENTING: "实验运行",
  VALIDATING: "结果验证",
  PAPER_WRITING: "论文撰写",
};

/** 阶段产出 → 可展开详情的纯文本（执行轨迹的「看到智能体做了什么」主体）。 */
function formatStageOutputs(node: string, outputs: Record<string, unknown>): string {
  const str = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
  const list = (value: unknown): string[] =>
    Array.isArray(value) ? value.map(item => str(item)).filter(Boolean) : [];
  const section = (label: string, items: string[]): string =>
    items.length ? `${label}：\n${items.map(item => `- ${item}`).join("\n")}` : "";
  const parts: string[] = [];
  if (node === "PROBLEM_ANALYSIS") {
    if (str(outputs.title)) parts.push(`标题：${str(outputs.title)}`);
    if (str(outputs.problem_type)) parts.push(`问题类型：${str(outputs.problem_type)}`);
    parts.push(section("目标问题", list(outputs.objectives)));
    parts.push(section("约束条件", list(outputs.constraints)));
    parts.push(section("数据需求", list(outputs.data_requirements)));
    parts.push(section("关键假设", list(outputs.key_assumptions)));
    const outline = Array.isArray(outputs.plan_outline)
      ? (outputs.plan_outline as Array<Record<string, unknown>>)
        .map(item => str(item?.text)).filter(Boolean)
      : [];
    parts.push(section("执行计划", outline));
  } else if (node === "DATA_PREPARATION") {
    if (str(outputs.profile_summary)) parts.push(`数据画像：${str(outputs.profile_summary)}`);
    const datasets = Array.isArray(outputs.datasets)
      ? (outputs.datasets as Array<Record<string, unknown>>)
        .map(item => [str(item?.name), str(item?.source)].filter(Boolean).join("｜")).filter(Boolean)
      : [];
    parts.push(section("数据清单", datasets));
    parts.push(section("准备步骤", list(outputs.preparation_steps)));
    if (str(outputs.missing_value_strategy)) parts.push(`缺失值策略：${str(outputs.missing_value_strategy)}`);
    if (str(outputs.outlier_strategy)) parts.push(`异常值策略：${str(outputs.outlier_strategy)}`);
    parts.push(section("衍生变量", list(outputs.derived_features)));
  } else if (node === "MODEL_PLANNING") {
    const plans = Array.isArray(outputs.plans)
      ? (outputs.plans as Array<Record<string, unknown>>)
      : [];
    for (const plan of plans) {
      const head = `方案 ${str(plan?.id)}｜${str(plan?.name)}：${str(plan?.approach)}`;
      const steps = section("  步骤", list(plan?.steps));
      const risks = section("  风险", list(plan?.risks));
      parts.push([head, steps, risks].filter(Boolean).join("\n"));
    }
    if (str(outputs.recommended_plan_id)) parts.push(`推荐方案：${str(outputs.recommended_plan_id)}`);
    if (str(outputs.rationale)) parts.push(`推荐理由：${str(outputs.rationale)}`);
  } else if (node === "EXPERIMENTING") {
    if (str(outputs.approach_summary)) parts.push(`实现思路：${str(outputs.approach_summary)}`);
    const metrics = outputs.metrics && typeof outputs.metrics === "object"
      ? Object.entries(outputs.metrics as Record<string, unknown>).map(([key, value]) => `${key} = ${String(value)}`)
      : [];
    parts.push(section("核心指标", metrics));
    // stdout_tail 已由节点侧截到 2000 字（nodes._STDOUT_TAIL_CHARS），
    // 这里不再二次裁剪：展开详情就是给用户看完整尾部的地方
    const stdout = str(outputs.stdout_tail);
    if (stdout) parts.push(`运行输出（尾部）：\n${stdout}`);
  } else if (node === "VALIDATING") {
    if (str(outputs.verdict)) parts.push(`总体结论：${str(outputs.verdict)}`);
    const checks = Array.isArray(outputs.checks)
      ? (outputs.checks as Array<Record<string, unknown>>)
        .map(item => [str(item?.name), str(item?.result), str(item?.note)].filter(Boolean).join("｜")).filter(Boolean)
      : [];
    parts.push(section("逐项检查", checks));
    parts.push(section("主要风险", list(outputs.risks)));
    if (str(outputs.validation_summary)) parts.push(`检验结论：${str(outputs.validation_summary)}`);
  } else if (node === "PAPER_WRITING") {
    if (str(outputs.title)) parts.push(`标题：${str(outputs.title)}`);
    if (str(outputs.abstract)) parts.push(`摘要：${str(outputs.abstract)}`);
    const keywords = list(outputs.keywords);
    if (keywords.length) parts.push(`关键词：${keywords.join("；")}`);
    const sections = Array.isArray(outputs.sections)
      ? (outputs.sections as Array<Record<string, unknown>>)
        .map(item => str(item?.heading)).filter(Boolean)
      : [];
    parts.push(section("章节", sections));
  }
  return parts.filter(Boolean).join("\n\n");
}

interface PendingStreamRow {
  element: HTMLElement;
  sinceServerMs: number | null;
  /** 行标题原文（重试计数、落定时的后缀都以它为基底）。 */
  titleBase: string;
  /** 同一 key 的第几次尝试（模型调用失败自动重试时递增，行复用不堆叠）。 */
  attempts: number;
  /** 实时生成区（llm_delta 事件流入的可展开详情），首个增量到达时创建。 */
  livePre?: HTMLElement;
  /** 实时生成区的文本节点：打字机逐字往里追加（appendData 摊销 O(1)）。 */
  liveText?: Text;
  /** 还没打上屏的增量（服务端按秒批量下发，前端逐字匀速消化）。 */
  liveQueue?: string;
  liveTimer?: number;
}

interface AgentStreamState {
  host: HTMLElement | null;
  seen: Set<number>;
  /** 待落定的行（模型调用、等待确认等）：key → 行元素与状态 */
  pending: Map<string, PendingStreamRow>;
  /** 当前处理的是首连回放的历史事件（决定落点，见 resolveStreamHost）。 */
  replaying: boolean;
}

const streamByRoot = new WeakMap<HTMLElement, AgentStreamState>();

function streamState(root: HTMLElement): AgentStreamState {
  let state = streamByRoot.get(root);
  if (!state) {
    state = { host: null, seen: new Set(), pending: new Map(), replaying: false };
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
 *  - 首条消息封口后：写入对话末尾的执行轨迹块，与后续对话按时间交替；
 *  - 首连回放的历史事件例外：它们属于「过去」，即使首气泡此刻已经封口也要落回
 *    首气泡的活动流，否则重进任务时整段执行轨迹会排到后来的对话气泡后面。 */
function resolveStreamHost(root: HTMLElement, state: AgentStreamState): HTMLElement | null {
  const scroll = root.querySelector<HTMLElement>(".chat-scroll");
  if (scroll && !state.replaying) {
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
  const state = streamState(root);
  const host = resolveStreamHost(root, state);
  if (!host) return;
  const scroll = root.querySelector<HTMLElement>(".chat-scroll, .focused-agent-scroll");
  const stick = scroll
    ? scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120
    : false;
  // 首连回放的历史行不重播入场动画：重进任务时执行轨迹应当「本来就在」，
  // 而不是一条条动画重演；只有实时新事件才浮现入场。
  if (!state.replaying) node.classList.add("stream-in");
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
  /** 同类行归组（用户要求：大批同类行平铺难看）：连续同组行折进一个
   *  可展开的组行，组标题计数递增；被其他行隔断则另起新组，时序不乱。 */
  group?: StreamRowGroup;
}

interface StreamRowGroup {
  key: string;
  title: string;
  icon: string;
}

/** 归组落行：尾部已是同组容器则续写；尾部是同组散行则升格成组容器
 *  （首条同类行保持散行——只有一条时套组反而多一层）；否则按散行落下。 */
function appendGrouped(root: HTMLElement, item: HTMLElement, group: StreamRowGroup): void {
  const state = streamState(root);
  const host = resolveStreamHost(root, state);
  if (!host) return;
  const tail = host.lastElementChild instanceof HTMLElement ? host.lastElementChild : null;
  let body = tail?.classList.contains("stream-group") && tail.dataset.groupKey === group.key
    ? tail.querySelector<HTMLElement>(".stream-group-body")
    : null;
  if (!body && tail?.classList.contains("stream-item") && tail.dataset.groupKey === group.key) {
    const container = document.createElement("div");
    container.className = "stream-item stream-group";
    container.dataset.groupKey = group.key;
    container.innerHTML = `
      <div class="stream-row is-expandable" role="button" tabindex="0" aria-expanded="false">
        <i class="ph ph-${group.icon}" aria-hidden="true"></i>
        <span class="stream-title"><span></span><span class="stream-group-count"></span></span>
        <i class="ph ph-caret-down stream-chevron" aria-hidden="true"></i>
      </div>
      <div class="stream-group-body" hidden></div>`;
    container.querySelector<HTMLElement>(".stream-title > span")!.textContent = group.title;
    const row = container.querySelector<HTMLElement>(".stream-row")!;
    const groupBody = container.querySelector<HTMLElement>(".stream-group-body")!;
    const toggle = (): void => {
      groupBody.hidden = !groupBody.hidden;
      row.setAttribute("aria-expanded", String(!groupBody.hidden));
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
    if (!state.replaying) container.classList.add("stream-in");
    host.append(container);
    groupBody.append(tail); // 把先落下的散行收编进组，保持发生顺序
    body = groupBody;
  }
  if (!body) {
    item.dataset.groupKey = group.key;
    streamAppend(root, item);
    return;
  }
  const scroll = root.querySelector<HTMLElement>(".chat-scroll, .focused-agent-scroll");
  const stick = scroll
    ? scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120
    : false;
  body.append(item);
  const badge = body.parentElement?.querySelector<HTMLElement>(".stream-group-count");
  if (badge) badge.textContent = `×${body.children.length}`;
  if (stick && scroll) scroll.scrollTop = scroll.scrollHeight;
}

/** 给已有过程行补挂可展开详情区（创建时或实时增量首次到达时调用）。 */
function attachRowDetail(
  item: HTMLElement,
  options: { mono?: boolean; live?: boolean; open?: boolean } = {},
): HTMLElement {
  const row = item.querySelector<HTMLElement>(".stream-row")!;
  const detail = document.createElement("div");
  detail.className = `stream-detail${options.mono ? " is-mono" : ""}${options.live ? " is-live" : ""}`;
  detail.hidden = !options.open;
  const pre = document.createElement("pre");
  detail.append(pre);
  item.append(detail);
  row.classList.add("is-expandable");
  row.setAttribute("role", "button");
  row.tabIndex = 0;
  row.setAttribute("aria-expanded", String(Boolean(options.open)));
  row.insertAdjacentHTML("beforeend", '<i class="ph ph-caret-down stream-chevron" aria-hidden="true"></i>');
  const toggle = (): void => {
    detail.hidden = !detail.hidden;
    row.setAttribute("aria-expanded", String(!detail.hidden));
    // 生成中途展开实时区：直接跳到最新内容处继续跟随
    if (options.live && !detail.hidden) pre.scrollTop = pre.scrollHeight;
  };
  row.addEventListener("click", toggle);
  row.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggle();
    }
  });
  return pre;
}

// ── 实时生成区的打字机：服务端按秒批量下发增量，前端逐字匀速上屏 ────────────

const LIVE_TYPE_TICK_MS = 24;

function stopLiveTyping(entry: PendingStreamRow): void {
  if (entry.liveTimer !== undefined) {
    window.clearInterval(entry.liveTimer);
    entry.liveTimer = undefined;
  }
}

/** 把积压的增量一次性全部上屏（落定、行复用重置前调用）。 */
function flushLiveTyping(entry: PendingStreamRow): void {
  stopLiveTyping(entry);
  if (entry.liveText && entry.liveQueue) {
    entry.liveText.appendData(entry.liveQueue);
    entry.liveQueue = "";
  }
}

function liveTypeTick(entry: PendingStreamRow): void {
  if (!entry.liveText || !entry.liveQueue) {
    stopLiveTyping(entry);
    return;
  }
  // 逐字输出；积压过大时按比例加速追赶（约 1.2 秒内消化完当前积压），
  // 视觉上仍是打字节奏而不是整段闪现。
  const take = Math.max(1, Math.round(entry.liveQueue.length / 50));
  entry.liveText.appendData(entry.liveQueue.slice(0, take));
  entry.liveQueue = entry.liveQueue.slice(take);
  const detail = entry.livePre?.parentElement;
  if (entry.livePre && detail instanceof HTMLElement && !detail.hidden) {
    entry.livePre.scrollTop = entry.livePre.scrollHeight;
  }
  if (!entry.liveQueue) stopLiveTyping(entry);
}

function enqueueLiveDelta(entry: PendingStreamRow, text: string): void {
  if (!entry.livePre) {
    // 默认折叠：不打扰主流程，想看的用户自己展开（展开后自动吸底跟随）
    entry.livePre = attachRowDetail(entry.element, { live: true, open: false });
    entry.liveText = document.createTextNode("");
    entry.livePre.append(entry.liveText);
  }
  entry.liveQueue = (entry.liveQueue ?? "") + text;
  if (prefersReducedMotion() || document.documentElement.dataset.reduceMotion === "on") {
    flushLiveTyping(entry);
    return;
  }
  if (entry.liveTimer === undefined) {
    entry.liveTimer = window.setInterval(() => liveTypeTick(entry), LIVE_TYPE_TICK_MS);
  }
}

/** 标题入场扫光：与「思考中…」同款渐变从左到右扫两遍后恢复常规配色。 */
function shineTitleOnce(title: HTMLElement): void {
  if (prefersReducedMotion() || document.documentElement.dataset.reduceMotion === "on") return;
  title.classList.add("title-shine-once");
  title.addEventListener("animationend", () => title.classList.remove("title-shine-once"), { once: true });
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
  const title = item.querySelector<HTMLElement>(".stream-title")!;
  title.textContent = options.title;
  const timeCell = item.querySelector<HTMLElement>(".stream-elapsed")!;
  if (options.waitingSinceMs !== undefined) {
    // 进行中的行：标题持续扫光（与「思考中…」同一动效语言），落定时移除
    title.classList.add("thinking-shimmer");
    // 走秒锚定事件的服务端时间：刷新重进时在途调用接着真实起点累计，
    // 不再每次进页从 0 重走（钟差导致事件时间超前本地时取本地，保证非负）
    const since = Math.min(options.waitingSinceMs ?? Date.now(), Date.now());
    timeCell.dataset.elapsedSince = String(since);
    timeCell.textContent = formatElapsed(Date.now() - since);
    if (options.key) {
      state.pending.set(options.key, {
        element: item,
        sinceServerMs: options.waitingSinceMs ?? null,
        titleBase: options.title,
        attempts: 1,
      });
    }
  } else if (options.elapsedMs !== undefined) {
    timeCell.textContent = formatElapsed(options.elapsedMs);
  }
  // 已落定的新行（阶段产出、沙箱执行等）：实时到达时标题扫光入场；
  // 首连回放的历史行不重播动画。
  if (options.waitingSinceMs === undefined && !state.replaying) shineTitleOnce(title);
  if (options.detail) {
    const pre = attachRowDetail(item, { mono: options.mono });
    pre.textContent = options.detail;
  }
  if (options.group) {
    appendGrouped(root, item, options.group);
  } else {
    streamAppend(root, item);
  }
}

/** 移除等待中的行：被信息更完整的行整体替换时用（与「落定」不同，不保留元素）。 */
function dropPendingStreamRow(root: HTMLElement, key: string): PendingStreamRow | undefined {
  const state = streamState(root);
  const entry = state.pending.get(key);
  if (!entry) return undefined;
  state.pending.delete(key);
  stopLiveTyping(entry);
  entry.element.remove();
  return entry;
}

/** 等待中的行落定：优先用服务端时间差，取不到再退回本地走秒值。 */
function settleStreamRow(
  root: HTMLElement,
  key: string,
  endedServerMs: number | null,
  options: { failed?: boolean; title?: string } = {},
): void {
  const state = streamState(root);
  const entry = state.pending.get(key);
  if (!entry) return;
  state.pending.delete(key);
  flushLiveTyping(entry);
  entry.element.classList.remove("is-waiting");
  const title = entry.element.querySelector<HTMLElement>(".stream-title");
  if (title) {
    title.classList.remove("thinking-shimmer");
    if (options.title) {
      title.textContent = options.title;
    } else if (entry.attempts > 1 && !options.failed) {
      title.textContent = `${entry.titleBase}（第 ${entry.attempts} 次尝试成功）`;
    } else {
      title.textContent = entry.titleBase;
    }
  }
  const timeCell = entry.element.querySelector<HTMLElement>(".stream-elapsed");
  if (timeCell) {
    if (entry.sinceServerMs !== null && endedServerMs !== null) {
      timeCell.textContent = formatElapsed(endedServerMs - entry.sinceServerMs);
    }
    delete timeCell.dataset.elapsedSince;
  }
  const icon = entry.element.querySelector<HTMLElement>(".stream-row > i");
  if (icon) icon.className = options.failed ? "ph-fill ph-warning-circle" : "ph-fill ph-check-circle";
  // 实时生成区随落定收起（内容保留，可点击回看）
  const live = entry.element.querySelector<HTMLElement>(".stream-detail.is-live");
  if (live && !live.hidden) {
    live.hidden = true;
    entry.element.querySelector(".stream-row")?.setAttribute("aria-expanded", "false");
  }
}

/** 把所有进行中的模型调用行落定（步骤失败、运行终态时调用，不碰审批行）。 */
function settlePendingLlmRows(
  root: HTMLElement,
  endedServerMs: number | null,
  failed: boolean,
): void {
  const state = streamState(root);
  for (const key of [...state.pending.keys()]) {
    if (!key.startsWith("llm:")) continue;
    const entry = state.pending.get(key);
    settleStreamRow(root, key, endedServerMs, {
      failed,
      ...(failed && entry ? { title: `${entry.titleBase}（本次调用中断）` } : {}),
    });
  }
}

/** 一条领域事件 → 活动流内容；step.* 归上方时间线，不在这里重复。
 *
 * ``replay`` 只影响落点，处理逻辑与实时事件完全一致。标记打在 state 上而不是
 * 逐层透传：streamNarration / streamRow 只在本函数内调用，且整条链路同步无重入。
 */
function ingestStreamEvent(
  root: HTMLElement,
  event: { sequence?: number; type?: string; payload?: Record<string, unknown>; created_at?: string },
  replay = false,
): void {
  const sequence = Number(event.sequence);
  const state = streamState(root);
  if (!Number.isFinite(sequence) || state.seen.has(sequence)) return;
  state.seen.add(sequence);
  state.replaying = replay;
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
      // 运行到达终态：所有还在走秒的模型调用行一并落定，不留永远转圈的僵尸行
      const to = String(payload.to ?? "");
      if (to === "FAILED" || to === "CANCELLED") settlePendingLlmRows(root, eventMs, true);
      if (to === "COMPLETED") settlePendingLlmRows(root, eventMs, false);
      return;
    }
    case "run.log": {
      const kind = String(payload.kind ?? "");
      if (kind === "llm_call_started") {
        // 调用开始即出走秒行：一次模型调用动辄一两分钟，没有这行的话调用期间
        // 活动流完全静默、结束时整批闪现。结束事件到达时被 thinking 行整体
        // 替换（推理模型）或就地落定（无思考内容的模型）。
        const key = `llm:${String(payload.prompt_id)}`;
        const stage = STAGE_BY_PROMPT[String(payload.prompt_id)];
        // repair = 上次输出没过结构校验、这次带着错误反馈重生成：如实标注
        const title = `深度思考${stage ? ` · ${stage}` : ""}${payload.repair === true ? "（修复输出格式）" : ""}`;
        // 同一提示词的上一次调用还没落定（调用失败重试、进程重启续跑、历史
        // 回放的孤儿行）：复用同一行推进尝试计数，绝不堆出一排相同的走秒行。
        const existing = state.pending.get(key);
        if (existing) {
          existing.attempts += 1;
          existing.sinceServerMs = eventMs;
          const titleNode = existing.element.querySelector<HTMLElement>(".stream-title");
          if (titleNode) titleNode.textContent = `${title}（第 ${existing.attempts} 次尝试）`;
          const timeCell = existing.element.querySelector<HTMLElement>(".stream-elapsed");
          if (timeCell) {
            // 与新行同规则：锚定事件时间，重放的重试行也从真实起点累计
            const since = Math.min(eventMs ?? Date.now(), Date.now());
            timeCell.dataset.elapsedSince = String(since);
            timeCell.textContent = formatElapsed(Date.now() - since);
          }
          // 新一次尝试是全新生成：上次失败尝试的半截增量不再保留
          stopLiveTyping(existing);
          existing.liveQueue = "";
          if (existing.liveText) existing.liveText.data = "";
          delete existing.element.dataset.lastChannel;
          return;
        }
        streamRow(root, {
          key,
          icon: "sparkle",
          title,
          waitingSinceMs: eventMs,
        });
        return;
      }
      if (kind === "llm_delta") {
        // 模型生成的实时增量：流入走秒行的可展开详情（默认折叠）。实时逐字
        // 打上屏；历史回放直接批量归位——已落定调用的增量随 thinking 行整体
        // 替换不重复，在途调用刷新前已生成的内容则原样找回（不再整段丢失）。
        const entry = state.pending.get(`llm:${String(payload.prompt_id)}`);
        if (!entry) return;
        const channel = String(payload.channel ?? "");
        // 思考与正文之间空一行，阅读时能分清两段
        const glue = channel === "text" && entry.element.dataset.lastChannel === "reasoning"
          ? "\n\n"
          : "";
        entry.element.dataset.lastChannel = channel;
        enqueueLiveDelta(entry, glue + String(payload.text ?? ""));
        if (replay) flushLiveTyping(entry);
        return;
      }
      if (kind === "llm_call_failed") {
        // 一次调用失败（超时/限流/网关错误）：行保留并标注重试中；若这是
        // 最后一次尝试，随后的 step.failed / 运行终态会把它彻底落定。
        const entry = state.pending.get(`llm:${String(payload.prompt_id)}`);
        if (entry) {
          const titleNode = entry.element.querySelector<HTMLElement>(".stream-title");
          if (titleNode) titleNode.textContent = `${entry.titleBase}（上次调用中断，自动重试中）`;
        }
        return;
      }
      if (kind === "thinking") {
        const key = `llm:${String(payload.prompt_id)}`;
        const dropped = dropPendingStreamRow(root, key);
        const stage = STAGE_BY_PROMPT[String(payload.prompt_id)];
        const retries = dropped && dropped.attempts > 1 ? `（第 ${dropped.attempts} 次尝试成功）` : "";
        streamRow(root, {
          icon: "sparkle",
          title: `深度思考${stage ? ` · ${stage}` : ""}${retries}`,
          elapsedMs: Number(payload.elapsed_ms) || 0,
          detail: String(payload.text ?? ""),
        });
        return;
      }
      if (kind === "llm_call") {
        // 无思考内容的模型不会发 thinking：调用摘要到达时就地落定走秒行。
        // 对话页不显示模型/接口等信息（与聊天回复的既有政策一致）：llm_call 过程事件
        // 本身不进活动流；用量透明度只体现在设置中心的本机用量记录。早退避免落入下方
        // 通用 run.log 兜底把 payload（含模型名）原样展示出来。
        settleStreamRow(root, `llm:${String(payload.prompt_id)}`, eventMs);
        return;
      }
      if (kind === "materials_ingested") {
        // 题意解析开始前读取了哪些材料：用户最需要确认「智能体真的看了我的文件」
        const attachments = Array.isArray(payload.attachments)
          ? (payload.attachments as unknown[]).map(item => String(item ?? "")).filter(Boolean)
          : [];
        const references = Array.isArray(payload.references)
          ? (payload.references as unknown[]).map(item => String(item ?? "")).filter(Boolean)
          : [];
        const total = attachments.length + references.length;
        if (!total) return;
        streamRow(root, {
          icon: "paperclip",
          title: `已读取题目附件与引用材料 ×${total}`,
          detail: [...attachments, ...references.map(titleText => `@${titleText}`)].join("\n"),
        });
        return;
      }
      if (kind === "task_renamed" || kind === "budget_limit") {
        // 有现成人话 message 的运营事件：以叙述行呈现，不落进原始 JSON 兜底
        const message = String(payload.message ?? "").trim();
        if (message) streamNarration(root, message.endsWith("。") ? message : `${message}。`);
        return;
      }
      if (payload.tool === "python_run") {
        // 实验沙箱执行：标题说人话，输入/输出摘要进可展开详情。
        // 失败时 failure_detail 携带 stderr 尾部（含 traceback）——没有它用户只能看到
        // 「python exited with code 1」一行，无从判断崩在哪。
        const failed = payload.status !== "succeeded";
        streamRow(root, {
          icon: failed ? "warning-circle" : "terminal-window",
          title: failed ? "实验代码执行失败（准备修复重试）" : "已在沙箱执行实验代码",
          elapsedMs: Number(payload.duration_ms) || undefined,
          detail: [
            `状态：${String(payload.status ?? "")}`,
            payload.input_summary ? `输入：${String(payload.input_summary)}` : "",
            payload.output_summary ? `输出：${String(payload.output_summary)}` : "",
            payload.failure_detail ? `报错详情：\n${String(payload.failure_detail)}` : "",
          ].filter(Boolean).join("\n"),
          mono: true,
        });
        return;
      }
      // 论文分章直播：骨架事件预挂大纲，章节事件把正文实时推进编辑器
      if (kind === "paper_outline") {
        const headings = Array.isArray(payload.headings)
          ? (payload.headings as unknown[]).map(item => String(item ?? ""))
          : [];
        streamNarration(root, `论文骨架已定：共 ${Number(payload.total) || headings.length} 章。`);
        preparePaperOutline(root, {
          total: Number(payload.total) || headings.length,
          headings,
        });
        return;
      }
      if (kind === "paper_section") {
        const index = Number(payload.index) || 0;
        const total = Number(payload.total) || 0;
        const heading = String(payload.heading ?? "");
        streamRow(root, {
          icon: "file-text",
          title: `已完成章节 ${index}/${total}：${heading}`,
        });
        appendPaperSection(root, {
          index,
          total,
          heading,
          content: String(payload.content ?? ""),
        }, !replay);
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
    case "step.succeeded": {
      // 阶段完成：先输出智能体的进度叙述正文（progress_note，直接可读、
      // 不折叠——叙述与过程行交替），再挂结构化「阶段产出」可展开明细。
      // 模拟节点只有 {label}，两者都为空则不渲染。
      const node = String(payload.node ?? "");
      const label = STAGE_OUTPUT_LABELS[node];
      const outputs = (payload.outputs ?? {}) as Record<string, unknown>;
      if (!label) return;
      const note = typeof outputs.progress_note === "string" ? outputs.progress_note.trim() : "";
      if (note) {
        const paragraph = document.createElement("div");
        paragraph.className = "analysis-copy stream-note";
        paragraph.innerHTML = renderMarkdown(note);
        typesetMath(paragraph);
        streamAppend(root, paragraph);
      }
      const detail = formatStageOutputs(node, outputs);
      if (detail) {
        streamRow(root, {
          icon: "clipboard-text",
          title: `阶段产出 · ${label}`,
          detail,
        });
      }
      return;
    }
    case "step.failed": {
      // 阶段失败（含进程中断后的修复落定）：进行中的模型调用行一并按失败收尾，
      // 后续重试会开新的走秒行。
      settlePendingLlmRows(root, eventMs, true);
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
      // 一个阶段常一口气写十几个文件：连续的「写入」行归进一个可展开的
      // 组行（写入产物文件 ×N），活动流不再被同类小标题刷屏。
      const name = String(payload.name ?? payload.kind ?? "文件");
      streamRow(root, {
        icon: "file-arrow-down",
        title: `写入 ${name}`,
        detail: [`名称：${name}`, payload.kind ? `类型：${String(payload.kind)}` : "", payload.uri ? `位置：${String(payload.uri)}` : ""].filter(Boolean).join("\n"),
        mono: true,
        group: { key: "artifacts", title: "写入产物文件", icon: "files" },
      });
      return;
    }
    default:
      return;
  }
}

/**
 * 审批门的选项列表（ADR-0013 第 14 项）：把选项摆在 CTA 上方让用户挑重做起点。
 *
 * 摆不摆由 `shouldOfferOptions` 定；这里只负责渲染。修订门必须摆出来的理由见
 * ADR §3：从问题分析重做和从论文撰写重做差一个数量级的花费，服务端只把建议项标成
 * recommended、绝不替用户默选，不给改选入口的话「知情同意」就是一句空话。
 */
function renderApprovalOptions(
  root: HTMLElement,
  view: ModelingWorkspaceView,
  action: ModelingWorkspaceView["agent"]["action"],
  hidden: boolean,
): void {
  const approval = view.pending_approval;
  const show = shouldOfferOptions(approval, action);
  const selectedId = action.kind === "approve" ? action.option_id : null;

  root.querySelectorAll<HTMLButtonElement>("[data-agent-cta]").forEach(cta => {
    const previous = cta.previousElementSibling;
    const existing = previous instanceof HTMLElement && previous.dataset.approvalOptions !== undefined
      ? previous
      : null;
    if (!show || approval === null) {
      existing?.remove();
      return;
    }
    const host = existing ?? document.createElement("div");
    host.className = "approval-options";
    host.dataset.approvalOptions = approval.id;
    host.hidden = hidden;
    // 同一道门重复渲染（SSE 每来一帧都会走到这里）时只更新选中态，不重建 DOM——
    // 重建会打断用户正在用键盘走的焦点。
    if (existing && existing.dataset.approvalOptions === approval.id
      && existing.childElementCount === approval.options.length + 1) {
      existing.querySelectorAll<HTMLButtonElement>("[data-approval-option]").forEach(button => {
        const active = button.dataset.approvalOption === selectedId;
        button.classList.toggle("is-selected", active);
        button.setAttribute("aria-checked", String(active));
      });
      return;
    }
    const legend = document.createElement("p");
    legend.className = "approval-options-legend";
    legend.textContent = approval.title;
    const children: HTMLElement[] = [legend];
    approval.options.forEach(option => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.approvalOption = option.id;
      button.setAttribute("role", "radio");
      const active = option.id === selectedId;
      button.setAttribute("aria-checked", String(active));
      button.classList.toggle("is-selected", active);
      button.classList.toggle("is-withdraw", option.id === REJECT_OPTION_ID);
      const label = document.createElement("strong");
      label.textContent = option.label;
      button.append(label);
      if (option.recommended) {
        const badge = document.createElement("span");
        badge.className = "approval-option-badge";
        badge.textContent = t("建议");
        button.append(badge);
      }
      if (option.description) {
        const description = document.createElement("small");
        description.textContent = option.description;
        button.append(description);
      }
      children.push(button);
    });
    host.replaceChildren(...children);
    host.setAttribute("role", "radiogroup");
    host.setAttribute("aria-label", approval.title);
    if (!existing) cta.insertAdjacentElement("beforebegin", host);
  });
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
  // 条目文案优先用服务端派生的本任务计划短句（问题分析的 plan_outline，
  // 方案确认后实验条目细化为选中方案），未产出时回退固定阶段名；
  // 勾选状态始终来自 pages.status（引擎执行事实），文本变化不影响状态语义。
  // 「正在思考并规划…」只在真的有生成在进行时出现：排队等待执行器（QUEUED
  // 且开场分析已结束）不是思考，此时整个面板隐藏，避免拿思考态糊弄等待。
  if (view.run_status === "QUEUED" && !openingPending) {
    hideTaskTodos(root);
  } else {
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
        return { key: page.key, label: page.plan_text ?? page.label, status };
      }),
    });
  }

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
  renderApprovalOptions(root, view, action, planning || Boolean(openingPending));
  root.querySelectorAll<HTMLButtonElement>("[data-agent-cta]").forEach(cta => {
    cta.removeAttribute("data-go");
    cta.dataset.agentAction = action.kind;
    cta.textContent = action.label;
    // 多选项审批门没选中任何一项时按钮点了也是 no-op（服务端要求显式 option_id），
    // 与其让用户点空，不如禁用它、由上方选项列表引导先选一项。
    cta.disabled = action.kind === "none"
      || (action.kind === "approve" && action.option_id === null);
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
    // 页内锚点（论文大纲 #section-N）解析后的 pathname 恰好等于当前工作台路由，
    // 改写会把目录跳转变成整页导航，必须跳过。
    if (link.getAttribute("href")?.startsWith("#")) return;
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
  // 真实运行的子分页纪律：纯演示分页撤走，可填充分页待真实内容就绪再放出
  prepareStageTabs(root);
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
  // 首连回放的边界：连接那一刻快照里的最大事件序号。序号不超过它的都是历史，
  // 用来决定活动流的落点（见 resolveStreamHost）。连接后冻结，不随刷新推进，
  // 否则实时事件也会被当成历史挤回首气泡。
  let replayThrough: number | undefined;
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
        // 同一运行第二次进入同一状态（G2→G1 两道门、修订第 2 轮完成）要各自提醒
        approvalId: view.pending_approval?.id ?? null,
        eventSequence: view.latest_event_sequence,
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
      renderWorkspace(root, currentScreen, view);
      // 五类页面正文（数据画像/方案/实验/论文/交付）：拉取失败或阶段未产出
      // 时保留演示模板，绝不阻断主视图刷新；渲染器内部按 updated_at 幂等。
      void modelingWorkspaceApi.getStageOutputs(runId, abortController.signal)
        .then(outputs => {
          if (!disposed) renderStageContent(root, outputs);
        })
        .catch(() => undefined);
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

  /**
   * 首屏一次性水合历史事件：REST 分页拉全量，同步喂给活动流（单个任务内上屏、
   * 不重演入场动画），SSE 随后只接实时增量。此前历史靠 SSE 从头逐帧重放，
   * 重进任务时整段轨迹一条条动画加载（2026-08-31 用户报障）。
   * 拉取失败时 lastSequence 维持原样，SSE 按旧路径从头回放兜底，功能不减。
   */
  const hydrateHistory = async (): Promise<void> => {
    try {
      let after = 0;
      for (;;) {
        const { items } = await modelingWorkspaceApi.listRunEvents(runId, after, abortController.signal);
        if (disposed || items.length === 0) return;
        for (const item of items) ingestStreamEvent(root, item, true);
        const tail = items[items.length - 1]?.sequence ?? 0;
        if (tail <= after) return;
        after = tail;
        lastSequence = Math.max(lastSequence ?? 0, tail);
        if (items.length < 1000) return;
      }
    } catch {
      // 历史拉取失败（网络/权限）：退回 SSE 全量回放，只是入场略慢
    }
  };

  const connectEvents = (): void => {
    if (disposed || streamEnded) return;
    // 活动流完全由事件重建：正常路径 hydrateHistory 已拉完历史（lastSequence
    // 就位），这里只接增量；水合失败时 lastSequence 为空，让服务端从头回放兜底。
    // 快照的 latest_event_sequence 不能拿来当首连起点——那等于宣称「历史我都
    // 有了」，结果是重进任务后执行轨迹一片空白。
    replayThrough ??= currentView?.latest_event_sequence ?? 0;
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
        const frame = JSON.parse(String(message.data));
        ingestStreamEvent(root, frame, Number(frame?.sequence) <= (replayThrough ?? 0));
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

    const approvalOption = target?.closest<HTMLButtonElement>("[data-approval-option]");
    if (approvalOption?.dataset.approvalOption && currentView?.pending_approval) {
      event.preventDefault();
      event.stopPropagation();
      // 只记选择、不提交：真正生效仍要点下面那个确认按钮。修订门这一下可能触发
      // 整条下游链路重跑并追加一份配额，不能让「点了个单选」等于「已经开跑」。
      chosenApprovalOption = {
        approvalId: currentView.pending_approval.id,
        optionId: approvalOption.dataset.approvalOption,
      };
      renderAgent(root, currentScreen, currentView);
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
        // 多选项审批门（修订门、G2 数据闸门）的选项就摆在按钮上方；「方案页」那句
        // 只对 G1 方案确认成立，对修订门说出来是驴唇不对马嘴。
        const message = shouldOfferOptions(currentView.pending_approval, action)
          ? "请先在上方选项中选定一项，再确认"
          : "请先在当前方案页选择要采用的方案";
        renderError(root, new Error(message));
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
  void refresh().then(async () => {
    if (disposed || !currentView) return;
    // 先把 replayThrough 冻结在快照序号上（水合本身会推高 lastSequence，
    // 不能反过来把水合期间产生的新事件也算成历史）。
    replayThrough ??= currentView.latest_event_sequence ?? 0;
    await hydrateHistory();
    if (!disposed) connectEvents();
  });
}
