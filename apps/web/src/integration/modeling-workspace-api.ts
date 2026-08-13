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

export const modelingWorkspaceApi = {
  createProject(input: CreateProjectInput, signal?: AbortSignal): Promise<Project> {
    return request<Project>("/api/v1/projects", {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
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
