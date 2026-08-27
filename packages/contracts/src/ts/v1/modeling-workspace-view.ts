/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type PageKey = "running" | "data" | "model" | "experiments" | "editor" | "complete";
export type Route =
  | "/task/running"
  | "/workspace/data"
  | "/workspace/model-plan"
  | "/workspace/experiments"
  | "/workspace/paper-editor"
  | "/task/complete";
export type AgentState = "QUEUED" | "WORKING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
export type AgentAction = ApproveAgentAction | NavigateAgentAction | TaskAgentAction | NoneAgentAction;
export type PageStatus = "PENDING" | "RUNNING" | "WAITING_APPROVAL" | "PAUSED" | "SUCCEEDED" | "FAILED" | "CANCELLED";
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 建模运行面向 Web 的语义投影。后端只输出阶段、Agent 文案、动作和产物语义，前端负责映射到既有流程页面与 DOM 样式。
 */
export interface ModelingWorkspaceView {
  run_id: string;
  project_id: string;
  project_name: string;
  goal: string;
  workflow_version: string;
  run_status: "QUEUED" | "RUNNING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  active_node: string;
  active_page: PageKey;
  suggested_route: Route;
  agent: AgentProjection;
  /**
   * @minItems 6
   * @maxItems 6
   */
  pages: {
    [k: string]: unknown;
  } & [PageProjection, PageProjection, PageProjection, PageProjection, PageProjection, PageProjection];
  artifacts: ArtifactProjection[];
  pending_approval: null | ApprovalProjection;
  latest_event_sequence: null | number;
  updated_at: Timestamp;
}
export interface AgentProjection {
  state: AgentState;
  title: string;
  summary: string;
  current_step: string;
  action: AgentAction;
}
export interface ApproveAgentAction {
  kind: "approve";
  label: string;
  target_route: Route;
  approval_id: string;
  option_id: string | null;
}
export interface NavigateAgentAction {
  kind: "navigate";
  label: string;
  target_route: Route;
  approval_id: null;
  option_id: null;
}
export interface TaskAgentAction {
  kind: "pause" | "resume" | "retry";
  label: string;
  target_route: Route;
  approval_id: null;
  option_id: null;
}
export interface NoneAgentAction {
  kind: "none";
  label: string;
  target_route: null;
  approval_id: null;
  option_id: null;
}
export interface PageProjection {
  key: PageKey;
  label: string;
  route: Route;
  /**
   * @minItems 1
   */
  nodes: [string, ...string[]];
  status: PageStatus;
  artifact_ids: string[];
  /**
   * 本任务专属的计划短句（问题分析的 plan_outline 派生，方案确认后实验条目细化为选中方案）；未产出时为 null，展示层回退 label。
   */
  plan_text?: null | string;
}
export interface ArtifactProjection {
  id: string;
  kind: "dataset" | "code" | "figure" | "table" | "log" | "report" | "paper" | "model" | "other";
  name: string;
  media_type: string;
  size_bytes: number | null;
  status: "PENDING" | "READY" | "STALE" | "DELETED";
  producer_node: string | null;
  download_url: null | string;
}
export interface ApprovalProjection {
  id: string;
  title: string;
  description: string | null;
  /**
   * @minItems 1
   */
  options: [ApprovalOption, ...ApprovalOption[]];
}
export interface ApprovalOption {
  id: string;
  label: string;
  description?: string | null;
}
