/**
 * OpenMathModel 契约 TypeScript 类型（v1）。
 *
 * 事实来源是 ../schemas/v1/*.schema.json；本文件与 Schema 同步人工维护，
 * 引入代码生成前，两者不一致以 Schema 为准（差异属于缺陷）。
 * 字段命名与 JSON 载荷一致（snake_case），不做驼峰转换。
 */

export type { ModelingWorkspaceView } from "./ts/v1/modeling-workspace-view";

// ---- TaskRun：status 是稳定生命周期，current_node 是领域阶段（两轴分离） ----

export const TASK_RUN_STATUSES = [
  "QUEUED",
  "RUNNING",
  "WAITING_APPROVAL",
  "PAUSED",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
] as const;
export type TaskRunStatus = (typeof TASK_RUN_STATUSES)[number];

/** 终态：不再发生任何状态迁移。 */
export const TERMINAL_TASK_RUN_STATUSES = ["COMPLETED", "FAILED", "CANCELLED"] as const;

/**
 * workflow_version = sim-0.1 的节点集合。
 * current_node 随 workflow_version 演进，消费方必须容忍未知节点名。
 */
export const SIM_01_NODES = [
  "CREATED",
  "PROBLEM_ANALYSIS",
  "DATA_PREPARATION",
  "MODEL_PLANNING",
  "EXPERIMENTING",
  "VALIDATING",
  "PAPER_WRITING",
  "COMPLETED",
] as const;

export const STEP_RUN_STATUSES = [
  "PENDING",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "SKIPPED",
] as const;
export type StepRunStatus = (typeof STEP_RUN_STATUSES)[number];

export const TASK_RUN_ACTIONS = ["approve", "pause", "resume", "cancel", "retry"] as const;
export type TaskRunAction = (typeof TASK_RUN_ACTIONS)[number];

export const AGENT_EVENT_TYPES = [
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
export type AgentEventType = (typeof AGENT_EVENT_TYPES)[number];

export const ARTIFACT_KINDS = [
  "dataset",
  "code",
  "figure",
  "table",
  "log",
  "report",
  "paper",
  "model",
  "other",
] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export type ArtifactStatus = "PENDING" | "READY" | "STALE" | "DELETED";
export type ApprovalStatus = "PENDING" | "RESOLVED" | "EXPIRED" | "CANCELLED";
export type ApprovalDecisionType =
  | "confirm_plan"
  | "confirm_method"
  | "confirm_results"
  | "generic";
export type ProjectMode =
  | "learning"
  | "collaboration"
  | "auto_experiment"
  | "review"
  | "organization";

export type FailureClass =
  | "TRANSIENT"
  | "TOOL_ENV"
  | "CODE_DEFECT"
  | "METHOD_INVALID"
  | "DATA_DEFECT"
  | "EVIDENCE_GAP"
  | "POLICY_BLOCK"
  | "NON_PROGRESS";

export interface Budget {
  max_wall_time_s?: number | null;
  max_model_calls?: number | null;
  cost_limit_usd?: number | null;
}

export interface RunFailure {
  failure_class: FailureClass | (string & {});
  message: string;
}

export interface Project {
  id: string;
  name: string;
  /** MVP 单用户为 local-dev；接入认证后为用户 ID。 */
  owner: string;
  mode: ProjectMode | (string & {});
  competition_policy?: string | null;
  workspace_uri?: string | null;
  description?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskRun {
  id: string;
  project_id: string;
  goal: string;
  workflow_version: string;
  status: TaskRunStatus | (string & {});
  /** 领域阶段；消费方必须容忍未知节点名。 */
  current_node: string;
  budget?: Budget | null;
  params?: Record<string, unknown> | null;
  parent_run_id?: string | null;
  failure?: RunFailure | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface StepRun {
  id: string;
  run_id: string;
  node: string;
  attempt: number;
  status: StepRunStatus | (string & {});
  input_hash?: string | null;
  failure_class?: FailureClass | null;
  failure_message?: string | null;
  created_at: string;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface AgentEvent {
  id: string;
  run_id: string;
  /** 单调递增，(run_id, sequence) 唯一；SSE 以此为 Last-Event-ID。 */
  sequence: number;
  step_id?: string | null;
  /** 消费者必须容忍未知事件类型：渲染为通用条目，不崩溃、不静默丢弃。 */
  type: AgentEventType | (string & {});
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  project_id: string;
  run_id?: string | null;
  kind: ArtifactKind | (string & {});
  uri: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
  producer_step_id?: string | null;
  /** 上游 Artifact 血缘；失效传播沿此边计算。 */
  inputs: string[];
  status: ArtifactStatus | (string & {});
  created_at: string;
}

export interface ApprovalOption {
  id: string;
  label: string;
  description?: string | null;
}

export interface ApprovalResolution {
  option_id: string;
  actor: string;
  comment?: string | null;
  resolved_at: string;
}

export interface ApprovalRequest {
  id: string;
  run_id: string;
  step_id?: string | null;
  decision_type: ApprovalDecisionType | (string & {});
  title: string;
  description?: string | null;
  options: ApprovalOption[];
  evidence_snapshot_id?: string | null;
  status: ApprovalStatus | (string & {});
  resolution?: ApprovalResolution | null;
  expires_at?: string | null;
  created_at: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  request_id: string;
  details?: Record<string, unknown> | unknown[] | null;
}

// ---- 请求载荷（与 src/omm_contracts/inputs.py 对齐） ----

export interface CreateProjectInput {
  name: string;
  description?: string | null;
  mode?: ProjectMode;
  competition_policy?: string | null;
  workspace_uri?: string | null;
}

export interface CreateTaskRunInput {
  project_id: string;
  goal: string;
  workflow_version?: string;
  budget?: Budget | null;
  params?: Record<string, unknown> | null;
  /** 创建后立即开始推进。 */
  auto_start?: boolean;
}

export interface TaskRunActionInput {
  action: TaskRunAction;
  approval_id?: string | null;
  option_id?: string | null;
  comment?: string | null;
  /** 客户端幂等令牌；同令牌重复提交返回同一结果。 */
  client_token?: string | null;
}

// ---- 响应包装 ----

export interface ListResponse<T> {
  items: T[];
  total?: number;
}
