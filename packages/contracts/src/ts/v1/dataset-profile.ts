/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type RunId = string;
/**
 * 清洗沙盒最终采用波的验收结论：passed 断言全过（cleaned/ 有产物且影响面标记行合格）/ failed 到预算仍未过。
 */
export type CleaningStatus = "passed" | "failed";
/**
 * 审稿结论：accept 通过 / reject 驳回（驳回成立须至少一条 blocker，否则节点按 accept 记）。
 */
export type ReviewVerdict = "accept" | "reject";
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
  /**
   * 清洗脚本的执行结论（沙盒子代理按准备方案清洗 data/ → cleaned/：影响面统计由脚本标记行给出、节点只做除法与求交）与独立审稿结论（生成者-评审者环）。数据节点未产出该字段（该字段出现之前的运行、模拟节点）时为 null；执行被跳过时 executed=false 并给原因。可选字段：旧消费者可忽略。
   */
  cleaning?: null | CleaningReport;
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
export interface CleaningReport {
  /**
   * 清洗脚本是否真的执行过。false 时 status 为 null、数字为 0、列表为空、reason 说明原因（未配置工具 / 监督者 / 会话端口、无数据文件、预算不足、子代理未完成）。
   */
  executed: boolean;
  /**
   * 最终采用波的验收结论；未执行时为 null。
   */
  status: null | CleaningStatus;
  /**
   * 未执行的原因；执行过为空串。
   */
  reason: string;
  /**
   * 沙盒波次（首波 + 按审稿意见的修复波）；未执行为 0。
   */
  attempts: number;
  /**
   * 清洗前数据行数（脚本标记行 OMM_METRICS_JSON.rows_before）。
   */
  rows_before: number;
  /**
   * 清洗后数据行数（脚本标记行 rows_after）。
   */
  rows_after: number;
  /**
   * 删行比例 = 1 − rows_after / rows_before（节点按标记行计算，不信脚本自述）；G2 阈值 0.05。
   */
  rows_deleted_ratio: number;
  /**
   * 被插补的列（脚本标记行 imputed_columns）；无则空列表。
   */
  imputed_columns: string[];
  /**
   * 被插补的列中属于目标列的部分（大小写不敏感求交）；非空即触发 G2。
   */
  imputed_target_columns: string[];
  /**
   * 清洗工程师（沙盒子代理）终答里的一句话自述；未执行为空串。
   */
  summary: string;
  /**
   * 清洗脚本的独立审稿结论（生成者-评审者环：节点确定性复跑核对 + 只读审稿子代理；驳回退修、修不动即僵持交 G2 裁定，僵持时推荐改用原始数据）。首波未过验收、未执行、或审稿环之前的运行时为 null。
   */
  review: null | ReviewReport;
}
export interface ReviewReport {
  /**
   * 独立审稿是否真的进行过（至少一轮审稿子代理给出了有效终答）。false 时 verdict 为 null、findings 为空、reason 说明原因（未配置监督者 / 预算不足 / 子代理未完成）。
   */
  executed: boolean;
  /**
   * 最后一轮审稿人的结论；未执行时为 null。
   */
  verdict: null | ReviewVerdict;
  /**
   * 审稿轮数（含驳回修复后的复审）；未派出过为 0。
   */
  rounds: number;
  /**
   * 最后一轮审稿意见（blocker 在前、同级保序）；未执行时为空列表。
   */
  findings: ReviewFinding[];
  /**
   * 阻断性意见条数（= findings 中 severity=blocker 的条数）。
   */
  blockers: number;
  /**
   * 审稿人的一句话总结；未执行时为空串。
   */
  summary: string;
  /**
   * 僵持：驳回后修复预算已尽 / 修复波未过验收 / 修复后复审未完成 / 达到轮次上限仍有 blocker。true 时保留最后一波通过验收的结果，交由该阶段的人工闸门（结果采用 / 数据确认）裁定。
   */
  stalemate: boolean;
  /**
   * 节点用同一份最终脚本确定性复跑一次并逐键比对该阶段的关键数字（实验 / 检验：核心指标；清洗：影响面标记行）：true 一致 / false 不一致（可复现性存疑）/ null 未复跑（预算不足或脚本正文缺失）。
   */
  rerun_consistent: boolean | null;
  /**
   * 未执行或僵持的原因；正常通过时为空串。
   */
  reason: string;
}
export interface ReviewFinding {
  /**
   * 意见编号（审稿人给出，缺省按序补 R1、R2…）。
   */
  id: string;
  /**
   * 严重度：blocker 会让结果不可信（一票驳回的依据）/ major 应修 / minor 建议。
   */
  severity: "blocker" | "major" | "minor";
  /**
   * 问题位置（文件 / 函数 / 行）；无则空串。
   */
  location: string;
  /**
   * 问题描述，一句话。
   */
  issue: string;
  /**
   * 修法建议；无则空串。
   */
  fix_hint: string;
}
