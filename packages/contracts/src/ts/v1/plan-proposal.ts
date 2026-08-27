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
 * 建模方案页正文投影：MODEL_PLANNING 阶段真实 LLM 节点的最新成功输出（审批门的确认对象）。节点侧已保证 recommended_plan_id 指向 plans 中的方案；llm_attempts 等过程杂项不进入本投影。
 */
export interface PlanProposal {
  run_id: RunId;
  /**
   * 候选方案列表（当前提示词约定为 A/B 两套）。
   *
   * @minItems 1
   */
  plans: [PlanOption, ...PlanOption[]];
  /**
   * 推荐方案的 id；审批「采用当前方案」即采纳该方案。
   */
  recommended_plan_id: string;
  /**
   * 推荐理由（与数据规模、约束和评审标准的匹配度）；节点未给出时为 null。
   */
  rationale: string | null;
  updated_at: Timestamp;
}
export interface PlanOption {
  /**
   * 方案标识（如 A / B）。
   */
  id: string;
  /**
   * 方法名。
   */
  name: string;
  /**
   * 核心思路与数学工具。
   */
  approach: string;
  /**
   * 可执行的实验步骤（能直接转成 Python 实验）。
   */
  steps: string[];
  /**
   * 该方案的主要风险与失效条件。
   */
  risks: string[];
}
