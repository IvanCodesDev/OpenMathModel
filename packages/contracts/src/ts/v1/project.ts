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
export type RunId = string;

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
  /**
   * 列表统计投影：仅 GET /v1/projects?include=stats 计算并返回对象，其余端点为 null 或缺省。服务端一次聚合，客户端不再按项目逐个拉取运行与产物。
   */
  stats?: null | ProjectStats;
}
export interface ProjectStats {
  /**
   * 该项目最新一次运行的轻量投影（按创建时间取最近）；从未发起运行时为 null。
   */
  latest_run: null | {
    id: RunId;
    status: "QUEUED" | "RUNNING" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
    /**
     * 领域阶段节点，随 workflow_version 演进；消费方必须容忍未知节点名。
     */
    current_node: string;
    goal: string;
    updated_at: Timestamp;
  };
  /**
   * 项目产物总条数（运行产出与手动上传都计入）。
   */
  artifact_count: number;
}
