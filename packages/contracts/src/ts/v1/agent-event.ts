/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 供 Web/Desktop 实时呈现的统一事件信封。数据库事件表是时间线事实来源（当前默认 SQLite，目标部署 PostgreSQL）；sequence 在 run 内单调递增且唯一，SSE 的事件 id 即 sequence。
 */
export interface AgentEvent {
  id: string;
  run_id: string;
  sequence: number;
  step_id?: null | string;
  type:
    | "run.created"
    | "run.status_changed"
    | "run.node_changed"
    | "run.log"
    | "step.started"
    | "step.succeeded"
    | "step.failed"
    | "approval.requested"
    | "approval.resolved"
    | "artifact.published";
  /**
   * 按 type 解释的载荷。消费方必须容忍未知字段。
   */
  payload: {};
  created_at: Timestamp;
}
