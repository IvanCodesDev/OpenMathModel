import type {
  CreateProjectInput,
  CreateTaskRunInput,
  ModelingWorkspaceView,
  Project,
  TaskRun,
} from "@openmathmodel/contracts";
import { requestTimeoutSeconds } from "../preferences/network-preferences";

export interface TaskRunActionInput {
  action: "approve" | "pause" | "resume" | "retry";
  approval_id?: string | null;
  option_id?: string | null;
  client_token?: string | null;
}

/** 步骤运行记录（/steps）：执行轨迹的真实耗时与尝试次数来源。 */
export interface WorkspaceStepRun {
  id: string;
  node: string;
  attempt: number;
  status: string;
  failure_class?: string | null;
  failure_message?: string | null;
  created_at: string;
  started_at?: string | null;
  ended_at?: string | null;
}

interface ErrorEnvelope {
  code?: string;
  message?: string;
  request_id?: string;
}

export class WorkspaceApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(status: number, payload: ErrorEnvelope) {
    super(payload.message ?? "建模工作台请求失败");
    this.status = status;
    this.code = payload.code ?? "UNKNOWN_ERROR";
    this.requestId = payload.request_id;
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // 高级设置「请求超时」：超时信号与调用方自己的中止信号并联，谁先触发听谁的。
  const timeoutSeconds = requestTimeoutSeconds();
  const timeout = AbortSignal.timeout(timeoutSeconds * 1000);
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...init.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError") {
      throw new Error(`请求超过 ${timeoutSeconds} 秒未响应，已中止；可在设置中心「高级设置」调整请求超时。`);
    }
    throw error;
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new WorkspaceApiError(response.status, (payload ?? {}) as ErrorEnvelope);
  }
  return payload as T;
}

/** 发送前接待判定的结果：modeling_task 才继续创建任务，其余原地展示 reply。 */
export interface TaskIntakeResult {
  intent: "modeling_task" | "needs_info" | "chat";
  reply: string;
  source: "heuristic" | "judge" | "fallback";
}

/** GET /task-runs/{id}/stage-outputs：五类页面正文（未产出的阶段为 null）。 */
export interface StageOutputsPayload {
  run_id: string;
  dataset_profile: import("@openmathmodel/contracts").DatasetProfile | null;
  plan_proposal: import("@openmathmodel/contracts").PlanProposal | null;
  experiment_summary: import("@openmathmodel/contracts").ExperimentSummary | null;
  document_draft: import("@openmathmodel/contracts").DocumentDraft | null;
  delivery_manifest: import("@openmathmodel/contracts").DeliveryManifest | null;
}

