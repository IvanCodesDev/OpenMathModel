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

/**
 * 图表、数据、代码、日志、论文等交付物的元数据。内容本体在对象存储中内容寻址，服务端负责重新计算并核验 sha256。
 */
export interface Artifact {
  id: string;
  project_id: string;
  run_id?: null | string;
  kind: "dataset" | "code" | "figure" | "table" | "log" | "report" | "paper" | "model" | "other";
  uri: string;
  sha256: Sha256;
  size_bytes: number;
  media_type: string;
  producer_step_id?: null | string;
  /**
   * 上游 Artifact 血缘；失效传播沿此边计算。
   */
  inputs: string[];
  status: "PENDING" | "READY" | "STALE" | "DELETED";
  created_at: Timestamp;
}
