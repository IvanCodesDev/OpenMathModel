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
 * 读题结果正文投影：PROBLEM_ANALYSIS 阶段真实 LLM 节点的最新成功输出（run_domain_events 的 STEP_SUCCEEDED）。subquestions 是后续子问题并行（map lane）的展开依据。模拟链或阶段未完成时该投影整体为 null，由 stage-outputs 端点表达。
 */
export interface ProblemFrame {
  run_id: RunId;
  /**
   * 不超过 20 字的任务标题，概括实际要解决的核心问题。
   */
  title: string;
  /**
   * 问题类型（如 优化 / 预测 / 评价 / 机理建模 / 混合）。
   */
  problem_type: string;
  /**
   * 需要回答的目标问题列表，逐条对应题目小问。
   */
  objectives: string[];
  /**
   * 题目明确给出的约束与边界条件列表；无则为空列表。
   */
  constraints: string[];
  /**
   * 完成建模需要的数据清单（含题目附带与需自行收集）；无则为空列表。
   */
  data_requirements: string[];
  /**
   * 为使问题可解而显式声明的关键假设列表；无则为空列表。
   */
  key_assumptions: string[];
  /**
   * 子问题分解（子问题并行 lane 的展开依据）；题目不可分解时为覆盖全题的单条；旧运行未产出时为空列表。
   */
  subquestions: Subquestion[];
  updated_at: Timestamp;
}
export interface Subquestion {
  /**
   * 子问题标识（如 q1）。
   */
  id: string;
  /**
   * 子问题的一句话描述。
   */
  text: string;
  /**
   * 依赖的子问题 id 列表；无依赖为空列表。
   */
  depends_on: string[];
}
