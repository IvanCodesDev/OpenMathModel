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
 * 人工确认（HIL）请求。人工确认是正式状态转换：审批解决后运行才能离开 WAITING_APPROVAL。
 */
export interface ApprovalRequest {
  id: string;
  run_id: string;
  step_id?: null | string;
  decision_type: "confirm_plan" | "confirm_method" | "confirm_results" | "generic";
  title: string;
  description?: string | null;
  /**
   * @minItems 1
   */
  options: [
    {
      id: string;
      label: string;
      description?: string | null;
    },
    ...{
      id: string;
      label: string;
      description?: string | null;
    }[]
  ];
  /**
   * 审批所依据的证据快照引用；Evidence 体系落地前允许为空。
   */
  evidence_snapshot_id?: string | null;
  status: "PENDING" | "RESOLVED" | "EXPIRED" | "CANCELLED";
  resolution?: null | {
    option_id: string;
    actor: string;
    comment?: string | null;
    resolved_at: Timestamp;
  };
  expires_at?: null | Timestamp;
  created_at: Timestamp;
}
