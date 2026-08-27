/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type RunId = string;
/**
 * 检验总体结论：pass 可信 / concerns 可用但有保留 / fail 不可信需重做。
 */
export type Verdict = "pass" | "concerns" | "fail";
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 最终成果页正文投影：本次运行的成果交付清单。数据源是 artifacts 表（run 产出的产物列表）与各阶段最新成功输出（题目标题、实验关键指标、检验结论、论文引用）。运行尚无任何可交付内容时整体为 null，由 stage-outputs 端点表达。
 */
export interface DeliveryManifest {
  run_id: RunId;
  /**
   * PROBLEM_ANALYSIS 提取的任务标题；模拟链或阶段未完成时为 null。
   */
  problem_title: string | null;
  /**
   * 本次运行产出的交付物（按创建顺序）；投影形状与 ModelingWorkspaceView.artifacts 一致。
   */
  artifacts: ArtifactProjection[];
  /**
   * EXPERIMENTING 阶段的核心指标（自由载荷：指标名 → 数值）；实验未完成时为 null，脚本未打印指标时为空对象。
   */
  key_metrics: {} | null;
  /**
   * VALIDATING 阶段的总体结论；检验未完成时为 null。
   */
  validation_verdict: null | Verdict;
  /**
   * 论文引用（标题、摘要、关键词与草稿产物指引）；论文阶段未完成时为 null。
   */
  paper_citation: null | PaperCitation;
  updated_at: Timestamp;
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
export interface PaperCitation {
  /**
   * 论文标题。
   */
  title: string;
  /**
   * 论文摘要。
   */
  abstract: string | null;
  /**
   * 关键词；未给出时为空列表。
   */
  keywords: string[];
  /**
   * 论文草稿产物（kind=paper）；沿 /v1/artifacts/{id}/download 获取本体。
   */
  artifact_id: null | string;
}
