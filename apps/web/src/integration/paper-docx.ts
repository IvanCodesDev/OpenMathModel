/**
 * PaperDocument → 真正的 Word 文档（.docx = OOXML 部件 + zip），在浏览器里生成、不经服务端。
 *
 * 旧版把 HTML 打成 MHTML 冒充 .doc：Word 不加载外链样式、看不懂 KaTeX 的排版标记，公式散成
 * 乱码，标题与表格没有样式。这里直接写 OOXML：
 * - 版式：A4、页边距 2.5 cm、正文宋体 / Times New Roman 小四、1.5 倍行距、首行缩进 2 字符、
 *   两端对齐（含长串的段落左对齐，见 raggedRight）；标题黑体并带大纲级别（Word 导航窗格可用）；
 *   页脚居中页码。
 * - 西文断行：约 11 个字符以上的长串在 `_ = / ; \` 等分隔符后插 1 pt 的空格作断点（见 breakable），
 *   单词、数字内部不断；表格单元格不插（见 textRun 的 plain）。不用零宽空格——WPS 不在 U+200B 处断行，还按正文字号给它排宽度，`data_source`
 *   会显示成 `da ta_ so u r ce`；也不开「允许西文在单词中间换行」（w:wordWrap=0）——单词、数字会在
 *   行尾任意字母处被劈开（`S|OC`、`240|6`）。
 * - 公式：KaTeX → MathML → OMML（text/mathml-omml），是 Word 原生公式、可继续编辑；转换失败的
 *   公式退回等宽源码，不丢内容。
 * - 图：word/media/ 内嵌，按版心等比缩放；表：三线表，表头跨页重复，列宽见 tableColumnWidths。
 * 元素顺序严格按 WordprocessingML schema（rPr / pPr / tblPr / settings 的子元素顺序写错 Word
 * 会报「文件已损坏」，LibreOffice 却能打开——别凭 LibreOffice 能开就认为没问题）。
 */

import { strToU8, zipSync } from "fflate";
import { mathmlToOmml } from "../text/mathml-omml";
import {
  type Block,
  type Inline,
  type PaperDocument,
  type TableCell,
  type TextMarks,
  SOFT_BREAK,
  displayWidth,
  stripControlChars,
  tableLayout,
  unbreakableWidth,
} from "./paper-document";
import { type EmbeddedImage, imagePixelSize } from "./paper-export-images";

export const DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export interface DocxOptions {
  /** tex → KaTeX 的 MathML 输出（renderToString(tex, { output: "mathml" })）；抛错 / 空串 → 公式退回源码。 */
  texToMathml: (tex: string, display: boolean) => string;
  /** core.xml 的创建时间；测试注入固定值。 */
  now?: Date;
}

const NS = {
  w: "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
  r: "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
  m: "http://schemas.openxmlformats.org/officeDocument/2006/math",
  wp: "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
  a: "http://schemas.openxmlformats.org/drawingml/2006/main",
  pic: "http://schemas.openxmlformats.org/drawingml/2006/picture",
  rel: "http://schemas.openxmlformats.org/package/2006/relationships",
  docRel: "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
};

// A4（twip = 1/20 pt）与版心
const PAGE_WIDTH = 11906;
const PAGE_HEIGHT = 16838;
const MARGIN = 1418;
const TEXT_WIDTH = PAGE_WIDTH - 2 * MARGIN;
const EMU_PER_TWIP = 635;
const EMU_PER_PX = 9525;
const MAX_IMAGE_WIDTH = Math.round(TEXT_WIDTH * EMU_PER_TWIP * 0.82);
const MAX_IMAGE_HEIGHT = 3_600_000; // 10 cm

const WORD_IMAGE_TYPES: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpeg",
  "image/jpg": "jpeg",
  "image/gif": "gif",
  "image/bmp": "bmp",
};

const XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';

