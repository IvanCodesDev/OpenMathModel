/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type RunId = string;
/**
 * 失败分类，见规划文档 §8.6。
 */
export type FailureClass =
  | "TRANSIENT"
  | "TOOL_ENV"
  | "CODE_DEFECT"
  | "METHOD_INVALID"
  | "DATA_DEFECT"
  | "EVIDENCE_GAP"
  | "POLICY_BLOCK"
  | "NON_PROGRESS";
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;
export type NullableTimestamp = null | string;

/**
 * 一次可暂停、恢复、重试、分支的 Agent 运行。status 是稳定生命周期枚举；current_node 是随 workflow_version 演进的领域阶段，消费方必须容忍未知节点名。
 */
export interface TaskRun {
  id: RunId;
  project_id: string;
  goal: string;
  /**
   * 工作流定义版本。首个模拟实现为 sim-0.1，节点集合：CREATED, PROBLEM_ANALYSIS, DATA_PREPARATION, MODEL_PLANNING, EXPERIMENTING, VALIDATING, PAPER_WRITING, COMPLETED。
   */
  workflow_version: string;
  status: "QUEUED" | "RUNNING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  current_node: string;
  budget?: null | {
    max_wall_time_s?: number;
    max_model_calls?: number;
    cost_limit_usd?: number;
  };
  /**
   * 运行输入参数（题目引用、模拟钩子等），按 workflow_version 解释。
   */
  params?: {} | null;
  /**
   * 分支来源运行（规划文档 §7.1 的 parent_branch）。
   */
  parent_run_id?: null | RunId;
  failure?: null | {
    failure_class: FailureClass;
    message: string;
  };
  created_at: Timestamp;
  updated_at: Timestamp;
  started_at?: NullableTimestamp;
  ended_at?: NullableTimestamp;
}
