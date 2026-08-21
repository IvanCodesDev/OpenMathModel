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
 * 论文导出任务（ADR-0012 阶段 A）：客户端提交完整 .tex 源，服务端排队编译 PDF。tex 源与 PDF 都是 kind=paper 的 Artifact，PDF 的 inputs 指向 tex 源；下载沿用 /v1/artifacts/{id}/download。
 */
export interface PaperExport {
  id: string;
  project_id: string;
  /**
   * 关联的工作台运行；带 run_id 的导出完成时沿 run 事件流追加 paper.export.finished。
   */
  run_id?: null | string;
  /**
   * pdf = 排队编译；tex = 只落源产物并立即 READY。
   */
  format: "pdf" | "tex";
  /**
   * UNSUPPORTED = 服务端未安装编译器，诚实降级不伪装成功。
   */
  status: "QUEUED" | "RUNNING" | "READY" | "FAILED" | "UNSUPPORTED";
  /**
   * 交付产物：format=pdf 时为编译出的 PDF，format=tex 时为 tex 源产物。
   */
  artifact_id?: null | string;
  /**
   * 受理时落库的 .tex 源产物；编译失败仍可下载排查。
   */
  source_artifact_id?: null | string;
  /**
   * FAILED 时为编译日志尾部；UNSUPPORTED 时为启用途径说明。
   */
  detail?: null | string;
  created_at: Timestamp;
  started_at?: null | Timestamp;
  ended_at?: null | Timestamp;
}
