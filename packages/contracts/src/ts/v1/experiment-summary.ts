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
   * VALIDATING 阶段的检验报告（评审判读 + 沙盒复跑的稳健性检查）；该阶段未成功完成时为 null。
   */
  validation: null | ValidationReport;
  updated_at: Timestamp;
}
export interface ValidationReport {
  verdict: Verdict;
  /**
   * 逐项检查列表（评审判读，模型给出）。
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
  /**
   * 沙盒复跑的稳健性检查（G3 结果采用闸门的判定依据），数字来自检验脚本的标记行而非模型转述；验证节点未产出该字段（沙盒化之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。
   */
  robustness?: null | RobustnessReport;
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
export interface RobustnessReport {
  /**
   * 沙盒复跑是否真的执行过。false 时 checks 为空列表、reason 说明原因（工具 / 监督者 / 会话出口 / 实验脚本 / 预算任一缺席，或子代理未完成）。
   */
  executed: boolean;
  /**
   * 沙盒会话终态（sandbox-run-report 的 status，如 passed / failed）；未执行时为 null。非 passed 时 checks 为空、G3 不触发。
   */
  status: string | null;
  /**
   * 供论文引用的一句话稳健性结论，数字只来自标记行（不是模型转述）；未执行时为空串。
   */
  summary_text: string;
  /**
   * 逐项稳健性检查；未执行或复跑未成功时为空列表。
   */
  checks: RobustnessCheck[];
  /**
   * 检查项总数（= checks 长度）。
   */
  checks_total: number;
  /**
   * 未通过项数；≥1 即触发 G3 结果采用闸门。
   */
  checks_failed: number;
  /**
   * 未执行 / 未完成的原因（executed=false 时非空）；执行成功时为空串。
   */
  reason: string;
}
export interface RobustnessCheck {
  /**
   * 检查项 id（脚本自定，稳定可引用）。
   */
  id: string;
  /**
   * 检查名（扰动敏感性、重采样稳定性、对基线优势幅度等）。
   */
  name: string;
  /**
   * 是否达标：脚本按写死在代码里的阈值判定。
   */
  passed: boolean;
  /**
   * 实测值（来自标记行）；脚本未给数值时为 null。
   */
  value: number | null;
  /**
   * 判定阈值：数值，或脚本给出的文字口径（如「≤ 0.05」）；未给时为 null。
   */
  threshold: number | string | null;
  /**
   * 依据，一句话；无则空串。
   */
  detail: string;
}
