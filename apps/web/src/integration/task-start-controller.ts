import type { CreateProjectInput, CreateTaskRunInput, ProjectMode } from "@openmathmodel/contracts";
import { attachmentsWithin } from "../attachments/composer-attachments";
import { toDraftAttachments, uploadStateOf } from "../attachments/draft";
import { describeFormat } from "../attachments/formats";
import { formatBytes } from "../attachments/limits";
import type { AttachmentStore } from "../attachments/store";
import { uploadAttachments } from "../attachments/upload";
import { fetchMe, invalidateMe } from "../auth/api";
import { openAuthDialog } from "../auth/auth-dialog";
import type { ScreenId } from "../types/screens";
import { modelingWorkspaceApi, WorkspaceApiError } from "./modeling-workspace-api";
import {
  buildRunningUrl,
  deriveProjectName,
  MAX_GOAL_LENGTH,
  normalizeTaskDescription,
  parseTaskDraft,
  type TaskAttachmentDraft,
  type TaskDraft,
} from "./task-start-state";

const DRAFT_KEY = "openmathmodel.taskDraft.v1";
const LEGACY_PROMPT_KEY = "openmathmodelPrompt";
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
  | { status: "auth-required" };

interface SubmitOptions {
  signal: AbortSignal;
  /** 首页输入框的附件集合；确认页只有草稿元数据时为空 */
  attachments?: AttachmentStore;
  onProgress: (message: string) => void;
  onDraft: (draft: TaskDraft) => void;
}

async function submitDraft(initial: TaskDraft, options: SubmitOptions): Promise<SubmitOutcome> {
  let draft = initial;
  // project_id 与 run_request_token 必须落盘，重试时才不会重复建项目、重复起任务。
  const persist = (next: TaskDraft, failure: string): void => {
    draft = next;
    options.onDraft(next);
    if (!saveDraft(next)) throw new Error(failure);
  };

  const me = await fetchMe(true);
  if (!me) return { status: "auth-required" };

  let projectId = draft.project_id;
  if (!projectId) {
    const projectInput: CreateProjectInput = {
      name: deriveProjectName(draft.description),
      description: draft.description.slice(0, 2000),
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
    goal: draft.description,
    auto_start: true,
    params: {
      task_type: draft.task_type,
      selected_model: draft.selected_model,
      attachment_metadata: draft.attachments,
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
  sessionStorage.setItem(LEGACY_PROMPT_KEY, draft.description);
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
  setBusy: (busy: boolean) => void;
  isDisposed: () => boolean;
}

function createTaskSubmitter(options: TaskSubmitterOptions): TaskSubmitter {
  const { root } = options;
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
    renderStatus(root, "正在验证登录状态并创建项目…");
    void submitDraft(draft, {
      signal: options.signal,
      attachments: options.attachments,
      onProgress: message => renderStatus(root, message),
      onDraft: updated => { draft = updated; },
    }).then(outcome => {
      if (options.isDisposed()) return;
      if (outcome.status === "auth-required") {
        pending = false;
        options.setBusy(false);
        requestAuthentication("请先登录，登录成功后会继续创建当前任务。");
        return;
      }
      root.dataset.taskStartState = "created";
      renderStatus(root, "任务已创建，正在进入运行工作台…");
      navigate(outcome.url);
    }, (error: unknown) => {
      if (options.isDisposed() || (error instanceof DOMException && error.name === "AbortError")) return;
      pending = false;
      options.setBusy(false);
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

  const submitter = createTaskSubmitter({
    root,
    signal: abortController.signal,
    attachments,
    isDisposed: () => disposed,
    // 首页发送键只有图标没有文案，忙碌态用禁用 + aria-busy 表达，进度写在状态行里。
    setBusy: busy => {
      if (!sendButton) return;
      sendButton.disabled = busy;
      sendButton.setAttribute("aria-busy", String(busy));
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
    draft = sameSubmission(draft, next)
      ? { ...next, project_id: draft.project_id, run_request_token: draft.run_request_token }
      : next;
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
    draft = sameSubmission(persisted, next)
      ? { ...next, project_id: persisted.project_id, run_request_token: persisted.run_request_token }
      : next;
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
  if (attachment.artifact_id) {
    return attachment.characters ? `已上传 · ${attachment.characters.toLocaleString("zh-CN")} 字` : "已上传";
  }
  if (!attachment.parse_status) return "已保存元数据";
  return attachment.characters
    ? `${PARSE_STATE_TEXT[attachment.parse_status]} · ${attachment.characters.toLocaleString("zh-CN")} 字`
    : PARSE_STATE_TEXT[attachment.parse_status];
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
  const draft = stored && normalizeTaskDescription(stored.description) ? stored : DEMO_DRAFT;
  const isDemo = draft === DEMO_DRAFT;
  const abortController = new AbortController();
  let disposed = false;
  const startButton = root.querySelector<HTMLButtonElement>('[data-task-start-submit], [data-go="running"]');
  const submitter = createTaskSubmitter({
    root,
    signal: abortController.signal,
    isDisposed: () => disposed,
    setBusy: busy => {
      if (startButton) setStartBusy(startButton, busy);
    },
  });

  root.dataset.taskStartSource = isDemo ? "demo" : "draft";
  root.dataset.taskStartState = isDemo ? "demo" : "ready";
  const projectName = deriveProjectName(draft.description);
  const projectNameNode = root.querySelector<HTMLElement>("[data-task-project-name]");
  const descriptionNode = root.querySelector<HTMLElement>("[data-task-description-preview]");
  if (projectNameNode) projectNameNode.textContent = projectName;
  if (descriptionNode) descriptionNode.textContent = draft.description;
  renderAttachments(root, draft.attachments);
  if (isDemo) {
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
    // 重试时以持久化草稿为准：失败前已写回的 project_id / run_request_token
    // 不能被挂载时的旧草稿覆盖，否则会重复创建项目或换幂等键重复起任务。
    submitter.start(readDraft() ?? draft);
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
