/**
 * 基于 ZIP 的文档格式抽取：docx / pptx / xlsx / OpenDocument / 普通压缩包。
 *
 * 这几种格式底层都是一个 zip 加一堆 XML，所以共用同一套解压与扫描，只依赖
 * fflate（约 10KB），不必为每种格式各引一个重量级 SDK。解压时用 fflate 的
 * `filter` 只解需要的条目——一个几十兆的 xlsx 里样式和主题占了绝大部分体积，
 * 抽文本一个字节都用不上。
 */

import { unzipSync } from "fflate";
import { MAX_ARCHIVE_TEXT_ENTRIES, MAX_SHEET_ROWS } from "./limits";
import { decodeBytes, tidyText } from "./text-decode";
import { collectText, parseAttributes, scanXml } from "./xml-scan";

/** 单元格横向截断阈值：宽表抽全列对阅读没有帮助，只会撑爆摘要。 */
const MAX_ROW_CELLS = 64;

function readEntries(bytes: Uint8Array, wanted: (name: string) => boolean): Map<string, string> {
  const files = unzipSync(bytes, { filter: file => wanted(file.name) });
  return new Map(Object.entries(files).map(([name, data]) => [name, decodeBytes(data)]));
}

/** 取文件名末尾的序号，避免 slide10 排到 slide2 前面。 */
function ordinalOf(name: string): number {
  return Number(name.match(/(\d+)\D*$/)?.[1] ?? 0);
}

export interface DocumentExtraction {
  text: string;
  /** 结构规模（段落数 / 幻灯片数 / 工作表数），用于附件卡片副标题 */
  segments: number;
}

/**
 * 逐段收集文本：`textLeaf` 是承载文字的叶子元素，`paragraph` 是断行边界。
 * docx 与 pptx 的差别只在标签名和几个换行标记上。
 *
 * `skip` 是属性容器（`w:pPr`、`w:rPr` 等）。它们内部同样会出现 `w:tab`——那是
 * "这一段的制表位设置"，不是正文里的制表符，照抽会在每段行首插进假的空白。
 */
function readParagraphs(
  source: string,
  paragraph: string,
  textLeaf: string,
  breaks: ReadonlySet<string> = new Set(),
  skip: ReadonlySet<string> = new Set(),
): string[] {
  const lines: string[] = [];
  let depth = 0;
  let skipped = 0;
  let line = "";
  scanXml(source, {
    onOpen(name, _attributes, selfClosing) {
      // 自闭合元素也加一层：scanXml 会给它补一次 onClose，这里配平即可，
      // 否则 <w:pPr><w:rPr/></w:pPr> 的内层会把外层的计数提前抵消掉。
      if (skip.has(name)) {
        skipped += 1;
        return;
      }
      if (skipped > 0) return;
      if (breaks.has(name)) line += name.endsWith("tab") ? "\t" : "\n";
      else if (name === textLeaf && !selfClosing) depth += 1;
    },
    onText(text) {
      if (depth > 0 && skipped === 0) line += text;
    },
    onClose(name) {
      if (skip.has(name)) skipped = Math.max(0, skipped - 1);
      else if (skipped > 0) return;
      else if (name === textLeaf && depth > 0) depth -= 1;
      else if (name === paragraph) {
        if (line.trim()) lines.push(line);
        line = "";
      }
    },
  });
  if (line.trim()) lines.push(line);
  return lines;
}

const DOCX_BREAKS = new Set(["w:tab", "w:br", "w:cr"]);
const DOCX_SKIP = new Set(["w:pPr", "w:rPr", "w:sectPr", "w:tblPr", "w:trPr", "w:tcPr", "w:tabs"]);
const PPTX_SKIP = new Set(["a:pPr", "a:rPr", "a:endParaRPr", "a:defRPr", "a:lstStyle"]);

/** Word：段落、表格单元格与制表/换行标记都还原成可读的行。 */
export function extractDocxText(bytes: Uint8Array): DocumentExtraction {
  const entries = readEntries(bytes, name =>
    name === "word/document.xml" || name === "word/footnotes.xml" || name === "word/endnotes.xml");
  const main = entries.get("word/document.xml");
  if (!main) throw new Error("docx 缺少正文部件 word/document.xml");

  const paragraphs = [main, entries.get("word/footnotes.xml"), entries.get("word/endnotes.xml")]
    .filter((source): source is string => Boolean(source))
    .flatMap(source => readParagraphs(source, "w:p", "w:t", DOCX_BREAKS, DOCX_SKIP));
  return { text: tidyText(paragraphs.join("\n")), segments: paragraphs.length };
}