/** 五号字一个汉字约 210 twip；每格再加左右内边距（各 100）与估宽余量。 */
const CHAR_TWIPS = 210;
const CELL_EXTRA = 360;
/** 每列先保证的宽度（字）：再窄的列也有 4 个字，汉字才不会一字一行竖着排。 */
const MIN_COLUMN = 4;
/** 正文不超过 8 个字、表头不超过 6 个字的列尽量不折行（数字、单位、「状态与影响」这类表头）。 */
const SHORT_BODY = 8;
const SHORT_HEAD = 6;
/** 不可断片段参与分宽时的封顶（字）：更长的标识符由 Word 在格边硬断，不能为它把别的列挤没。 */
const LONGEST_CAP = 12;

/**
 * 表格列宽（twip）与哪些列会折行。版心按三档逐档分：① 每列 4 个字（自然宽不到 4 个字的按自然宽）；
 * ② 不折坏的宽度——最长的不可断片段（封顶 12 字）、短正文、短表头；③ 自然宽度。前一档分满了才分
 * 下一档，分不满的那一档按各列差额的比例分（与 LaTeX 导出的 latexTableLayout 同一思路）。
 * 自然宽度排得下就都用自然宽度。`wrap` 标出正文是长文本（超过 8 个字）且放不下一行的列，这些列
 * 左对齐折行；数字、短词列即使表头折成两行也照旧居中。
 */
export function tableColumnWidths(
  rows: readonly TableCell[][],
  columns: number,
  headCount: number,
): { widths: number[]; wrap: boolean[] } {
  const sum = (values: readonly number[]) => values.reduce((total, value) => total + value, 0);
  const widest = (column: number, from: number, to: number, measure: (inlines: readonly Inline[]) => number) =>
    Math.max(0, ...rows.slice(from, to).map(row => (row[column] ? measure(row[column].inlines) : 0)));
  const twips = (chars: number) => Math.round(chars * CHAR_TWIPS + CELL_EXTRA);
  const natural = Array.from({ length: columns }, (_, column) => Math.max(1, widest(column, 0, rows.length, displayWidth)));
  const body = natural.map((_, column) => widest(column, headCount, rows.length, displayWidth));
  const intact = natural.map((width, column) => {
    const head = widest(column, 0, headCount, displayWidth);
    return Math.min(width, Math.max(
      MIN_COLUMN,
      Math.min(LONGEST_CAP, widest(column, 0, rows.length, unbreakableWidth)),
      body[column] <= SHORT_BODY ? body[column] : 0,
      head <= SHORT_HEAD ? head : 0,
    ));
  });
  const [minimum, ...levels] = [natural.map(width => Math.min(width, MIN_COLUMN)), intact, natural]
    .map(level => level.map(twips));
  let widths = minimum;
  if (sum(minimum) > TEXT_WIDTH) {
    widths = minimum.map(width => Math.floor((width * TEXT_WIDTH) / sum(minimum)));
  } else {
    for (const level of levels) {
      const room = TEXT_WIDTH - sum(widths);
      const need = sum(level) - sum(widths);
      if (need <= room) {
        widths = level;
        continue;
      }
      widths = widths.map((width, column) => width + Math.floor((room * (level[column] - width)) / need));
      break;
    }
  }
  return { widths, wrap: widths.map((width, column) => body[column] > SHORT_BODY && width < twips(body[column])) };
}

/** 不可断片段宽到这个数（displayWidth 口径，约 11 个西文字符）的段落改左对齐。 */
const RAGGED_RUN = 6;

/**
 * 含长串的段落左对齐：断开后剩下的长数字（`0.0021355180250620664`）仍整段挪行，两端对齐会把上一行
 * 摊开；1 pt 的断点空格在两端对齐时也会被拉宽，`rolling_ domination` 中间能裂开 6 pt。左对齐的代价是
 * 右边缘不齐，有了断点，行尾空出的最多是一小段的宽度。行内公式不计：displayWidth 按源码长度折算
 * 公式，带上下标的短公式也会超过阈值。
 */
