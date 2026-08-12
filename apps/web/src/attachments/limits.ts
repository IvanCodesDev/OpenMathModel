/** 附件体积与数量护栏。上限同时约束浏览器解析与服务端上传，两侧必须一致。 */

/** 与后端 `settings.artifact_max_bytes` 对齐，超出会被 413 拒绝。 */
export const MAX_FILE_BYTES = 50 * 1024 * 1024;
export const MAX_FILE_COUNT = 20;
export const MAX_TOTAL_BYTES = 200 * 1024 * 1024;

/**
 * 浏览器内解析的体积上限。超过就直接排给服务端：解析全在主线程上跑，
 * 几十兆的 PDF 会把输入框卡到没法打字，宁可让用户先看到卡片再等服务端结果。
 */
export const MAX_CLIENT_PARSE_BYTES = 16 * 1024 * 1024;

/** PDF 逐页抽取的页数上限，超出部分标记为部分解析。 */
export const MAX_PDF_PAGES = 200;
/** 单个工作表抽取的行数上限。 */
export const MAX_SHEET_ROWS = 2000;
/** 压缩包内自动展开的文本条目数上限。 */
export const MAX_ARCHIVE_TEXT_ENTRIES = 8;

/** 单个附件在内存里保留的提取文本上限。 */
export const MAX_EXTRACTED_CHARS = 200_000;

/**
 * 写进任务草稿的摘要上限。草稿落在 sessionStorage（通常 5MB），提取全文动辄
 * 几十万字符，塞进去会直接把草稿写失败，因此只带摘要，全文随二进制上传由
 * 服务端重新解析。
 */
export const MAX_DRAFT_EXCERPT_CHARS = 4_000;
export const MAX_DRAFT_EXCERPT_TOTAL = 24_000;

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}
