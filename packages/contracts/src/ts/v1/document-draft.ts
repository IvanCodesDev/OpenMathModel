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
 * 论文编辑页正文投影：PAPER_WRITING 阶段真实 LLM 节点的最新成功输出（结构化论文草稿）。version/updated_at 支撑后续论文编辑的版本演进；markdown 产物本体沿 Artifact 下载链路获取。
 */
export interface DocumentDraft {
  run_id: RunId;
  /**
   * 论文标题。
   */
  title: string;
  /**
   * 摘要（问题、方法、核心结果、结论）。
   */
  abstract: string;
  /**
   * 关键词；节点未给出时为空列表。
   */
  keywords: string[];
  /**
   * 章节列表，按论文顺序排列。
   */
  sections: PaperSection[];
  /**
   * 草稿版本号：PAPER_WRITING 阶段每次成功产出递增（重试/重跑产生新版本）。
   */
  version: number;
  updated_at: Timestamp;
}
export interface PaperSection {
  /**
   * 章节标题。
   */
  heading: string;
  /**
   * 正文 Markdown（可含列表与表格）。
   */
  content: string;
}