function raggedRight(inlines: readonly Inline[]): string {
  const text = inlines.map(inline => (inline.kind === "math" ? { kind: "break" as const } : inline));
  return unbreakableWidth(text) >= RAGGED_RUN ? '<w:jc w:val="left"/>' : "";
}

/**
 * 长串的断点是一个 1 pt 的普通空格（约 0.25 pt 宽，看不出来）：WPS 不在 U+200B 处断行，按正文字号还会
 * 把它排出宽度；普通空格在 Word / WPS 里都是断点。代价是从 Word 里复制出的标识符在断点处多一个空格。
 */
const BREAK_RUN = '<w:r><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>';
/**
 * `_ = / ; \`、后面不是数字的 `, :`、字母前的 `.` 之后可断（千分位 `5,087,342` 与 `12:30` 不拆）；
 * 断点前后各至少留 2 个字符（不把 `F_`、`=1` 单独甩在行尾、行首）。
 */
const BREAK_AFTER = /(?<=[A-Za-z0-9]{2})([_=/;\\]|[,:](?![0-9])|\.(?=[A-Za-z]))(?=..)/g;
/**
 * 紧跟数字的负号（含指数 `e-11`）换成不断行连字符 U+2011，字形与 `-` 相同：WPS 会在 `-` 后断行
 * （`pct=-` | `1.431863`），`<w:noBreakHyphen/>` 在 WPS 里后面又多出约 1.7 pt（`- 1.431863`）。
 * 前面是数字的 `-`（`2376-2399`、日期）照旧可断。
 */
const MINUS = /(?<![0-9])-(?=[0-9])/g;

/** 约 11 个字符以上的拉丁串按 BREAK_AFTER 切成可断的几段；字母、数字内部不断。 */
function breakable(text: string): string[] {
  return text.replace(/[A-Za-z0-9_.=:/\\,;+-]{11,}/g, token => token.replace(BREAK_AFTER, `$1${SOFT_BREAK}`)).split(SOFT_BREAK);
}

