export const MAX_GOAL_LENGTH = 4000;

const PROJECT_ID_PATTERN = /^proj_[0-9a-f]{32}$/;
const TOKEN_PATTERN = /^[A-Za-z0-9]{16,64}$/;
const ARTIFACT_ID_PATTERN = /^art_[0-9a-f]{32}$/;

/** 与 attachments/parse.ts 的 ParseStatus 对齐；草稿要能独立于解析模块被校验。 */
const PARSE_STATUSES = ["ready", "partial", "server-pending", "empty", "failed"] as const;
export type TaskAttachmentParseStatus = (typeof PARSE_STATUSES)[number];

/** 单个附件带进任务参数的正文摘要上限。 */
export const MAX_ATTACHMENT_EXCERPT = 4000;

export interface TaskAttachmentDraft {
  name: string;
  size: number;
  type: string;
  last_modified: number;
  /** 格式登记名，例如 PDF、Excel */
  format?: string;
  parse_status?: TaskAttachmentParseStatus;
  /** 浏览器抽取到的字符数（不是摘要长度） */
  characters?: number;
  /** 检测到的图片数（近似值，ADR-0010）；权威计数以服务端解析为准 */
  images?: number;
  /** 正文摘要；完整正文以服务端对上传产物的解析为准 */
  excerpt?: string;
  artifact_id?: string;
}

export interface TaskDraft {
  version: 1;
  description: string;
  task_type: string;
  selected_model: string;
  attachments: TaskAttachmentDraft[];
  project_id?: string;
  run_request_token?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function boundedString(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.slice(0, maxLength) : "";
}

export function normalizeTaskDescription(value: string): string {
  return value.replace(/\r\n?/g, "\n").trim();
}

export function deriveProjectName(description: string): string {
  const compact = normalizeTaskDescription(description)
    .replace(/\s+/g, " ")
    .replace(/^(?:请帮我|请|帮我|我想(?:要)?)/, "")
    .trim();
  const firstClause = compact.split(/[。！？!?；;\n]/, 1)[0]?.trim() || "未命名建模任务";
  const characters = Array.from(firstClause);
  return characters.length > 24 ? `${characters.slice(0, 24).join("")}…` : firstClause;
}

export function parseTaskDraft(raw: string | null): TaskDraft | null {
  if (!raw) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(payload) || payload.version !== 1) return null;

  const attachments = Array.isArray(payload.attachments)
    ? payload.attachments.flatMap(item => {
      if (!isRecord(item)) return [];
      const name = boundedString(item.name, 300).trim();
      const size = typeof item.size === "number" && Number.isSafeInteger(item.size) && item.size >= 0
        ? item.size
        : 0;
      const lastModified = typeof item.last_modified === "number" && Number.isFinite(item.last_modified)
        ? item.last_modified
        : 0;
      if (!name) return [];
      const format = boundedString(item.format, 40);
      const status = PARSE_STATUSES.find(candidate => candidate === item.parse_status);
      const characters = typeof item.characters === "number" && Number.isSafeInteger(item.characters) && item.characters >= 0
        ? item.characters
        : 0;
      const images = typeof item.images === "number" && Number.isSafeInteger(item.images) && item.images > 0
        ? item.images
        : 0;
      const excerpt = boundedString(item.excerpt, MAX_ATTACHMENT_EXCERPT);
      const artifactId = boundedString(item.artifact_id, 64);
      return [{
        name,
        size,
        type: boundedString(item.type, 200),
        last_modified: lastModified,
        ...(format ? { format } : {}),
        ...(status ? { parse_status: status } : {}),
        ...(characters ? { characters } : {}),
        ...(images ? { images } : {}),
        ...(excerpt ? { excerpt } : {}),
        ...(ARTIFACT_ID_PATTERN.test(artifactId) ? { artifact_id: artifactId } : {}),
      }];
    })
    : [];

  const projectId = boundedString(payload.project_id, 64);
  const runRequestToken = boundedString(payload.run_request_token, 64);
  return {
    version: 1,
    description: boundedString(payload.description, MAX_GOAL_LENGTH + 1),
    task_type: boundedString(payload.task_type, 40) || "竞赛建模",
    selected_model: boundedString(payload.selected_model, 100) || "auto",
    attachments,
    ...(PROJECT_ID_PATTERN.test(projectId) ? { project_id: projectId } : {}),
    ...(TOKEN_PATTERN.test(runRequestToken) ? { run_request_token: runRequestToken } : {}),
  };
}

export function buildRunningUrl(runId: string, projectId: string): string {
  const params = new URLSearchParams({ run_id: runId, project_id: projectId });
  return `/task/running?${params.toString()}`;
}

/** 首页普通对话的回看地址：首页挂载时据此重建对话现场。 */
export function buildChatUrl(chatId: string): string {
  return `/?${new URLSearchParams({ chat: chatId }).toString()}`;
}
