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
 * 实验与验证页正文投影：EXPERIMENTING 阶段真实节点（LLM 生成代码 + python 沙箱执行）的最新成功输出；validation 为同页 VALIDATING 阶段的检验报告，检验未完成时为 null。
 */
export interface ExperimentSummary {
  run_id: RunId;
  /**
   * 实现思路摘要（算法、数据构造方式、评估口径）。
   */
  approach_summary: string;
  /**
   * 实验脚本按 OMM_METRICS_JSON 标记行打印的核心指标（自由载荷：指标名 → 数值），消费方容忍未知指标名；脚本未打印时为空对象。
   */
  metrics: {};
  /**
   * 实验脚本标准输出尾部（节点侧截断）。
   */
  stdout_tail: string;
  /**
   * 实验过程摘要（思路 + 核心指标 + 产物文件），供检验与论文阶段引用。
   */
  experiment_summary: string;
  /**
   * VALIDATING 阶段的检验报告；该阶段未成功完成时为 null。
   */
  validation: null | ValidationReport;
  updated_at: Timestamp;
}
export interface ValidationReport {
  verdict: Verdict;
  /**
   * 逐项检查列表。
   */
  checks: ValidationCheck[];
  /**
   * 主要风险与失效条件；节点未给出时为空列表。
   */
  risks: string[];
  /**
   * 一段话的检验结论（如实包含保留意见），供论文阶段引用。
   */
  validation_summary: string;
}
export interface ValidationCheck {
  /**
   * 检查名（结果合理性、稳健性等）。
   */
  name: string;
  /**
   * 该项检查结论。
   */
  result: "pass" | "warn" | "fail";
  /**
   * 依据，一句话。
   */
  note: string;
}
