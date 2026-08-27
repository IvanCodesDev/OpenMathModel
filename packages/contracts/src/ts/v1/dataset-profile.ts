/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type RunId = string;
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 数据准备页正文投影：DATA_PREPARATION 阶段真实 LLM 节点的最新成功输出（run_domain_events 的 STEP_SUCCEEDED）。模拟链或阶段未完成时该投影整体为 null，由 stage-outputs 端点表达。
 */
export interface DatasetProfile {
  run_id: RunId;
  /**
   * 一段话的数据画像摘要（数据构成、规模量级、质量状况与可用性结论）。
   */
  profile_summary: string;
  /**
   * 数据清单；题目未附数据时 source 注明「需收集」或「需构造」。
   */
  datasets: DatasetEntry[];
  /**
   * 可执行的数据准备步骤，按执行顺序排列。
   */
  preparation_steps: string[];
  /**
   * 缺失值处理策略与理由；节点未给出时为 null。
   */
  missing_value_strategy: string | null;
  /**
   * 异常值识别与处理策略；节点未给出时为 null。
   */
  outlier_strategy: string | null;
  /**
   * 建议构造的衍生变量（含构造方式）；节点未给出时为空列表。
   */
  derived_features: string[];
  updated_at: Timestamp;
}
export interface DatasetEntry {
  /**
   * 数据集名。
   */
  name: string;
  /**
   * 来源：题目附件 / 需收集 / 需构造。
   */
  source: string;
  /**
   * 字段清单（含字段含义与单位）。
   */
  fields: string[];
  /**
   * 该数据集的质量风险（缺失、异常、口径不一等）；无则为空列表。
   */
  quality_risks: string[];
}