export const modelingWorkspaceApi = {
  createProject(input: CreateProjectInput, signal?: AbortSignal): Promise<Project> {
    return request<Project>("/api/v1/projects", {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  },

  /** 发送前接待判定（对话优先门控）：判定失败时服务端已放行，前端无需兜底分支。 */
  runTaskIntake(
    input: {
      goal: string;
      has_attachments: boolean;
      /** 浏览器已解析的附件证据（名字+正文摘录）；有摘录时服务端按内容判定而非放行 */
      attachments?: { name: string; excerpt: string; characters: number }[];
    },
    signal?: AbortSignal,
  ): Promise<TaskIntakeResult> {
    return request<TaskIntakeResult>("/api/v1/task-intake", {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  },

  /** 五类页面正文（数据画像/建模方案/实验总结/论文草稿/交付清单）。 */
  getStageOutputs(runId: string, signal?: AbortSignal): Promise<StageOutputsPayload> {
    return request<StageOutputsPayload>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/stage-outputs`,
      { signal },
    );
  },

  /** 历史事件一页（首屏一次性水合活动流用；after 为游标，最大页长 1000）。 */
  listRunEvents(
    runId: string,
    after: number,
    signal?: AbortSignal,
  ): Promise<{ items: import("@openmathmodel/contracts").AgentEvent[] }> {
    const params = new URLSearchParams({ after: String(after), limit: "1000" });
    return request<{ items: import("@openmathmodel/contracts").AgentEvent[] }>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/events/history?${params}`,
      { signal },
    );
  },

  /** 项目列表；默认只含未归档，archived=true 时只看已归档。 */
  listProjects(
    options: {
      archived?: boolean;
      limit?: number;
      offset?: number;
      /** stats = 每项附带最新运行投影与产物计数（服务端一次聚合，切片②）。 */
      include?: "stats";
      /** 按项目名或最新运行目标模糊搜索（大小写不敏感）。 */
      q?: string;
      /** 按最新运行归桶：active = 未到终态；done = 已完成。 */
      state?: "active" | "done";
    } = {},
    signal?: AbortSignal,
  ): Promise<{ items: Project[]; total: number }> {
    const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
    if (options.archived) params.set("archived", "true");
    if (options.offset) params.set("offset", String(options.offset));
    if (options.include) params.set("include", options.include);
    if (options.q) params.set("q", options.q);
    if (options.state) params.set("state", options.state);
    return request<{ items: Project[]; total: number }>(`/api/v1/projects?${params}`, { signal });
  },

  /** 当前用户的运行列表（创建时间倒序）；传 projectId 只看单个项目（运行历史）。 */
  listTaskRuns(
    limit = 20,
    projectId?: string,
    signal?: AbortSignal,
  ): Promise<{ items: TaskRun[]; total: number }> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (projectId) params.set("project_id", projectId);
    return request<{ items: TaskRun[]; total: number }>(`/api/v1/task-runs?${params}`, {
      signal,
    });
  },

  /** 项目维护：重命名与归档/取消归档（侧栏「最近任务」的操作菜单）。 */
  updateProject(
    projectId: string,
    input: { name?: string; archived?: boolean },
    signal?: AbortSignal,
  ): Promise<Project> {
    return request<Project>(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
      method: "PATCH",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  },

  /** 删除项目及其全部运行与产物；不可恢复，仅隐藏请用归档。 */
  deleteProject(projectId: string, signal?: AbortSignal): Promise<void> {
    return request<void>(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
      signal,
    });
  },

  createTaskRun(
    input: CreateTaskRunInput,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<TaskRun> {
    return request<TaskRun>("/api/v1/task-runs", {
      method: "POST",
      signal,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
    });
  },

  get(runId: string, signal?: AbortSignal): Promise<ModelingWorkspaceView> {
    return request<ModelingWorkspaceView>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/workspace`,
      { signal },
    );
  },

  steps(runId: string, signal?: AbortSignal): Promise<{ items: WorkspaceStepRun[] }> {
    return request<{ items: WorkspaceStepRun[] }>(
      `/api/v1/task-runs/${encodeURIComponent(runId)}/steps`,
      { signal },
    );
  },

  /** 运行中追加补充要求（§11.3 方案 A）：落库后在后续每次节点执行时注入提示词。
   *  运行已到终态时服务端返回 409 RUN_FINISHED，由调用方如实向用户说明。 */
  postRunNote(
    runId: string,
    text: string,
    scope = "global",
    signal?: AbortSignal,
  ): Promise<{ id: string }> {
    return request<{ id: string }>(`/api/v1/task-runs/${encodeURIComponent(runId)}/notes`, {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, scope }),
    });
  },

  act(runId: string, input: TaskRunActionInput, signal?: AbortSignal): Promise<unknown> {
    const idempotencyKey = input.client_token ?? crypto.randomUUID().replaceAll("-", "");
    return request(`/api/v1/task-runs/${encodeURIComponent(runId)}/actions`, {
      method: "POST",
      signal,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ ...input, client_token: idempotencyKey }),
    });
  },
};

export const WORKSPACE_EVENT_TYPES = [
  "run.created",
  "run.status_changed",
  "run.node_changed",
  "run.log",
  "step.started",
  "step.succeeded",
  "step.failed",
  "approval.requested",
  "approval.resolved",
  "artifact.published",
] as const;
