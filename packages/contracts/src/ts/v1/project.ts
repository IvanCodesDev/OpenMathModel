/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type ProjectId = string;
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 一个持续存在的建模项目。领域对象事实来源为 PostgreSQL，本契约描述 API 对外表示。
 */
export interface Project {
  id: ProjectId;
  name: string;
  /**
   * 所有者标识。MVP 单用户阶段固定为 local-dev，接入认证后为用户 ID。
   */
  owner: string;
  /**
   * 产品模式，见规划文档 §1.2。
   */
  mode: "learning" | "collaboration" | "auto_experiment" | "review" | "organization";
  /**
   * CompetitionPolicyProfile 引用 ID；MVP 允许为空。
   */
  competition_policy?: string | null;
  /**
   * 工作区根 URI（对象存储前缀或本地路径）。
   */
  workspace_uri?: string | null;
  description?: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}
