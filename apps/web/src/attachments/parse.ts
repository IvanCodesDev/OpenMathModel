/**
 * 附件解析总入口：按格式路由到对应抽取器，统一成一份可展示、可入库的结果。
 *
 * 这一层只负责"浏览器里能拿到什么"。解不动的格式不算失败，而是标成
 * `server-pending`，等二进制上传后由服务端出权威结果——用户看到的是"排队中"
 * 而不是"解析失败"。
 */

import { describeFormat, fileExtension, type FormatDescriptor } from "./formats";
import {
  MAX_ARCHIVE_TEXT_ENTRIES,
  MAX_CLIENT_PARSE_BYTES,
  MAX_EXTRACTED_CHARS,
  formatBytes,
} from "./limits";
import { extractPdfText } from "./pdf-document";
import { decodeBytes, tidyText } from "./text-decode";
import {
  extractArchiveEntries,
  extractDocxText,
  extractOpenDocumentText,
  extractPptxText,
  extractXlsxText,
  listArchiveEntries,
} from "./zip-documents";

export type ParseStatus = "ready" | "partial" | "server-pending" | "empty" | "failed";

/** 卡片副标题上的结构信息，例如"12 页""3 个工作表"。 */
export interface ParseMetric {
  label: string;
  value: string;
}

export interface ParseOutcome {
  status: ParseStatus;
  /** 抽取到的纯文本，已按 MAX_EXTRACTED_CHARS 截断 */
  text: string;
  characters: number;
  metrics: ParseMetric[];
  /** 展示给用户的一句话说明：排队原因、截断提示或失败原因 */
  notice?: string;
}

/** 压缩包内自动展开的单个条目体积上限。 */
const MAX_ARCHIVE_ENTRY_BYTES = 1024 * 1024;
/** 判定 PDF 是否为扫描件的阈值：每页平均可读字符数。 */
const SCANNED_PDF_CHARS_PER_PAGE = 12;

function countLines(text: string): number {
  return text ? text.split("\n").length : 0;
}

interface NotebookCell {
  cell_type?: unknown;
  source?: unknown;
}

function cellSource(cell: NotebookCell): string {
  if (typeof cell.source === "string") return cell.source;
  if (Array.isArray(cell.source)) return cell.source.filter(line => typeof line === "string").join("");
  return "";
}

function extractNotebook(text: string): { text: string; metrics: ParseMetric[] } {
  const payload: unknown = JSON.parse(text);
  const cells = (payload as { cells?: unknown }).cells;
  if (!Array.isArray(cells)) throw new Error("ipynb 缺少 cells 字段");
  const blocks = cells.map((cell: NotebookCell) => {
    const source = cellSource(cell);
    if (!source.trim()) return "";
    return cell.cell_type === "code" ? `\`\`\`\n${source}\n\`\`\`` : source;
  }).filter(Boolean);
  return {
    text: tidyText(blocks.join("\n\n")),
    metrics: [{ label: "单元格", value: `${cells.length} 个` }],
  };
}

function extractDelimited(text: string, extension: string): ParseMetric[] {
  const delimiter = extension === "tsv" ? "\t" : ",";
  const lines = text.split("\n").filter(line => line.trim());
  const columns = lines[0]?.split(delimiter).length ?? 0;
  return [
    { label: "行", value: `${Math.max(lines.length - 1, 0)} 行数据` },
    { label: "列", value: `${columns} 列` },
  ];
}

function extractArchive(bytes: Uint8Array): { text: string; metrics: ParseMetric[]; notice?: string } {
  const entries = listArchiveEntries(bytes);
  const readable = entries
    .filter(entry => entry.size > 0 && entry.size <= MAX_ARCHIVE_ENTRY_BYTES)
    .filter(entry => describeFormat(entry.name, "").route === "text")
    .slice(0, MAX_ARCHIVE_TEXT_ENTRIES);
  const contents = extractArchiveEntries(bytes, readable.map(entry => entry.name));

  const manifest = [`# 压缩包内容（${entries.length} 个文件）`, ...entries.map(
    entry => `- ${entry.name}（${formatBytes(entry.size)}）`,
  )];
  const bodies = Array.from(contents, ([name, content]) => `# ${name}\n${tidyText(content)}`);
  const deferred = entries.length - readable.length;
  return {
    text: tidyText([...manifest, ...bodies].join("\n\n")),
    metrics: [{ label: "条目", value: `${entries.length} 个文件` }],
    notice: deferred > 0
      ? `已展开 ${readable.length} 个文本文件，其余 ${deferred} 个交由服务端解析`
      : undefined,
  };
}

