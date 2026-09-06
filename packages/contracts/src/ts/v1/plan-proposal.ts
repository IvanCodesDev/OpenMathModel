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
   * 候选方案列表（归约后 1~3 套：A 主候选 / B 可用基线 / C 条件回退；无监督者的单次调用路径为 A/B 两套）。
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
  /**
   * 模型假设表（H3）：全局假设（scope=global）与各方案特定假设（scope=方案 id），由方案阶段归约后的一次规范化调用整理，供方案页「模型假设」分页与后续论文引用。节点未产出（2026-09-05 之前的运行、模拟节点、规范化调用失败）时为 null。可选字段：旧消费者可忽略。
   */
  assumptions?: null | Assumption[];
  /**
   * 符号表（H3）：题面共有的集合 / 参数（plan_id=null）与各方案自己的决策变量 / 目标（plan_id=方案 id），共享符号在前。未产出时为 null。可选字段：旧消费者可忽略。
   */
  symbols?: null | Symbol[];
  /**
   * G1 决策台账（H3）：用户对**这一版**方案的正向确认（采用推荐案 / 改用某备选案），按提出审批的那一趟节点对上；等待审批、无人值守、旧运行、退回重做中均为 null。拒绝不落台账：它导致方案阶段重做，新版本对应新的审批。可选字段：旧消费者可忽略。
   */
  decision?: null | PlanDecision;
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
  /**
   * 实现语言（小写标识：python / r / matlab / octave / julia …），随 G1 一并确认并决定实验阶段的执行器路由（设计 §7.4）；节点只在当前执行器可用的语言里选。可选字段：2026-09-06 之前的运行没有该键或为 null，消费者按 python 理解。
   */
  language?: string | null;
}
export interface Assumption {
  /**
   * 表内稳定编号：全局假设 G1、G2…，方案特定假设 <方案 id>1、<方案 id>2…（如 A1、B1）。
   */
  id: string;
  /**
   * 假设陈述（一句话）。
   */
  text: string;
  /**
   * 适用范围："global" 或 plans 中某个方案的 id。
   */
  scope: string;
  /**
   * 依据：题面 / 数据画像 / 领域常识 / 简化需要；空串表示节点未给出。
   */
  basis: string;
  /**
   * 假设不成立时对结论的影响程度。
   */
  impact: "low" | "medium" | "high";
  /**
   * confirmed = 题面或数据直接支持；to_verify = 需在实验中用数据检验；critical = 影响大且需做敏感性 / 稳健性分析。
   */
  status: "confirmed" | "to_verify" | "critical";
}
export interface Symbol {
  /**
   * 符号本身，LaTeX 记法、不带 $ 定界（如 x_{ijt}、\mathcal{I}）；前端按行内公式排版。
   */
  symbol: string;
  /**
   * 集合 / 参数（含常数与输入数据）/ 决策变量或状态量 / 目标函数 / 其它。
   */
  kind: "set" | "parameter" | "variable" | "objective" | "other";
  /**
   * 含义（一句话）。
   */
  definition: string;
  /**
   * 单位；无量纲或不适用为 null。
   */
  unit: string | null;
  /**
   * 取值范围或定义域（如「非负整数」「0…K_i」）；不适用为 null。
   */
  range: string | null;
  /**
   * 所属方案 id；题面共有的集合 / 参数为 null。
   */
  plan_id: string | null;
}
export interface PlanDecision {
  /**
   * 对应的审批请求 id（GET /task-runs/{run_id}/approvals 可回看全部选项与证据）。
   */
  approval_id: string;
  /**
   * 用户所选审批项："approve"（采用推荐案）或 "adopt:<方案 id>"（改用某备选案）。
   */
  option_id: string;
  /**
   * 据此进入实验的方案 id（与下游节点选案规则一致：adopt 目标 → 推荐案 → 首案），前端不必再复现规则。
   */
  chosen_plan_id: string;
  /**
   * 决策者标识：服务端审批解决记录里的原值（与 approvals 的 resolution.actor 同一份，通常是账户标识），投影不改写。
   */
  actor: string;
  /**
   * 用户随审批填写的备注原文；未填为 null。AI 不得改写。
   */
  comment: string | null;
  resolved_at: Timestamp;
}
