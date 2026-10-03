import type { CreateProjectInput, CreateTaskRunInput, ProjectMode } from "@openmathmodel/contracts";
import { attachmentsWithin } from "../attachments/composer-attachments";
import { toDraftAttachments, uploadStateOf } from "../attachments/draft";
import { describeFormat } from "../attachments/formats";
import { formatBytes } from "../attachments/limits";
import type { AttachmentStore } from "../attachments/store";
import { persistTaskAttachmentExcerpts } from "../attachments/task-attachment-context";
import { uploadAttachments } from "../attachments/upload";
import { fetchMe, invalidateMe } from "../auth/api";
import { openAuthDialog } from "../auth/auth-dialog";
import { t } from "../i18n/locale";
import type { ScreenId } from "../types/screens";
import {
  clearComposerReferences,
  composerReferenceBlock,
  listComposerReferences,
  persistPendingTaskReferences,
} from "./composer-references";
import { demoMode } from "./demo-mode";
import {
  beginHomeChatTurn,
  homeIntakeContext,
  rememberIntake,
  resetHomeChat,
  restoreHomeChat,
  runHomeChatTurn,
  stopHomeChatGeneration,
  type PendingHomeTurn,
} from "./home-chat";
import {
  modelingWorkspaceApi,
  WorkspaceApiError,
  type TaskIntakePendingTask,
  type TaskIntakeResult,
  type TaskIntakeTurn,
} from "./modeling-workspace-api";
import {
  showTaskLaunchOverlay,
  type TaskLaunchOverlay,
  type TaskLaunchPhase,
} from "./task-launch-overlay";
import {
  buildRunningUrl,
  deriveProjectName,
  MAX_CONVERSATION_CONTEXT,
  MAX_GOAL_LENGTH,
  normalizeTaskDescription,
  parseTaskDraft,
  type TaskAttachmentDraft,
  type TaskDraft,
} from "./task-start-state";

const DRAFT_KEY = "openmathmodel.taskDraft.v1";
const LEGACY_PROMPT_KEY = "openmathmodelPrompt";
/** 送入接待判定的单附件正文摘录上限（服务端提示词还会二次截断） */
const INTAKE_EXCERPT_CHARS = 1200;
const ACTIVE_RUN_KEY = "openmathmodel.activeRunId";
const ACTIVE_PROJECT_KEY = "openmathmodel.activeProjectId";
const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;
const PROJECT_ID_PATTERN = /^proj_[0-9a-f]{32}$/;

