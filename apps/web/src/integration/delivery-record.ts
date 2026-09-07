/**
 * 成果页交付记录的纯数据整形（不碰 DOM，node --test 直接断言）：
 * delivery-manifest 契约的 delivery（H5 DeliveryManifest 真实化——G4 交付状态 + 终稿审计计数 +
 * 五项确定性一致性检查 + 文件计数）与 artifacts[].sha256（文件 + 哈希）→ 摘要行 / 检查行 / 文件行。
 *
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。契约 enum 之外的
 * status / check id 原样透出（消费者须容忍未知取值）。
 */

import type { DeliveryManifest } from "@openmathmodel/contracts";

export type DeliveryRecord = NonNullable<DeliveryManifest["delivery"]>;
export type DeliveryCheck = DeliveryRecord["checks"][number];
export type ManifestArtifact = DeliveryManifest["artifacts"][number];

/** 交付状态 → 一句中文源串（调用方 t()）。 */
export const DELIVERY_STATUS_LABELS: Record<string, string> = {
  not_ready: "尚未成交付",
  pending_confirmation: "等待确认交付",
  confirmed: "已确认交付",
  returned_for_revision: "已退回修改",
  unattended: "已发布，未挂定稿闸门",
};

/** 状态 → 视觉语气（页面只用三档，不给每个状态单独配色）。 */
export type DeliveryTone = "ok" | "warn" | "muted";

export function deliveryTone(status: string): DeliveryTone {
  if (status === "confirmed") return "ok";
  if (status === "pending_confirmation" || status === "returned_for_revision") return "warn";
  return "muted";
}

export interface ChecksSummary {
  passed: number;
  total: number;
}

export function summarizeChecks(checks: readonly DeliveryCheck[]): ChecksSummary {
  return { passed: checks.filter(check => check.passed).length, total: checks.length };
}

/** 登记哈希的短写（前 12 位 + …）；缺失 → "—"。 */
export function shortHash(sha256: string | null | undefined): string {
  const text = String(sha256 ?? "").trim();
  return text ? `${text.slice(0, 12)}…` : "—";
}

export interface FileRow {
  name: string;
  kind: string;
  hash: string;
  ready: boolean;
}

export function fileRows(artifacts: readonly ManifestArtifact[]): FileRow[] {
  return artifacts.map(artifact => ({
    name: artifact.name,
    kind: artifact.kind,
    hash: shortHash(artifact.sha256),
    ready: Boolean(artifact.download_url),
  }));
}

export interface DeliveryView {
  status: string;
  /** 中文源串（调用方 t()）；enum 外的状态原样。 */
  statusLabel: string;
  tone: DeliveryTone;
  confirmedAt: string | null;
  comment: string | null;
  checks: readonly DeliveryCheck[];
  summary: ChecksSummary;
  audit: DeliveryRecord["audit"];
  files: { total: number; ready: number; hashed: number };
  rows: FileRow[];
}

/** 交付记录缺席（模拟链 / 未到论文阶段 / 旧接口）→ null：成果页保持空态，不编一条。 */
export function describeDelivery(manifest: DeliveryManifest): DeliveryView | null {
  const delivery = manifest.delivery ?? null;
  if (!delivery) return null;
  return {
    status: delivery.status,
    statusLabel: DELIVERY_STATUS_LABELS[delivery.status] ?? delivery.status,
    tone: deliveryTone(delivery.status),
    confirmedAt: delivery.confirmed_at ?? null,
    comment: delivery.comment ?? null,
    checks: delivery.checks,
    summary: summarizeChecks(delivery.checks),
    audit: delivery.audit ?? null,
    files: { total: delivery.files_total, ready: delivery.files_ready, hashed: delivery.files_hashed },
    rows: fileRows(manifest.artifacts),
  };
}