/** PowerPoint：按幻灯片顺序抽取，备注归到对应页下面。 */
export function extractPptxText(bytes: Uint8Array): DocumentExtraction {
  const slidePattern = /^ppt\/slides\/slide\d+\.xml$/;
  const notesPattern = /^ppt\/notesSlides\/notesSlide\d+\.xml$/;
  const entries = readEntries(bytes, name => slidePattern.test(name) || notesPattern.test(name));

  const slides = Array.from(entries.keys()).filter(name => slidePattern.test(name))
    .sort((a, b) => ordinalOf(a) - ordinalOf(b));
  if (slides.length === 0) throw new Error("pptx 里没有找到任何幻灯片");

  const pages = slides.map((name, index) => {
    const body = readParagraphs(entries.get(name) ?? "", "a:p", "a:t", undefined, PPTX_SKIP);
    const notesSource = entries.get(`ppt/notesSlides/notesSlide${ordinalOf(name)}.xml`);
    const notes = notesSource ? readParagraphs(notesSource, "a:p", "a:t", undefined, PPTX_SKIP) : [];
    const block = [`# 第 ${index + 1} 页`, ...body];
    if (notes.length > 0) block.push(`备注：${notes.join(" ")}`);
    return block.join("\n");
  });
  return { text: tidyText(pages.join("\n\n")), segments: slides.length };
}

function columnIndex(reference: string): number {
  const letters = reference.match(/^[A-Z]+/)?.[0] ?? "";
  return Array.from(letters).reduce((total, letter) => total * 26 + (letter.charCodeAt(0) - 64), 0);
}

interface SheetExtraction {
  lines: string[];
  rows: number;
}

function readSheet(source: string, shared: readonly string[]): SheetExtraction {
  const lines: string[] = [];
  let rows = 0;
  let cells: string[] = [];
  let cellType = "";
  let cellColumn = 0;
  let inValue = false;
  let inInlineText = false;
  let value = "";

  const flushCell = (): void => {
    const text = cellType === "s" ? shared[Number(value)] ?? "" : value;
    while (cellColumn > 0 && cells.length < cellColumn - 1 && cells.length < MAX_ROW_CELLS) cells.push("");
    if (cells.length < MAX_ROW_CELLS) cells.push(text);
    cellType = "";
    cellColumn = 0;
    value = "";
  };

  scanXml(source, {
    onOpen(name, attributes, selfClosing) {
      if (name === "c") {
        const parsed = parseAttributes(attributes);
        cellType = parsed.get("t") ?? "";
        // 空单元格在 XML 里直接缺席，按列号补齐才能保住列的对应关系。
        cellColumn = columnIndex(parsed.get("r") ?? "");
        value = "";
        // 自闭合的 <c/> 由 scanXml 补发的 onClose 冲刷，这里不能再刷一次。
      } else if (name === "v" && !selfClosing) inValue = true;
      else if (name === "t" && cellType === "inlineStr" && !selfClosing) inInlineText = true;
    },
    onText(text) {
      if (inValue || inInlineText) value += text;
    },
    onClose(name) {
      if (name === "v") inValue = false;
      else if (name === "t") inInlineText = false;
      else if (name === "c") flushCell();
      else if (name === "row") {
        rows += 1;
        if (rows <= MAX_SHEET_ROWS) {
          const line = cells.join("\t");
          if (line.trim()) lines.push(line);
        }
        cells = [];
      }
    },
  });
  return { lines, rows };
}

/**
 * Excel：输出制表符分隔的文本。工作表顺序按 workbook 里的关系走，不能靠
 * `sheet1.xml` 这样的文件名猜——删过工作表的簿子里两者对不上。
 */