const DEMO_DRAFT: TaskDraft = {
  version: 1,
  description: "请结合共享单车订单、站点与天气数据，完成需求预测、区域划分和调度优化。",
  task_type: "竞赛建模",
  selected_model: "auto",
  attachments: [
    { name: "A题.pdf", size: 1_342_177, type: "application/pdf", last_modified: 0 },
    { name: "附件一.xlsx", size: 88_781, type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", last_modified: 0 },
    { name: "站点数据.csv", size: 524_698, type: "text/csv", last_modified: 0 },
    { name: "天气数据.csv", size: 254_874, type: "text/csv", last_modified: 0 },
  ],
};

let activeCleanup: (() => void) | undefined;

function readDraft(): TaskDraft | null {
  try {
    return parseTaskDraft(sessionStorage.getItem(DRAFT_KEY));
  } catch {
    return null;
  }
}

function saveDraft(draft: TaskDraft): boolean {
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

function selectedModel(): string {
  try {
    return localStorage.getItem("openmathmodelSelectedModel") || "auto";
  } catch {
    return "auto";
  }
}

/** 输入框里挂着附件集合时它就是唯一事实来源：清空附件也要如实写回草稿。 */
function attachmentsFor(store: AttachmentStore | undefined, fallback: TaskAttachmentDraft[]): TaskAttachmentDraft[] {
  return store ? toDraftAttachments(store.list()) : fallback;
}

/** 同一份内容的失败重试：沿用已写回的项目标识、幂等 token 与接待给出的题面 / 对话摘录。 */
function withRetryState(next: TaskDraft, from: TaskDraft): TaskDraft {
  return {
    ...next,
    ...(from.project_id ? { project_id: from.project_id } : {}),
    ...(from.run_request_token ? { run_request_token: from.run_request_token } : {}),
    ...(from.task_goal ? { task_goal: from.task_goal } : {}),
    ...(from.conversation_context ? { conversation_context: from.conversation_context } : {}),
  };
}

function navigate(path: string): void {
  window.location.href = path;
}

function statusElement(root: HTMLElement): HTMLElement | null {
  return root.querySelector<HTMLElement>("[data-task-start-status]");
}

function renderStatus(root: HTMLElement, message: string, kind: "status" | "error" = "status"): void {
  const status = statusElement(root);
  if (!status) return;
  status.hidden = false;
  status.textContent = message;
  status.setAttribute("role", kind === "error" ? "alert" : "status");
}

function clearStatus(root: HTMLElement): void {
  const status = statusElement(root);
  if (!status) return;
  status.hidden = true;
  status.textContent = "";
  status.setAttribute("role", "status");
}

function currentTaskType(root: HTMLElement, fallback: string): string {
  return root.querySelector<HTMLElement>("[data-task-type].active")?.dataset.taskType || fallback;
}

type SubmitOutcome =
  | { status: "created"; url: string }
  | { status: "auth-required" }
  /** 接待判定没有路由到 start（提议 / 追问 / 直接回答）：原地回应，不建项目。 */
  | { status: "guidance"; intake: TaskIntakeResult };

/** 接待判定要看的首页对话状态：最近几轮原话 + 上一轮待确认的提议。 */
type IntakeContext = () => { history: TaskIntakeTurn[]; pending_task?: TaskIntakePendingTask };

interface SubmitOptions {
  signal: AbortSignal;
  /** 首页输入框的附件集合；确认页只有草稿元数据时为空 */
  attachments?: AttachmentStore;
  /** 首页对话的接待上下文；确认页没有对话。 */
  intakeContext?: IntakeContext;
  /** 确认页点了「开始任务」：明确的执行意图。 */
  confirmed?: boolean;
  onProgress: (message: string) => void;
  onDraft: (draft: TaskDraft) => void;
  /** 接待判定放行后各阶段真实开始时回调：过场遮罩据此推进步骤。 */
  onPhase?: (phase: TaskLaunchPhase) => void;
}

async function submitDraft(initial: TaskDraft, options: SubmitOptions): Promise<SubmitOutcome> {
  let draft = initial;
  // project_id 与 run_request_token 必须落盘，重试时才不会重复建项目、重复起任务。
  const persist = (next: TaskDraft, failure: string): void => {
    draft = next;
    options.onDraft(next);
    if (!saveDraft(next)) throw new Error(failure);
  };

  // 用缓存的登录态，不为每次发送多绕一趟 /me：会话真的过期时接下来的任何请求都会
  // 401，错误路径统一 invalidateMe + 重新登录，不会漏掉。
  const me = await fetchMe();
  if (!me) return { status: "auth-required" };

  // 接待判定（ADR-0024，轮次理解 → 路由守卫）：只有 route=start 才建任务；拿不准先在
  // 首页对话里提议、下一句确认才启动，缺题面先问，寒暄 / 问知识 / 问文件直接回答。判定
  // 带着首页对话的最近几轮与上一轮提议，「好的，开始吧」才有所指；服务端绝不报错，判定
  // 失败时按本地强证据给出路由。已写回 project_id 的失败重试跳过判定——那份内容此前已被
  // 放行过，题面沿用草稿里记下的 task_goal。附件与 @ 引用只是证据：浏览器解析出的摘录随
  // 判定上送，单独的「上传文件」不构成开始建模。
  let goal = draft.task_goal || draft.description;
  if (!draft.project_id) {
    options.onProgress("正在确认任务类型…");
    const hasProblemReference = listComposerReferences().some(item => item.kind === "problem");
    const store = options.attachments;
    let intakeAttachments: { name: string; excerpt: string; characters: number }[] | undefined;
    if (!hasProblemReference && store && store.list().length > 0) {
      await store.settled();
      intakeAttachments = store.list().map(attachment => ({
        name: attachment.file.name,
        excerpt: (attachment.parse?.text ?? "").slice(0, INTAKE_EXCERPT_CHARS),
        characters: attachment.parse?.characters ?? 0,
      }));
    }
    const intake = await modelingWorkspaceApi.runTaskIntake(
      {
        goal: draft.description,
        has_attachments: draft.attachments.length > 0 || hasProblemReference,
        ...(intakeAttachments ? { attachments: intakeAttachments } : {}),
        ...(options.intakeContext?.() ?? {}),
        ...(options.confirmed ? { confirmed: true } : {}),
      },
      options.signal,
    );
    // 旧后端的结果没有 route：按旧三值意图兜底
    const route = intake.route ?? (intake.intent === "modeling_task" ? "start" : "reply");
    if (route !== "start") {
      return { status: "guidance", intake };
    }
    // 确认提议 / 指代上文时题面由接待拼好（不是「好的，开始吧」这一句）；首页对话摘录随任务
    // 交给问题分析节点。两者写进草稿：后续步骤失败重试时不再过判定，靠它们保住题面。
    goal = normalizeTaskDescription(intake.task_goal ?? "").slice(0, MAX_GOAL_LENGTH) || draft.description;
    const context = (intake.context_excerpt ?? "").trim().slice(0, MAX_CONVERSATION_CONTEXT);
    persist(
      { ...draft, task_goal: goal, ...(context ? { conversation_context: context } : {}) },
      "任务草稿保存失败，请允许当前站点使用会话存储后重试",
    );
  }

  // 判定已放行（或重试时此前放行过）：过场遮罩从这里接管视口
  options.onPhase?.("project");
  let projectId = draft.project_id;
  if (!projectId) {
    const projectInput: CreateProjectInput = {
      name: deriveProjectName(goal),
      description: goal.slice(0, 2000),
      mode: modeFor(draft),
    };
    const project = await modelingWorkspaceApi.createProject(projectInput, options.signal);
    if (!PROJECT_ID_PATTERN.test(project.id)) throw new Error("项目接口返回了无效的 project_id");
    projectId = project.id;
    persist(
      { ...draft, project_id: projectId },
      "项目已创建，但浏览器未能保存项目标识，请刷新后从项目列表进入",
    );
  }

  // 附件必须赶在任务创建前落地：auto_start 的任务一创建 Agent 就开跑，
  // 晚到的附件进不了第一轮上下文。
  const store = options.attachments;
  if (store && store.list().length > 0) {
    options.onPhase?.("attachments");
    await store.settled();
    const report = await uploadAttachments(store, projectId, options.signal, (done, total) => {
      options.onProgress(`正在上传附件 ${done}/${total}…`);
    });
    persist(
      { ...draft, attachments: toDraftAttachments(store.list()) },
      "附件已上传，但浏览器未能保存产物标识，请刷新后从项目列表进入",
    );
    if (report.failed > 0) {
      throw new Error(`${report.failed} 个附件上传失败，可移除后重试；已上传的不会重复上传`);
    }
  }

  options.onPhase?.("agent");
  options.onProgress(
    store && store.list().length > 0
      ? "附件已就位，正在启动 Agent 工作流…"
      : "项目已创建，正在启动 Agent 工作流…",
  );
  const requestToken = draft.run_request_token ?? crypto.randomUUID().replaceAll("-", "");
  if (!draft.run_request_token) {
    persist({ ...draft, run_request_token: requestToken }, "运行请求保存失败，请检查浏览器会话存储权限");
  }
  const runInput: CreateTaskRunInput = {
    project_id: projectId,
    goal,
    auto_start: true,
    params: {
      task_type: draft.task_type,
      selected_model: draft.selected_model,
      attachment_metadata: draft.attachments,
      // 首页对话摘录（ADR-0024）：任务由对话里的提议确认而来时，题意的补充说明散在对话里
      ...(draft.conversation_context ? { conversation_context: draft.conversation_context } : {}),
      // @ 引用的赛题/论文/方法正文摘要：问题分析靠它看到真实题面
      // （「这道题」+ 引用赛题的发送方式，题面全在引用里）。
      reference_metadata: listComposerReferences().map(reference => ({
        kind: reference.kind,
        title: reference.title,
        excerpt: reference.text.slice(0, 4000),
      })),
      attachment_upload_state: store && store.list().length > 0
        ? uploadStateOf(store.list())
        : draft.attachments.length > 0 ? "metadata_only" : "none",
    },
  };
  const run = await modelingWorkspaceApi.createTaskRun(runInput, requestToken, options.signal);
  if (!RUN_ID_PATTERN.test(run.id) || run.project_id !== projectId) {
    throw new Error("任务接口返回了无效的 run_id 或 project_id");
  }
  sessionStorage.setItem(ACTIVE_PROJECT_KEY, projectId);
  sessionStorage.setItem(ACTIVE_RUN_KEY, run.id);
  sessionStorage.setItem(LEGACY_PROMPT_KEY, goal);
  // 运行页首屏气泡按 run 隔离取题面；全局键只服务演示态兜底。
  sessionStorage.setItem(`openmathmodel.taskGoal.${run.id}`, goal);
  // 首页挂着的知识库引用 chips 随任务交接到运行页（按 run 存取，一次性消费）。
  persistPendingTaskReferences(run.id);
  // 浏览器解析摘录随任务交接：运行页开场分析在服务端正文没赶上时以它兜底，
  // 不至于对着任务名说「没收到题面」。
  persistTaskAttachmentExcerpts(run.id, draft.attachments);
  sessionStorage.removeItem(DRAFT_KEY);
  return { status: "created", url: buildRunningUrl(run.id, projectId) };
}

interface TaskSubmitter {
  start: (draft?: TaskDraft) => void;
  isPending: () => boolean;
}

interface TaskSubmitterOptions {
  root: HTMLElement;
  signal: AbortSignal;
  attachments?: AttachmentStore;
  /** 首页对话的接待上下文（最近几轮 + 待确认的提议）；确认页没有。 */
  intakeContext?: IntakeContext;
  /** 确认页：点「开始任务」本身就是明确的执行意图。 */
  confirmed?: boolean;
  setBusy: (busy: boolean) => void;
  isDisposed: () => boolean;
  /** 接待判定没有路由到 start 时的处理；缺省把模板回应显示在状态行（确认页）。首页转对话。 */
  onGuidance?: (intake: TaskIntakeResult, sentText: string) => void;
  /**
   * 首页的对话优先体感：点发送的瞬间先把消息落到对话区（`begin`），接待判定在后台
   * 静默进行；判定不放行时回复写进同一个占位，放行则过场遮罩接管；需要登录或出错
   * 时 `rollback` 撤回占位并把文字还给输入框。提供了它，状态行就只在出错时使用——
   * 进度反馈由占位块与过场遮罩承担，不再在输入框下面滚一行字。
   */
  optimistic?: { begin(sentText: string): void; rollback(): void };
}

function createTaskSubmitter(options: TaskSubmitterOptions): TaskSubmitter {
  const { root } = options;
  const showProgress = !options.optimistic;
  let pending = false;
  let draft: TaskDraft | undefined;

  function requestAuthentication(message: string): void {
    root.dataset.taskStartState = "auth-required";
    renderStatus(root, message, "error");
    openAuthDialog({
      onAuthenticated: () => {
        if (!options.isDisposed()) run();
      },
    });
  }

  function run(): void {
    if (pending || !draft || options.isDisposed()) return;
    pending = true;
    options.setBusy(true);
    root.dataset.taskStartState = "loading";
    clearStatus(root);
    const sentText = draft.description;
    options.optimistic?.begin(sentText);
    // 过场遮罩：接待判定放行、第一个阶段真实开始时才出现（闲聊/缺题面转
    // 首页对话的路径永远看不到它）；每次提交一个实例，失败即淡出。
    let overlay: TaskLaunchOverlay | undefined;
    const hasAttachments = (options.attachments?.list().length ?? 0) > 0
      || draft.attachments.length > 0;
    void submitDraft(draft, {
      signal: options.signal,
      attachments: options.attachments,
      intakeContext: options.intakeContext,
      confirmed: options.confirmed,
      onProgress: message => {
        if (showProgress) renderStatus(root, message);
        overlay?.setNote(message);
      },
      onDraft: updated => { draft = updated; },
      onPhase: phase => {
        if (options.isDisposed()) return;
        overlay ??= showTaskLaunchOverlay({ hasAttachments });
        overlay.setPhase(phase);
      },
    }).then(outcome => {
      if (options.isDisposed()) return;
      if (outcome.status === "auth-required") {
        pending = false;
        options.setBusy(false);
        overlay?.dismiss();
        options.optimistic?.rollback();
        requestAuthentication("请先登录，登录成功后会继续创建当前任务。");
        return;
      }
      if (outcome.status === "guidance") {
        pending = false;
        options.setBusy(false);
        overlay?.dismiss();
        root.dataset.taskStartState = "guidance";
        if (options.onGuidance) {
          clearStatus(root);
          options.onGuidance(outcome.intake, sentText);
        } else {
          renderStatus(root, outcome.intake.reply);
        }
        return;
      }
      root.dataset.taskStartState = "created";
      if (showProgress) renderStatus(root, "任务已创建，正在进入运行工作台…");
      // 成功：遮罩打勾定格后再导航；遮罩缺席（极端时序）直接导航兜底。
      if (overlay) overlay.succeed(() => navigate(outcome.url));
      else navigate(outcome.url);
    }, (error: unknown) => {
      if (options.isDisposed() || (error instanceof DOMException && error.name === "AbortError")) return;
      pending = false;
      options.setBusy(false);
      overlay?.dismiss();
      options.optimistic?.rollback();
      if (error instanceof WorkspaceApiError && error.status === 401) {
        invalidateMe();
        requestAuthentication("登录状态已失效，请重新登录后继续。");
        return;
      }
      root.dataset.taskStartState = "error";
      renderStatus(root, error instanceof Error ? error.message : "任务创建失败，请稍后重试。", "error");
    });
  }

  return {
    start(next) {
      if (next) draft = next;
      run();
    },
    isPending: () => pending,
  };
}

function sameSubmission(persisted: TaskDraft | null, next: TaskDraft): persisted is TaskDraft {
  return persisted !== null
    && persisted.description === next.description
    && persisted.task_type === next.task_type
    && persisted.selected_model === next.selected_model
    && JSON.stringify(persisted.attachments) === JSON.stringify(next.attachments);
}

function mountNewTask(root: HTMLElement): () => void {
  root.dataset.taskStartState = "draft";
  root.dataset.taskStartSource = "local";
  // 侧栏「最近任务」点开的历史对话在这里重建现场；地址栏没带 ?chat= 就是
  // 新的一段对话，把上一段的归属与上下文解绑，别把两段聊到一起去。
  const requestedChat = new URL(window.location.href).searchParams.get("chat");
  if (!requestedChat) {
    resetHomeChat();
  } else {
    // 记录在服务端（ADR-0016）：拉齐后重建现场，两边都没有记录才回到欢迎态
    void restoreHomeChat(root, requestedChat).then(restored => {
      if (!restored) resetHomeChat();
    });
  }
  const textarea = root.querySelector<HTMLTextAreaElement>('[data-task-description], textarea[aria-label="任务描述"]');
  const attachments = attachmentsWithin(root);
  const sendButton = root.querySelector<HTMLButtonElement>('[data-action="send"]');
  const abortController = new AbortController();
  let disposed = false;
  const stored = readDraft();
  let draft: TaskDraft = stored ?? {
    version: 1,
    description: "",
    task_type: "竞赛建模",
    selected_model: selectedModel(),
    attachments: [],
  };

  if (textarea && !textarea.value) {
    let legacyPrompt = "";
    try {
      legacyPrompt = sessionStorage.getItem(LEGACY_PROMPT_KEY) ?? "";
    } catch {
      // 会话存储不可用时仍可在当前页面输入任务。
    }
    textarea.value = draft.description || legacyPrompt;
    if (!draft.description && legacyPrompt) draft = { ...draft, description: legacyPrompt };
  }

  // 发送瞬间落地的占位对话轮：同一时刻只会有一次提交在途（submitter 自己有闸），
  // 一个变量够用。判定不放行时回复写进它；需要登录 / 出错时撤回并把文字还回输入框。
  let pendingTurn: PendingHomeTurn | null = null;

  const submitter = createTaskSubmitter({
    root,
    signal: abortController.signal,
    attachments,
    intakeContext: homeIntakeContext,
    isDisposed: () => disposed,
    // 首页发送键只有图标没有文案，忙碌态用禁用 + aria-busy 表达。
    setBusy: busy => {
      if (!sendButton) return;
      sendButton.disabled = busy;
      sendButton.setAttribute("aria-busy", String(busy));
    },
    optimistic: {
      begin: sentText => {
        pendingTurn = beginHomeChatTurn(
          root,
          sentText,
          listComposerReferences().map(reference => reference.title),
        );
        // 消息已经在对话区了，输入框腾出来。这里不写草稿：提交流程正拿着草稿写回
        // project_id / 幂等 token，同一把钥匙两处写会把重试凭据冲掉。
        if (textarea) textarea.value = "";
      },
      rollback: () => {
        pendingTurn?.discard();
        pendingTurn = null;
        if (textarea && !textarea.value && draft.description) textarea.value = draft.description;
      },
    },
    // 接待判定没有路由到 start → 进入首页对话：回复由用户配置的模型流式生成，写进发送时
    // 已落地的那个占位块；接待结论随本轮上送（服务端注入【接待判定】块，提议落 meta.intake），
    // @ 引用的资料与托盘里还没注入过的附件正文随本轮消息送给模型（与执行页同语义：发送
    // 成功后清空引用，失败保留以便重试；附件留在托盘作后续任务材料）。提议之后的下一句
    // 「开始」由接待判定直接启动任务。
    onGuidance: (intake, sentText) => {
      // 输入框在发送瞬间就腾出来了，这里不再碰它——用户可能已经在敲下一句；
      // 只把草稿按输入框现状重写（这句已是对话消息，不再是待办题面）。
      persistCurrent();
      // 连提交时写下的旧 openmathmodelPrompt 一起清掉，否则下次回首页输入框里又躺着它。
      try {
        sessionStorage.removeItem(LEGACY_PROMPT_KEY);
      } catch {
        // 会话存储不可用时也没有残留可清
      }
      const references = listComposerReferences();
      const pending = pendingTurn;
      pendingTurn = null;
      void runHomeChatTurn(root, sentText, {
        pending,
        referenceContext: composerReferenceBlock(),
        referenceTitles: references.map(reference => reference.title),
        intake: rememberIntake(intake),
        ...(attachments ? { attachments } : {}),
        // 未配置模型接口：首页没有对话模型，接待的模板回应就是这一轮的回复
        ...(intake.chat_ready === false ? { localReply: intake.reply } : {}),
      }).then(delivered => {
        if (delivered && references.length > 0) clearComposerReferences();
      });
    },
  });

  const persistCurrent = (): boolean => {
    const next: TaskDraft = {
      version: 1,
      description: textarea?.value ?? draft.description,
      task_type: currentTaskType(root, draft.task_type),
      selected_model: selectedModel(),
      attachments: attachmentsFor(attachments, draft.attachments),
    };
    // 内容一旦变化就丢弃已写回的 project_id 与幂等 token：旧标识只对创建它们的
    // 那份内容有效，带着旧 token 提交新内容会命中幂等键冲突（409）。
    draft = sameSubmission(draft, next) ? withRetryState(next, draft) : next;
    return saveDraft(draft);
  };

  // 创建过程中草稿由提交流程接管，继续编辑不能把已写入的 project_id 冲掉，否则重试会重复建项目。
  const onInput = (): void => {
    if (submitter.isPending()) return;
    persistCurrent();
    clearStatus(root);
  };
  // 解析是异步的，字数与产物标识会陆续回填，每次变更都要重写草稿。
  const unsubscribe = attachments?.subscribe(() => {
    if (submitter.isPending()) return;
    if (!persistCurrent()) {
      renderStatus(root, "浏览器未能保存附件信息，请检查隐私模式或存储权限。", "error");
    }
  });
  const onClick = (event: MouseEvent): void => {
    const target = event.target instanceof Element ? event.target : null;
    const taskType = target?.closest<HTMLElement>("[data-task-type]");
    if (taskType) {
      draft = { ...draft, task_type: taskType.dataset.taskType || draft.task_type };
      window.setTimeout(persistCurrent, 0);
      return;
    }
    const submit = target?.closest<HTMLElement>('[data-action="send"]');
    if (!submit) return;
    event.preventDefault();
    event.stopPropagation();
    // 生成中发送键就是暂停键：点击中止当前回复流，不进入任务创建
    if (submit.dataset.mode === "stop") {
      stopHomeChatGeneration();
      return;
    }
    if (submitter.isPending()) return;
    const description = normalizeTaskDescription(textarea?.value ?? "");
    if (!description) {
      renderStatus(root, "请输入任务描述后再继续。", "error");
      textarea?.focus();
      return;
    }
    if (description.length > MAX_GOAL_LENGTH) {
      renderStatus(root, `任务描述不能超过 ${MAX_GOAL_LENGTH} 个字符。`, "error");
      textarea?.focus();
      return;
    }
    const next: TaskDraft = {
      version: 1,
      description,
      task_type: currentTaskType(root, draft.task_type),
      selected_model: selectedModel(),
      attachments: attachmentsFor(attachments, draft.attachments),
    };
    // 重新发送未修改的同一份草稿视为失败重试：沿用已写回的 project_id 与幂等
    // token，不重复建项目、不换 Idempotency-Key；内容变化则视为新提交，重置两者。
    const persisted = readDraft();
    draft = sameSubmission(persisted, next) ? withRetryState(next, persisted) : next;
    if (!saveDraft(draft)) {
      renderStatus(root, "任务草稿保存失败，请允许当前站点使用会话存储后重试。", "error");
      return;
    }
    try {
      sessionStorage.setItem(LEGACY_PROMPT_KEY, description);
    } catch {
      // 正式草稿已经保存；旧演示提示词仅用于运行页首次渲染。
    }
    root.dataset.taskStartState = "ready";
    submitter.start(draft);
  };

  textarea?.addEventListener("input", onInput);
  root.addEventListener("click", onClick);
  return () => {
    disposed = true;
    abortController.abort();
    unsubscribe?.();
    textarea?.removeEventListener("input", onInput);
    root.removeEventListener("click", onClick);
  };
}

const PARSE_STATE_TEXT: Record<NonNullable<TaskAttachmentDraft["parse_status"]>, string> = {
  ready: "已解析",
  partial: "已部分解析",
  "server-pending": "等待服务端解析",
  empty: "未提取到文字",
  failed: "解析失败",
};

function attachmentState(attachment: TaskAttachmentDraft): string {
  // 图片计数始终展示：纯文本模型看不到图（ADR-0010），用户在确认页也该知道。
  const imageSuffix = attachment.images ? ` · ${attachment.images.toLocaleString("zh-CN")} 张图` : "";
  if (attachment.artifact_id) {
    const base = attachment.characters
      ? `已上传 · ${attachment.characters.toLocaleString("zh-CN")} 字`
      : "已上传";
    return base + imageSuffix;
  }
  if (!attachment.parse_status) return `已保存元数据${imageSuffix}`;
  const base = attachment.characters
    ? `${PARSE_STATE_TEXT[attachment.parse_status]} · ${attachment.characters.toLocaleString("zh-CN")} 字`
    : PARSE_STATE_TEXT[attachment.parse_status];
  return base + imageSuffix;
}

function renderAttachments(root: HTMLElement, attachments: TaskAttachmentDraft[]): void {
  const list = root.querySelector<HTMLElement>("[data-task-file-list]");
  if (!list) return;
  list.replaceChildren();
  if (attachments.length === 0) {
    const empty = document.createElement("div");
    empty.className = "file-read-row";
    empty.textContent = "未添加附件，可稍后在项目中补充。";
    list.append(empty);
    return;
  }
  attachments.forEach(attachment => {
    const row = document.createElement("div");
    row.className = "file-read-row";
    const name = document.createElement("span");
    name.className = "file-name";
    const icon = document.createElement("i");
    icon.className = `ph ph-${describeFormat(attachment.name, attachment.type).icon}`;
    icon.setAttribute("aria-hidden", "true");
    name.append(icon, attachment.name);
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = formatBytes(attachment.size);
    const state = document.createElement("span");
    state.className = "read";
    state.textContent = attachmentState(attachment);
    row.append(name, size, state);
    list.append(row);
  });
}

function modeFor(draft: TaskDraft): ProjectMode {
  if (draft.task_type === "论文优化") return "review";
  if (draft.task_type === "模型比较") return "auto_experiment";
  return "collaboration";
}

function setStartBusy(button: HTMLButtonElement, busy: boolean): void {
  if (busy) {
    button.dataset.idleLabel = button.textContent?.trim() || "开始任务";
    button.textContent = "正在创建…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.idleLabel || "开始任务";
    button.disabled = false;
  }
}

function mountConfirmTask(root: HTMLElement): () => void {
  const stored = readDraft();
  // 演示夹具只在显式 `?demo=1` 下启用；没有草稿又不是演示态时给空态，
  // 不再拿「2026 国赛 A 题 + 四个假附件」冒充用户刚填的任务。
  const isDemo = demoMode() && !(stored && normalizeTaskDescription(stored.description));
  const draft = stored && normalizeTaskDescription(stored.description)
    ? stored
    : (isDemo ? DEMO_DRAFT : null);
  const abortController = new AbortController();
  let disposed = false;
  const startButton = root.querySelector<HTMLButtonElement>('[data-task-start-submit], [data-go="running"]');
  const submitter = createTaskSubmitter({
    root,
    signal: abortController.signal,
    confirmed: true,
    isDisposed: () => disposed,
    setBusy: busy => {
      if (startButton) setStartBusy(startButton, busy);
    },
  });

  root.dataset.taskStartSource = draft ? (isDemo ? "demo" : "draft") : "empty";
  root.dataset.taskStartState = draft ? (isDemo ? "demo" : "ready") : "empty";
  const projectNameNode = root.querySelector<HTMLElement>("[data-task-project-name]");
  const descriptionNode = root.querySelector<HTMLElement>("[data-task-description-preview]");
  if (projectNameNode) projectNameNode.textContent = draft ? deriveProjectName(draft.description) : t("尚未创建任务");
  if (descriptionNode) {
    descriptionNode.textContent = draft
      ? draft.description
      : t("还没有任务草稿。回首页描述你要解决的问题，再回到这里确认。");
  }
  renderAttachments(root, draft?.attachments ?? []);
  if (!draft) {
    renderStatus(root, "回首页填写任务后再确认。", "status");
    if (startButton) startButton.disabled = true;
  } else if (isDemo) {
    renderStatus(root, "当前为示例任务；“开始任务”将进入演示工作台，不会创建项目。", "status");
    if (startButton) startButton.textContent = "查看演示任务";
  } else {
    renderStatus(root, `草稿已保存 · ${draft.attachments.length} 个附件元数据待项目创建后上传`, "status");
  }

  const onClick = (event: MouseEvent): void => {
    const target = event.target instanceof Element ? event.target : null;
    const back = target?.closest<HTMLElement>('[data-go="new"]');
    const start = target?.closest<HTMLElement>('[data-task-start-submit], [data-go="running"]');
    if (!back && !start) return;
    event.preventDefault();
    event.stopPropagation();
    if (back) {
      if (!submitter.isPending()) navigate("/");
      return;
    }
    if (submitter.isPending()) return;
    if (isDemo) {
      navigate("/task/running?demo=1");
      return;
    }
    // 空态（没有草稿）没有可提交的东西，按钮已禁用，这里再兜一层
    const pending = readDraft() ?? draft;
    if (!pending) {
      navigate("/");
      return;
    }
    // 重试时以持久化草稿为准：失败前已写回的 project_id / run_request_token
    // 不能被挂载时的旧草稿覆盖，否则会重复创建项目或换幂等键重复起任务。
    submitter.start(pending);
  };

  root.addEventListener("click", onClick);
  return () => {
    disposed = true;
    abortController.abort();
    root.removeEventListener("click", onClick);
  };
}

export function mountTaskStartFlow(screen: ScreenId): void {
  activeCleanup?.();
  activeCleanup = undefined;
  if (screen !== "new" && screen !== "confirm") return;
  const root = document.querySelector<HTMLElement>("[data-task-start-root]");
  if (!root) return;
  activeCleanup = screen === "new" ? mountNewTask(root) : mountConfirmTask(root);
}
