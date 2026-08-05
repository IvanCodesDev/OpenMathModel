/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

/**
 * API 统一错误返回。异步动作返回任务状态而不是伪装成同步成功；错误码只增不改、不复用旧码表达新语义。
 */
export interface ErrorEnvelope {
  /**
   * 机器可分类错误码，如 VALIDATION_ERROR / NOT_FOUND / CONFLICT / IDEMPOTENCY_KEY_REUSED / INVALID_ACTION。
   */
  code: string;
  message: string;
  request_id: string;
  /**
   * 结构化补充信息（字段级校验错误等），不得包含堆栈或敏感信息。
   */
  details?: {} | unknown[] | null;
}
