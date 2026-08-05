/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type Sha256 = string;
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;
export type NullableTimestamp = null | string;

/**
 * 状态机中单个节点的一次执行记录。同一节点重试产生新的 attempt。
 */
export interface StepRun {
  id: string;
  run_id: string;
  node: string;
  attempt: number;
  /**
   * 本次执行输入的内容哈希，幂等与重放依据。
   */
  input_hash?: null | Sha256;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "SKIPPED";
  failure_class?:
    | null
    | (
        | "TRANSIENT"
        | "TOOL_ENV"
        | "CODE_DEFECT"
        | "METHOD_INVALID"
        | "DATA_DEFECT"
        | "EVIDENCE_GAP"
        | "POLICY_BLOCK"
        | "NON_PROGRESS"
      );
  failure_message?: string | null;
  created_at: Timestamp;
  started_at?: NullableTimestamp;
  ended_at?: NullableTimestamp;
}