export function extractXlsxText(bytes: Uint8Array): DocumentExtraction {
  const entries = readEntries(bytes, name =>
    name === "xl/workbook.xml"
    || name === "xl/_rels/workbook.xml.rels"
    || name === "xl/sharedStrings.xml"
    || (name.startsWith("xl/worksheets/") && name.endsWith(".xml") && !name.includes("/_rels/")));

  const workbook = entries.get("xl/workbook.xml");
  if (!workbook) throw new Error("xlsx 缺少工作簿部件 xl/workbook.xml");

  const relationships = new Map<string, string>();
  const rels = entries.get("xl/_rels/workbook.xml.rels");
  if (rels) {
    scanXml(rels, {
      onOpen(name, attributes) {
        if (name !== "Relationship") return;
        const parsed = parseAttributes(attributes);
        const id = parsed.get("Id");
        const target = parsed.get("Target");
        if (id && target) relationships.set(id, `xl/${target.replace(/^\/?(xl\/)?/, "")}`);
      },
    });
  }

  const sharedSource = entries.get("xl/sharedStrings.xml");
  const shared = sharedSource ? collectText(sharedSource, "si", "t") : [];

  const sheets: Array<{ name: string; path: string }> = [];
  scanXml(workbook, {
    onOpen(name, attributes) {
      if (name !== "sheet") return;
      const parsed = parseAttributes(attributes);
      const relationId = parsed.get("r:id") ?? "";
      sheets.push({
        name: parsed.get("name") || `Sheet${sheets.length + 1}`,
        path: relationships.get(relationId) ?? `xl/worksheets/sheet${sheets.length + 1}.xml`,
      });
    },
  });

  const blocks: string[] = [];
  let truncated = false;
  for (const sheet of sheets) {
    const source = entries.get(sheet.path);
    if (!source) continue;
    const { lines, rows } = readSheet(source, shared);
    if (rows > MAX_SHEET_ROWS) truncated = true;
    blocks.push([`# 工作表：${sheet.name}（${rows} 行）`, ...lines].join("\n"));
  }

  if (blocks.length === 0) throw new Error("xlsx 里没有找到任何工作表数据");
  const suffix = truncated ? `\n\n（每个工作表最多抽取前 ${MAX_SHEET_ROWS} 行）` : "";
  return { text: tidyText(blocks.join("\n\n") + suffix), segments: blocks.length };
}

/** OpenDocument：正文都在 content.xml 里；表格行内的段落合并成一行以保住列关系。 */
export function extractOpenDocumentText(bytes: Uint8Array): DocumentExtraction {
  const content = readEntries(bytes, name => name === "content.xml").get("content.xml");
  if (!content) throw new Error("OpenDocument 缺少 content.xml");

  const lines: string[] = [];
  let rowCells: string[] | null = null;
  let paragraphDepth = 0;
  let buffer = "";

  const flushParagraph = (): void => {
    if (buffer.trim()) (rowCells ?? lines).push(buffer);
    buffer = "";
  };

  scanXml(content, {
    onOpen(name, _attributes, selfClosing) {
      if (name === "table:table-row" && !selfClosing) rowCells = [];
      else if (name === "text:tab") buffer += "\t";
      else if (name === "text:line-break") buffer += "\n";
      else if ((name === "text:p" || name === "text:h") && !selfClosing) paragraphDepth += 1;
    },
    onText(text) {
      if (paragraphDepth > 0) buffer += text;
    },
    onClose(name) {
      if (name === "text:p" || name === "text:h") {
        if (paragraphDepth > 0) paragraphDepth -= 1;
        if (paragraphDepth === 0) flushParagraph();
      } else if (name === "table:table-row" && rowCells) {
        if (rowCells.some(cell => cell.trim())) lines.push(rowCells.join("\t"));
        rowCells = null;
      }
    },
  });
  return { text: tidyText(lines.join("\n")), segments: lines.length };
}

export interface ArchiveEntry {
  name: string;
  size: number;
}

/** 只读中央目录列出条目：filter 返回 false 就不会真的解压，代价接近于零。 */
export function listArchiveEntries(bytes: Uint8Array): ArchiveEntry[] {
  const entries: ArchiveEntry[] = [];
  unzipSync(bytes, {
    filter: file => {
      if (!file.name.endsWith("/")) entries.push({ name: file.name, size: file.originalSize });
      return false;
    },
  });
  return entries;
}

/** 展开压缩包里挑出来的文本条目，用于赛题常见的“附件打包成 zip”。 */
export function extractArchiveEntries(bytes: Uint8Array, names: readonly string[]): Map<string, string> {
  const wanted = new Set(names.slice(0, MAX_ARCHIVE_TEXT_ENTRIES));
  if (wanted.size === 0) return new Map();
  return readEntries(bytes, name => wanted.has(name));
}
