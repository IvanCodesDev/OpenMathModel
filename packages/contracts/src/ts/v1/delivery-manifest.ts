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
  /**
   * 交付记录（H5 DeliveryManifest 真实化）：G4 定稿闸门的交付状态 + 终稿审计计数 + 确定性一致性检查（论文产物哈希可读、审计 0 发现、已插入图件有产物、实验指标出现在论文里、检验结论在场）+ 文件计数；只有真实论文草稿在场时才有，否则（模拟链、未到论文阶段）为 null。可选字段：旧消费者可忽略。
   */
  delivery?: null | DeliveryRecord;
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
  /**
   * 产物登记时的内容摘要（下载端点按它核验）；交付清单据此做「文件 + 哈希」。可选字段（形状与 ModelingWorkspaceView.artifacts 一致、另带哈希）：旧消费者可忽略；登记缺失时为 null。
   */
  sha256?: null | string;
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
export interface DeliveryRecord {
  /**
   * 交付状态（按产出当前论文那一趟的 G4 审批回放）：pending_confirmation = G4 挂起待人确认；confirmed = 用户「确认交付」；returned_for_revision = 用户「退回修改」（新一版论文到来前保持）；unattended = 论文已发布但该运行没有挂 G4（无人值守 / 评测）；not_ready = 审批过期 / 取消等未成交付的状态。
   */
  status: "not_ready" | "pending_confirmation" | "confirmed" | "returned_for_revision" | "unattended";
  /**
   * 对应的 DocumentDraft.version。
   */
  paper_version: number | null;
  /**
   * G4 审批 id；没挂过 G4 时为 null。
   */
  approval_id: string | null;
  /**
   * 用户确认交付的时间；未确认为 null。
   */
  confirmed_at: null | Timestamp;
  /**
   * 用户在 G4 拍板时留下的备注（确认或退回）；没有为 null。
   */
  comment: string | null;
  /**
   * 终稿审计与图件 / 文献计数（来自 DocumentDraft 的确定性字段）；论文未审计（旧运行）为 null。
   */
  audit: null | DeliveryAudit;
  /**
   * 一致性检查，固定顺序：paper_artifact_ready → audit_clean → figures_delivered → metrics_in_paper → validation_reported。全是代码判定，不经模型。
   */
  checks: DeliveryCheck[];
  /**
   * 交付清单里的产物数（与 artifacts 同口径）。
   */
  files_total: number;
  /**
   * 其中可下载（READY 且内容对象可读、哈希对得上）的产物数。
   */
  files_ready: number;
  /**
   * 其中带登记哈希的产物数。
   */
  files_hashed: number;
}
export interface DeliveryAudit {
  findings_total: number;
  /**
   * 发现类型 → 条数（键取 audit_finding.kind；消费者须容忍未知取值）。
   */
  findings_by_kind: {
    [k: string]: number;
  };
  frozen_numbers_total: number;
  figures_total: number;
  figures_inserted: number;
  references_total: number;
  references_cited: number;
}
export interface DeliveryCheck {
  /**
   * 检查项：论文草稿产物可读且哈希对得上 / 终稿审计 0 发现 / 已插入图件都有可下载产物 / 实验指标（冻结清单 EXPERIMENTING 项）逐个出现在论文里 / 检验结论在场。消费者须容忍未知取值。
   */
  id: "paper_artifact_ready" | "audit_clean" | "figures_delivered" | "metrics_in_paper" | "validation_reported";
  /**
   * 人可读的检查名。
   */
  label: string;
  passed: boolean;
  /**
   * 判定依据（计数、缺失项点名）。
   */
  detail: string;
}