/** XML 文本：去掉 XML 1.0 不允许的控制字符与孤立代理项，再转义。 */
export function xmlText(text: string): string {
  return stripControlChars(text)
    .replace(/[&<>"]/g, char => (char === "&" ? "&amp;" : char === "<" ? "&lt;" : char === ">" ? "&gt;" : "&quot;"));
}

class DocxPackage {
  private relationships: string[] = [];
  private nextRelationship = 10;
  private nextDrawing = 1;
  readonly media: Record<string, Uint8Array> = {};
  readonly extensions = new Set<string>();
  private imageRelationships = new Map<number, { id: string; size: { cx: number; cy: number } } | null>();

  constructor(private readonly doc: PaperDocument, private readonly options: DocxOptions) {}

  relationshipsXml(): string {
    const fixed = [
      `<Relationship Id="rId1" Type="${NS.docRel}/styles" Target="styles.xml"/>`,
      `<Relationship Id="rId2" Type="${NS.docRel}/settings" Target="settings.xml"/>`,
      `<Relationship Id="rId3" Type="${NS.docRel}/footer" Target="footer1.xml"/>`,
    ];
    return `${XML_HEADER}<Relationships xmlns="${NS.rel}">${[...fixed, ...this.relationships].join("")}</Relationships>`;
  }

  private hyperlink(href: string): string {
    const id = `rId${this.nextRelationship++}`;
    this.relationships.push(`<Relationship Id="${id}" Type="${NS.docRel}/hyperlink" Target="${xmlText(href)}" TargetMode="External"/>`);
    return id;
  }

  private image(index: number): { id: string; size: { cx: number; cy: number } } | null {
    if (this.imageRelationships.has(index)) return this.imageRelationships.get(index)!;
    const image: EmbeddedImage | undefined = this.doc.images[index];
    const extension = image ? WORD_IMAGE_TYPES[image.mediaType.toLowerCase()] : undefined;
    const pixels = image && extension ? imagePixelSize(image) : null;
    if (!image || !extension || !pixels || !pixels.width || !pixels.height) {
      this.imageRelationships.set(index, null);
      return null;
    }
    const id = `rId${this.nextRelationship++}`;
    const target = `media/image${index + 1}.${extension}`;
    this.media[`word/${target}`] = image.bytes;
    this.extensions.add(extension);
    this.relationships.push(`<Relationship Id="${id}" Type="${NS.docRel}/image" Target="${target}"/>`);
    let cx = pixels.width * EMU_PER_PX;
    let cy = pixels.height * EMU_PER_PX;
    const scale = Math.min(1, MAX_IMAGE_WIDTH / cx, MAX_IMAGE_HEIGHT / cy);
    cx = Math.round(cx * scale);
    cy = Math.round(cy * scale);
    const entry = { id, size: { cx, cy } };
    this.imageRelationships.set(index, entry);
    return entry;
  }

  // ── 行内 ──

  private omml(tex: string, display: boolean): string | null {
    try {
      const mathml = this.options.texToMathml(tex, display);
      if (!mathml || mathml.includes("katex-error")) return null;
      return mathmlToOmml(mathml);
    } catch {
      return null;
    }
  }

  /** `plain`（表格单元格）：不插断点、负号照旧，表里的标识符与数值复制出去能原样用（粘进 Excel 仍是数字）。 */
  private textRun(text: string, marks: TextMarks, plain = false): string {
    const props: string[] = [];
    if (marks.code) props.push('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>');
    if (marks.bold) props.push("<w:b/><w:bCs/>");
    if (marks.italic) props.push("<w:i/><w:iCs/>");
    if (marks.strike) props.push("<w:strike/>");
    if (marks.href) props.push('<w:color w:val="0563C1"/>');
    if (marks.code) props.push('<w:sz w:val="21"/><w:szCs w:val="21"/>');
    if (marks.underline || marks.href) props.push('<w:u w:val="single"/>');
    if (marks.sup) props.push('<w:vertAlign w:val="superscript"/>');
    else if (marks.sub) props.push('<w:vertAlign w:val="subscript"/>');
    const rPr = props.length ? `<w:rPr>${props.join("")}</w:rPr>` : "";
    const source = text.replaceAll(SOFT_BREAK, "");
    const pieces = plain ? [source] : breakable(source).map(piece => piece.replace(MINUS, "\u2011"));
    return pieces.map(piece => `<w:r>${rPr}<w:t xml:space="preserve">${xmlText(piece)}</w:t></w:r>`).join(BREAK_RUN);
  }

  inlines(inlines: readonly Inline[], plain = false): string {
    let out = "";
    for (const inline of inlines) {
      if (inline.kind === "break") {
        out += "<w:r><w:br/></w:r>";
      } else if (inline.kind === "math") {
        out += this.omml(inline.tex, false) ?? this.textRun(inline.tex, { code: true }, plain);
      } else if (inline.marks.href) {
        out += `<w:hyperlink r:id="${this.hyperlink(inline.marks.href)}" w:history="1">${this.textRun(inline.text, inline.marks, plain)}</w:hyperlink>`;
      } else {
        out += this.textRun(inline.text, inline.marks, plain);
      }
    }
    return out;
  }

  private drawing(index: number, alt: string): string | null {
    const entry = this.image(index);
    if (!entry) return null;
    const id = this.nextDrawing++;
    const { cx, cy } = entry.size;
    return `<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="${cx}" cy="${cy}"/>`
      + '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
      + `<wp:docPr id="${id}" name="图片 ${id}" descr="${xmlText(alt)}"/>`
      + '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
      + `<a:graphic><a:graphicData uri="${NS.pic}"><pic:pic>`
      + `<pic:nvPicPr><pic:cNvPr id="${id}" name="image${index + 1}"/><pic:cNvPicPr/></pic:nvPicPr>`
      + `<pic:blipFill><a:blip r:embed="${entry.id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>`
      + `<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>`
      + "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>";
  }

  // ── 块 ──

  private paragraph(content: string, style: string, extra = ""): string {
    return `<w:p><w:pPr><w:pStyle w:val="${style}"/>${extra}</w:pPr>${content}</w:p>`;
  }

  private table(block: Extract<Block, { kind: "table" }>): string {
    const layout = tableLayout(block.rows);
    const { widths, wrap } = tableColumnWidths(block.rows, layout.columns, layout.headCount);
    const tableWidth = widths.reduce((sum, width) => sum + width, 0);
    const border = (side: string, size: number) => `<w:${side} w:val="single" w:sz="${size}" w:space="0" w:color="000000"/>`;
    const rows = block.rows.map((cells, rowIndex) => {
      const header = rowIndex < layout.headCount;
      const lastHeader = rowIndex === layout.headCount - 1;
      const trPr = `<w:trPr><w:cantSplit/>${header ? "<w:tblHeader/>" : ""}</w:trPr>`;
      const tcs = Array.from({ length: layout.columns }, (_, column) => {
        const cell = cells[column];
        const borders = lastHeader ? `<w:tcBorders>${border("bottom", 6)}</w:tcBorders>` : "";
        const tcPr = `<w:tcPr><w:tcW w:w="${widths[column]}" w:type="dxa"/>${borders}<w:vAlign w:val="center"/></w:tcPr>`;
        const align = wrap[column] && !header ? "left" : "center";
        const runs = cell ? this.inlines(header ? cell.inlines.map(inline => (inline.kind === "text" ? { ...inline, marks: { ...inline.marks, bold: true } } : inline)) : cell.inlines, true) : "";
        return `<w:tc>${tcPr}<w:p><w:pPr><w:pStyle w:val="TableText"/><w:jc w:val="${align}"/></w:pPr>${runs}</w:p></w:tc>`;
      }).join("");
      return `<w:tr>${trPr}${tcs}</w:tr>`;
    }).join("");
    const tblPr = `<w:tblPr><w:tblW w:w="${tableWidth}" w:type="dxa"/><w:jc w:val="center"/>`
      + `<w:tblBorders>${border("top", 12)}<w:left w:val="nil"/>${border("bottom", 12)}<w:right w:val="nil"/><w:insideH w:val="nil"/><w:insideV w:val="nil"/></w:tblBorders>`
      + '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="40" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>'
      + '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="1" w:noVBand="1"/></w:tblPr>';
    const grid = `<w:tblGrid>${widths.map(width => `<w:gridCol w:w="${width}"/>`).join("")}</w:tblGrid>`;
    const caption = block.caption?.length ? this.paragraph(this.inlines(block.caption), "TableCaption") : "";
    // 表后补一个空的小段落：Word 里两张表或表与下一段贴得太紧时会粘连
    return `${caption}<w:tbl>${tblPr}${grid}${rows}</w:tbl>${this.paragraph("", "TableAfter")}`;
  }

  block(block: Block): string {
    switch (block.kind) {
      case "heading": {
        const style = block.role === "abstract" ? "AbstractHeading" : `Heading${block.level}`;
        return this.paragraph(this.inlines(block.inlines), style);
      }
      case "paragraph": {
        const style = block.role === "note" ? "Note"
          : block.role === "keywords" || block.continued ? "BodyNoIndent" : "Normal";
        const align = block.align ? `<w:ind w:firstLine="0" w:firstLineChars="0"/><w:jc w:val="${block.align}"/>` : raggedRight(block.inlines);
        return this.paragraph(this.inlines(block.inlines), style, align);
      }
      case "math": {
        const omml = this.omml(block.tex, true);
        if (!omml) return this.paragraph(this.textRun(block.tex, { code: true }), "Equation");
        return `<w:p><w:pPr><w:pStyle w:val="Equation"/></w:pPr><m:oMathPara><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>${omml}</m:oMathPara></w:p>`;
      }
      case "list":
        return block.items.map((item, index) => {
          const marker = block.ordered ? `${index + 1}.` : "•";
          return this.paragraph(`<w:r><w:t xml:space="preserve">${marker}</w:t></w:r><w:r><w:tab/></w:r>${this.inlines(item)}`, "ListItem", raggedRight(item));
        }).join("");
      case "quote":
        return this.paragraph(this.inlines(block.inlines), "Quote", raggedRight(block.inlines));
      case "code":
        return block.text.split("\n").map(line => {
          const parts = line.split("\t").map(part => `<w:t xml:space="preserve">${xmlText(part)}</w:t>`);
          return this.paragraph(line ? `<w:r>${parts.join("<w:tab/>")}</w:r>` : "", "Code");
        }).join("");
      case "table":
        return this.table(block);
      case "figure": {
        const drawing = block.image === null ? null : this.drawing(block.image, block.alt || block.caption.map(inline => (inline.kind === "text" ? inline.text : "")).join(""));
        const picture = drawing
          ? this.paragraph(drawing, "Figure")
          : this.paragraph(this.textRun(`［插图：${block.alt || "未命名"}（图片未能随导出带走）］`, { italic: true }), "Figure");
        const caption = block.caption.length ? this.paragraph(this.inlines(block.caption), "Caption") : "";
        return picture + caption;
      }
      case "rule":
        return '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/></w:pBdr><w:spacing w:after="120"/></w:pPr></w:p>';
    }
  }

  documentXml(): string {
    const body = [
      this.paragraph(this.textRun(this.doc.title, {}), "Title"),
      ...this.doc.blocks.map(block => this.block(block)),
    ].join("");
    const sectPr = '<w:sectPr><w:footerReference w:type="default" r:id="rId3"/>'
      + `<w:pgSz w:w="${PAGE_WIDTH}" w:h="${PAGE_HEIGHT}"/>`
      + `<w:pgMar w:top="${MARGIN}" w:right="${MARGIN}" w:bottom="${MARGIN}" w:left="${MARGIN}" w:header="851" w:footer="851" w:gutter="0"/>`
      + '<w:cols w:space="425"/></w:sectPr>';
    return `${XML_HEADER}<w:document xmlns:w="${NS.w}" xmlns:r="${NS.r}" xmlns:m="${NS.m}" xmlns:wp="${NS.wp}" xmlns:a="${NS.a}" xmlns:pic="${NS.pic}"><w:body>${body}${sectPr}</w:body></w:document>`;
  }
}

const BODY_FONTS = '<w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>';
const HEADING_FONTS = '<w:rFonts w:ascii="Times New Roman" w:eastAsia="黑体" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>';

function paragraphStyle(id: string, name: string, pPr: string, rPr = "", options: { basedOn?: string; next?: string; outline?: number } = {}): string {
  const based = options.basedOn ? `<w:basedOn w:val="${options.basedOn}"/>` : "";
  const next = options.next ? `<w:next w:val="${options.next}"/>` : "";
  const outline = options.outline === undefined ? "" : `<w:outlineLvl w:val="${options.outline}"/>`;
  return `<w:style w:type="paragraph" w:styleId="${id}"><w:name w:val="${name}"/>${based}${next}<w:qFormat/>`
    + `<w:pPr>${pPr}${outline}</w:pPr>${rPr ? `<w:rPr>${rPr}</w:rPr>` : ""}</w:style>`;
}

const NO_INDENT = '<w:ind w:firstLine="0" w:firstLineChars="0"/>';
const SINGLE = '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>';

function stylesXml(): string {
  const heading = (level: number, size: number, before: number, after: number) => paragraphStyle(
    `Heading${level}`, `heading ${level}`,
    `<w:keepNext/><w:keepLines/><w:spacing w:before="${before}" w:after="${after}" w:line="360" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="left"/>`,
    `${HEADING_FONTS}<w:b/><w:bCs/><w:sz w:val="${size}"/><w:szCs w:val="${size}"/>`,
    { basedOn: "Normal", next: "Normal", outline: level - 1 },
  );
  const styles = [
    `<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>`
      + '<w:pPr><w:widowControl/><w:snapToGrid w:val="0"/><w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="auto"/>'
      + '<w:ind w:firstLine="480" w:firstLineChars="200"/><w:jc w:val="both"/></w:pPr>'
      + `<w:rPr>${BODY_FONTS}<w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>`,
    paragraphStyle("BodyNoIndent", "Body No Indent", NO_INDENT, "", { basedOn: "Normal" }),
    paragraphStyle("Title", "Title", `<w:keepNext/><w:spacing w:before="0" w:after="240" w:line="360" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`,
      `${HEADING_FONTS}<w:b/><w:bCs/><w:sz w:val="32"/><w:szCs w:val="32"/>`, { basedOn: "Normal", next: "Normal" }),
    paragraphStyle("AbstractHeading", "Abstract Heading", `<w:keepNext/><w:spacing w:before="120" w:after="120" w:line="360" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`,
      `${HEADING_FONTS}<w:b/><w:bCs/><w:sz w:val="28"/><w:szCs w:val="28"/>`, { basedOn: "Normal", next: "Normal" }),
    heading(1, 28, 240, 120),
    heading(2, 24, 180, 60),
    heading(3, 24, 120, 60),
    heading(4, 24, 120, 60),
    paragraphStyle("Equation", "Equation", `<w:keepLines/><w:spacing w:before="60" w:after="60" w:line="240" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`, "", { basedOn: "Normal" }),
    paragraphStyle("Figure", "Figure", `<w:keepNext/><w:spacing w:before="120" w:after="60" w:line="240" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`, "", { basedOn: "Normal" }),
    paragraphStyle("Caption", "caption", `<w:spacing w:before="0" w:after="160" w:line="300" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`,
      '<w:sz w:val="21"/><w:szCs w:val="21"/>', { basedOn: "Normal", next: "Normal" }),
    paragraphStyle("TableCaption", "Table Caption", `<w:keepNext/><w:spacing w:before="120" w:after="60" w:line="300" w:lineRule="auto"/>${NO_INDENT}<w:jc w:val="center"/>`,
      '<w:b/><w:bCs/><w:sz w:val="21"/><w:szCs w:val="21"/>', { basedOn: "Normal", next: "Normal" }),
    paragraphStyle("TableText", "Table Text", `${SINGLE}${NO_INDENT}<w:jc w:val="center"/>`, '<w:sz w:val="21"/><w:szCs w:val="21"/>', { basedOn: "Normal" }),
    paragraphStyle("TableAfter", "Table After", `<w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="auto"/>${NO_INDENT}`, '<w:sz w:val="12"/><w:szCs w:val="12"/>', { basedOn: "Normal" }),
    paragraphStyle("ListItem", "List Item", '<w:tabs><w:tab w:val="left" w:pos="840"/></w:tabs><w:ind w:left="840" w:hanging="420"/>', "", { basedOn: "Normal" }),
    paragraphStyle("Quote", "Quote", '<w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="999999"/></w:pBdr><w:ind w:left="480" w:firstLine="0" w:firstLineChars="0"/>',
      '<w:color w:val="404040"/>', { basedOn: "Normal" }),
    paragraphStyle("Code", "Code", `<w:shd w:val="clear" w:color="auto" w:fill="F5F5F5"/>${SINGLE}${NO_INDENT}<w:jc w:val="left"/>`,
      '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="18"/><w:szCs w:val="18"/>', { basedOn: "Normal" }),
    paragraphStyle("Note", "Note", `${NO_INDENT}`, '<w:i/><w:iCs/><w:color w:val="555555"/><w:sz w:val="21"/><w:szCs w:val="21"/>', { basedOn: "Normal" }),
    paragraphStyle("Footer", "footer", `${SINGLE}${NO_INDENT}<w:jc w:val="center"/>`, '<w:sz w:val="18"/><w:szCs w:val="18"/>', { basedOn: "Normal" }),
  ];
  return `${XML_HEADER}<w:styles xmlns:w="${NS.w}">`
    + `<w:docDefaults><w:rPrDefault><w:rPr>${BODY_FONTS}<w:kern w:val="2"/><w:sz w:val="24"/><w:szCs w:val="24"/>`
    + '<w:lang w:val="en-US" w:eastAsia="zh-CN" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
    + '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    + `${styles.join("")}</w:styles>`;
}

function settingsXml(): string {
  return `${XML_HEADER}<w:settings xmlns:w="${NS.w}" xmlns:m="${NS.m}">`
    + '<w:zoom w:percent="100"/><w:defaultTabStop w:val="420"/>'
    + '<w:characterSpacingControl w:val="compressPunctuation"/>'
    + '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
    + '<m:mathPr><m:mathFont m:val="Cambria Math"/><m:brkBin m:val="before"/><m:brkBinSub m:val="--"/><m:smallFrac m:val="0"/>'
    + '<m:dispDef/><m:lMargin m:val="0"/><m:rMargin m:val="0"/><m:defJc m:val="centerGroup"/><m:wrapIndent m:val="1440"/>'
    + '<m:intLim m:val="subSup"/><m:naryLim m:val="undOvr"/></m:mathPr>'
    + '<w:themeFontLang w:val="en-US" w:eastAsia="zh-CN"/><w:decimalSymbol w:val="."/><w:listSeparator w:val=","/>'
    + "</w:settings>";
}

function footerXml(): string {
  return `${XML_HEADER}<w:ftr xmlns:w="${NS.w}" xmlns:r="${NS.r}"><w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr>`
    + '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
    + '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
    + "</w:p></w:ftr>";
}

function contentTypesXml(extensions: Set<string>): string {
  const media: Record<string, string> = { png: "image/png", jpeg: "image/jpeg", gif: "image/gif", bmp: "image/bmp" };
  const defaults = [...extensions].map(extension => `<Default Extension="${extension}" ContentType="${media[extension]}"/>`).join("");
  return `${XML_HEADER}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">`
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + `<Default Extension="xml" ContentType="application/xml"/>${defaults}`
    + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    + '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    + '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
    + '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
    + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    + '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    + "</Types>";
}

function rootRelationshipsXml(): string {
  return `${XML_HEADER}<Relationships xmlns="${NS.rel}">`
    + `<Relationship Id="rId1" Type="${NS.docRel}/officeDocument" Target="word/document.xml"/>`
    + '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    + `<Relationship Id="rId3" Type="${NS.docRel}/extended-properties" Target="docProps/app.xml"/>`
    + "</Relationships>";
}

function corePropertiesXml(title: string, now: Date): string {
  const stamp = now.toISOString().replace(/\.\d{3}Z$/, "Z");
  return `${XML_HEADER}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">`
    + `<dc:title>${xmlText(title)}</dc:title><dc:creator>OpenMathModel</dc:creator>`
    + `<dcterms:created xsi:type="dcterms:W3CDTF">${stamp}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">${stamp}</dcterms:modified>`
    + "</cp:coreProperties>";
}

function appPropertiesXml(): string {
  return `${XML_HEADER}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>OpenMathModel</Application></Properties>`;
}

/** 整篇 → .docx 字节。 */
export function buildDocx(doc: PaperDocument, options: DocxOptions): Uint8Array {
  const pkg = new DocxPackage(doc, options);
  const documentXml = pkg.documentXml();
  const files: Record<string, Uint8Array> = {
    "[Content_Types].xml": strToU8(contentTypesXml(pkg.extensions)),
    "_rels/.rels": strToU8(rootRelationshipsXml()),
    "docProps/core.xml": strToU8(corePropertiesXml(doc.title, options.now ?? new Date())),
    "docProps/app.xml": strToU8(appPropertiesXml()),
    "word/document.xml": strToU8(documentXml),
    "word/_rels/document.xml.rels": strToU8(pkg.relationshipsXml()),
    "word/styles.xml": strToU8(stylesXml()),
    "word/settings.xml": strToU8(settingsXml()),
    "word/footer1.xml": strToU8(footerXml()),
    ...pkg.media,
  };
  return zipSync(files, { level: 6 });
}