async function runParser(
  file: File,
  descriptor: FormatDescriptor,
): Promise<{ text: string; metrics: ParseMetric[]; notice?: string; status?: ParseStatus }> {
  if (descriptor.route === "text" || descriptor.route === "notebook") {
    const text = decodeBytes(new Uint8Array(await file.arrayBuffer()));
    const extension = fileExtension(file.name);
    if (descriptor.route === "notebook") return extractNotebook(text);
    if (extension === "csv" || extension === "tsv") {
      return { text: tidyText(text), metrics: extractDelimited(text, extension) };
    }
    const tidied = tidyText(text);
    return { text: tidied, metrics: [{ label: "行", value: `${countLines(tidied)} 行` }] };
  }

  const bytes = new Uint8Array(await file.arrayBuffer());
  if (descriptor.route === "pdf") {
    const pdf = await extractPdfText(bytes);
    const metrics: ParseMetric[] = [{ label: "页", value: `${pdf.pages} 页` }];
    if (pdf.text.length < pdf.extractedPages * SCANNED_PDF_CHARS_PER_PAGE) {
      return {
        text: pdf.text,
        metrics,
        status: "server-pending",
        notice: "疑似扫描件，浏览器抽不到文字层，已排队交由服务端 OCR",
      };
    }
    return {
      text: pdf.text,
      metrics,
      status: pdf.extractedPages < pdf.pages ? "partial" : undefined,
      notice: pdf.extractedPages < pdf.pages
        ? `浏览器只抽取了前 ${pdf.extractedPages} 页，完整正文以服务端解析为准`
        : undefined,
    };
  }

  if (descriptor.route === "archive") return extractArchive(bytes);

  const extraction = descriptor.route === "ooxml-word" ? extractDocxText(bytes)
    : descriptor.route === "ooxml-slides" ? extractPptxText(bytes)
      : descriptor.route === "ooxml-sheet" ? extractXlsxText(bytes)
        : extractOpenDocumentText(bytes);
  const unit = descriptor.route === "ooxml-slides" ? "页幻灯片"
    : descriptor.route === "ooxml-sheet" ? "个工作表"
      : "个段落";
  return { text: extraction.text, metrics: [{ label: "结构", value: `${extraction.segments} ${unit}` }] };
}

export async function parseAttachment(file: File): Promise<ParseOutcome> {
  const descriptor = describeFormat(file.name, file.type);
  if (descriptor.route === "server") {
    return { status: "server-pending", text: "", characters: 0, metrics: [], notice: descriptor.serverReason };
  }
  if (file.size > MAX_CLIENT_PARSE_BYTES) {
    return {
      status: "server-pending",
      text: "",
      characters: 0,
      metrics: [],
      notice: `文件有 ${formatBytes(file.size)}，超过浏览器解析上限，已排队交由服务端解析`,
    };
  }

  try {
    const result = await runParser(file, descriptor);
    const truncated = result.text.length > MAX_EXTRACTED_CHARS;
    const text = truncated ? result.text.slice(0, MAX_EXTRACTED_CHARS) : result.text;
    if (result.status === "server-pending") {
      return { status: "server-pending", text, characters: text.length, metrics: result.metrics, notice: result.notice };
    }
    if (!text) {
      return {
        status: "empty",
        text: "",
        characters: 0,
        metrics: result.metrics,
        notice: result.notice ?? "没有抽取到文字内容，已排队交由服务端复核",
      };
    }
    return {
      status: truncated || result.status === "partial" ? "partial" : "ready",
      text,
      characters: text.length,
      metrics: result.metrics,
      notice: truncated
        ? `内容较长，浏览器只保留了前 ${MAX_EXTRACTED_CHARS.toLocaleString("zh-CN")} 字，完整正文以服务端解析为准`
        : result.notice,
    };
  } catch (error) {
    return {
      status: "failed",
      text: "",
      characters: 0,
      metrics: [],
      notice: error instanceof Error ? `解析失败：${error.message}` : "解析失败，已排队交由服务端重试",
    };
  }
}
