/**
 * 对话附件的即席解析客户端（ADR-0010 批次三）：POST /api/v1/artifacts/parse。
 *
 * 不建产物、不落库——对话历史本就只存页面内存，附件保持同样的隐私姿态。
 * 服务端抽取链路与产物正文相同（含可选的远程 OCR），图片与扫描件也能
 * 转成模型可读的 Markdown。
 */

export type ServerParseStatus = "ready" | "partial" | "empty" | "unsupported" | "failed";

export interface AdhocParseResult {
  name: string;
  media_type: string;
  status: ServerParseStatus;
  engine: string;
  characters: number;
  segments?: number | null;
  images?: number | null;
  detail?: string | null;
  text: string;
}

/** 远程 OCR 逐页识别 + 大 PDF 抽取可达数十秒，预算给足；但不允许无限等着拖住发送流程。 */
const PARSE_TIMEOUT_MS = 180_000;

/** 网络失败、超时、未登录或服务端错误一律返回 null：调用方按「解析不可用」降级。 */
export async function parseAttachmentOnServer(file: File): Promise<AdhocParseResult | null> {
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/api/v1/artifacts/parse", {
      method: "POST",
      credentials: "same-origin",
      body: form,
      signal: AbortSignal.timeout(PARSE_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as AdhocParseResult;
  } catch {
    return null;
  }
}
