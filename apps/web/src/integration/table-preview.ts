/**
 * 数据页「原始数据」分页与「清洗后预览」的纯数据整形（不碰 DOM，node --test 直接断言）：
 * - dataset-profile 契约的 inputs（H4 切片 s27：运行参数里的附件 → 产物登记表；staged = 是否满足下发到
 *   沙盒 data/ 的条件）→ 文件行（大小 / 短哈希 / 下载 / 未下发标记）；
 * - `/api/v1/artifacts/{id}/preview` 的载荷 → 表头 / 行 / 「前 N 行 · 共 M 行」文案。
 *
 * 文案只产出中文源串 / 契约原文；调用方按片段 t() 翻译后再拼接。
 */

import type { DatasetProfile } from "@openmathmodel/contracts";

import type { ArtifactPreviewPayload } from "./modeling-workspace-api";

export type DataInput = NonNullable<DatasetProfile["inputs"]>[number];

/** 字节数 → 「12.3 KB」；未知 → "—"（与清洗数据分页同一写法）。 */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

export function shortHash(sha256: string | null | undefined): string {
  const text = String(sha256 ?? "").trim();
  return text ? `${text.slice(0, 12)}…` : "—";
}

/** 只有分隔符文本才去请求预览（与服务端 is_previewable_table 同口径；xlsx 等直接标「不支持预览」）。 */
export function isPreviewableName(name: string, mediaType: string | null | undefined): boolean {
  const lowered = name.toLowerCase();
  const media = String(mediaType ?? "").toLowerCase();
  if (!/\.(csv|tsv|txt)$/.test(lowered)) return false;
  return media === "" || media.startsWith("text/") || media === "application/csv"
    || media === "application/vnd.ms-excel" || media === "application/octet-stream";
}

export interface InputRow {
  artifactId: string;
  name: string;
  size: string;
  hash: string;
  downloadUrl: string | null;
  staged: boolean;
  /** 可下载且是分隔符文本 → 前端会去拉前 N 行。 */
  previewable: boolean;
}

export function inputRows(inputs: readonly DataInput[] | null | undefined): InputRow[] {
  return (inputs ?? []).map(input => ({
    artifactId: input.artifact_id,
    name: input.name,
    size: formatSize(input.size_bytes),
    hash: shortHash(input.sha256),
    downloadUrl: input.download_url ?? null,
    staged: input.staged,
    previewable: Boolean(input.download_url) && isPreviewableName(input.name, input.media_type),
  }));
}

export type RawDataView =
  /** 契约字段缺席（该字段出现之前的运行）：分页不出现。 */
  | { kind: "absent" }
  /** 字段在但为空：本次运行没有下发数据文件。 */
  | { kind: "empty" }
  | { kind: "inputs"; rows: InputRow[]; total: number; staged: number; previewable: number };

export function describeRawData(profile: DatasetProfile): RawDataView {
  const inputs = profile.inputs;
  if (!Array.isArray(inputs)) return { kind: "absent" };
  if (inputs.length === 0) return { kind: "empty" };
  const rows = inputRows(inputs);
  return {
    kind: "inputs",
    rows,
    total: rows.length,
    staged: rows.filter(row => row.staged).length,
    previewable: rows.filter(row => row.previewable).length,
  };
}

export interface PreviewTable {
  columns: string[];
  rows: string[][];
  /** 中文源串片段：["前", "N 行", "共 M 行"] 之类由调用方 t() 后拼；这里给结构化数字。 */
  shown: number;
  rowCount: number | null;
  truncated: boolean;
  encoding: string;
  delimiter: string;
}

/** 分隔符的可读名（调用方 t()）。 */
export function delimiterLabel(delimiter: string): string {
  if (delimiter === ",") return "逗号";
  if (delimiter === ";") return "分号";
  if (delimiter === "\t") return "制表符";
  if (delimiter === "|") return "竖线";
  return delimiter;
}

export function previewTable(payload: ArtifactPreviewPayload): PreviewTable {
  const width = payload.columns.length;
  const rows = payload.rows.map(row => {
    if (!width) return [...row];
    if (row.length === width) return [...row];
    if (row.length < width) return [...row, ...new Array<string>(width - row.length).fill("")];
    return row.slice(0, width);
  });
  return {
    columns: [...payload.columns],
    rows,
    shown: rows.length,
    rowCount: payload.row_count,
    truncated: payload.truncated,
    encoding: payload.encoding,
    delimiter: payload.delimiter,
  };
}
