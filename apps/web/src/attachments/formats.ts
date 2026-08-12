/**
 * 附件格式登记表。
 *
 * 每个扩展名映射到一条解析路径：能在浏览器里解开的走对应 parser，解不动的
 * （旧版二进制 Office、需要 OCR 的图片）标成 `server`，等二进制上传后由服务端
 * 出权威结果。判定以扩展名为准、MIME 只做兜底——竞赛附件经常从压缩包里解出来，
 * 浏览器给的 MIME 时常是空串或 `application/octet-stream`。
 */

export type ParseRoute =
  | "text"
  | "notebook"
  | "pdf"
  | "ooxml-word"
  | "ooxml-slides"
  | "ooxml-sheet"
  | "opendocument"
  | "archive"
  | "server";

export interface FormatDescriptor {
  /** 卡片副标题里显示的格式名 */
  readonly label: string;
  /** Phosphor 图标名，不带 `ph-` 前缀 */
  readonly icon: string;
  readonly route: ParseRoute;
  /** 上传时写给后端的产物类别 */
  readonly artifactKind: "dataset" | "paper" | "report" | "code" | "figure" | "other";
  /** route 为 server 时展示给用户的原因 */
  readonly serverReason?: string;
}

const TEXT: Omit<FormatDescriptor, "label"> = {
  icon: "file-text",
  route: "text",
  artifactKind: "other",
};

const CODE: Omit<FormatDescriptor, "label"> = {
  icon: "file-code",
  route: "text",
  artifactKind: "code",
};

const TABLE_TEXT: Omit<FormatDescriptor, "label"> = {
  icon: "file-csv",
  route: "text",
  artifactKind: "dataset",
};

const IMAGE: Omit<FormatDescriptor, "label"> = {
  icon: "file-image",
  route: "server",
  artifactKind: "figure",
  serverReason: "图片需要 OCR，已排队交由服务端识别",
};

const FORMATS: Readonly<Record<string, FormatDescriptor>> = {
  pdf: { label: "PDF", icon: "file-pdf", route: "pdf", artifactKind: "paper" },

  docx: { label: "Word", icon: "file-doc", route: "ooxml-word", artifactKind: "paper" },
  docm: { label: "Word", icon: "file-doc", route: "ooxml-word", artifactKind: "paper" },
  doc: {
    label: "Word 97-2003",
    icon: "file-doc",
    route: "server",
    artifactKind: "paper",
    serverReason: "旧版 .doc 是二进制格式，已排队交由服务端解析",
  },
  rtf: {
    label: "RTF",
    icon: "file-doc",
    route: "server",
    artifactKind: "paper",
    serverReason: "RTF 的中文转义依赖代码页，交由服务端解析更可靠",
  },
  odt: { label: "ODF 文档", icon: "file-doc", route: "opendocument", artifactKind: "paper" },

  xlsx: { label: "Excel", icon: "file-xls", route: "ooxml-sheet", artifactKind: "dataset" },
  xlsm: { label: "Excel", icon: "file-xls", route: "ooxml-sheet", artifactKind: "dataset" },
  xls: {
    label: "Excel 97-2003",
    icon: "file-xls",
    route: "server",
    artifactKind: "dataset",
    serverReason: "旧版 .xls 是二进制格式，已排队交由服务端解析",
  },
  ods: { label: "ODF 表格", icon: "file-xls", route: "opendocument", artifactKind: "dataset" },

  pptx: { label: "PowerPoint", icon: "file-ppt", route: "ooxml-slides", artifactKind: "report" },
  ppt: {
    label: "PowerPoint 97-2003",
    icon: "file-ppt",
    route: "server",
    artifactKind: "report",
    serverReason: "旧版 .ppt 是二进制格式，已排队交由服务端解析",
  },
  odp: { label: "ODF 演示", icon: "file-ppt", route: "opendocument", artifactKind: "report" },

  md: { label: "Markdown", ...TEXT, icon: "file-md" },
  markdown: { label: "Markdown", ...TEXT, icon: "file-md" },
  mdx: { label: "Markdown", ...TEXT, icon: "file-md" },
  txt: { label: "纯文本", ...TEXT },
  log: { label: "日志", ...TEXT },
  rst: { label: "reStructuredText", ...TEXT },
  tex: { label: "LaTeX", ...TEXT, artifactKind: "paper" },
  bib: { label: "BibTeX", ...TEXT, artifactKind: "paper" },

  csv: { label: "CSV", ...TABLE_TEXT },
  tsv: { label: "TSV", ...TABLE_TEXT },

  json: { label: "JSON", ...TEXT, icon: "brackets-curly" },
  jsonl: { label: "JSON Lines", ...TEXT, icon: "brackets-curly" },
  ndjson: { label: "JSON Lines", ...TEXT, icon: "brackets-curly" },
  geojson: { label: "GeoJSON", ...TEXT, icon: "brackets-curly", artifactKind: "dataset" },

  ipynb: { label: "Jupyter", icon: "file-code", route: "notebook", artifactKind: "code" },
  py: { label: "Python", ...CODE },
  m: { label: "MATLAB", ...CODE },
  r: { label: "R", ...CODE },
  jl: { label: "Julia", ...CODE },
  js: { label: "JavaScript", ...CODE },
  ts: { label: "TypeScript", ...CODE },
  java: { label: "Java", ...CODE },
  c: { label: "C", ...CODE },
  h: { label: "C 头文件", ...CODE },
  cpp: { label: "C++", ...CODE },
  cs: { label: "C#", ...CODE },
  go: { label: "Go", ...CODE },
  sql: { label: "SQL", ...CODE },
  sh: { label: "Shell", ...CODE },
  yaml: { label: "YAML", ...CODE },
  yml: { label: "YAML", ...CODE },
  toml: { label: "TOML", ...CODE },
  ini: { label: "INI", ...CODE },
  xml: { label: "XML", ...CODE },
  html: { label: "HTML", ...CODE },
  css: { label: "CSS", ...CODE },

  png: { label: "PNG 图片", ...IMAGE },
  jpg: { label: "JPEG 图片", ...IMAGE },
  jpeg: { label: "JPEG 图片", ...IMAGE },
  webp: { label: "WebP 图片", ...IMAGE },
  gif: { label: "GIF 图片", ...IMAGE },
  bmp: { label: "BMP 图片", ...IMAGE },
  tif: { label: "TIFF 图片", ...IMAGE },
  tiff: { label: "TIFF 图片", ...IMAGE },

  zip: { label: "ZIP 压缩包", icon: "file-zip", route: "archive", artifactKind: "other" },
  rar: {
    label: "RAR 压缩包",
    icon: "file-zip",
    route: "server",
    artifactKind: "other",
    serverReason: "RAR 需要专用解码器，已排队交由服务端展开",
  },
  "7z": {
    label: "7z 压缩包",
    icon: "file-zip",
    route: "server",
    artifactKind: "other",
    serverReason: "7z 需要专用解码器，已排队交由服务端展开",
  },
};

const UNKNOWN: FormatDescriptor = {
  label: "未知格式",
  icon: "file",
  route: "server",
  artifactKind: "other",
  serverReason: "浏览器无法识别该格式，已排队交由服务端尝试解析",
};

/** MIME 兜底：扩展名缺失或不认识时，用浏览器给的类型再试一次。 */
const MIME_FALLBACK: ReadonlyArray<readonly [RegExp, string]> = [
  [/^application\/pdf$/, "pdf"],
  [/wordprocessingml\.document$/, "docx"],
  [/spreadsheetml\.sheet$/, "xlsx"],
  [/presentationml\.presentation$/, "pptx"],
  [/^application\/msword$/, "doc"],
  [/^application\/vnd\.ms-excel$/, "xls"],
  [/^application\/vnd\.ms-powerpoint$/, "ppt"],
  [/^application\/json$/, "json"],
  [/^text\/csv$/, "csv"],
  [/^text\/markdown$/, "md"],
  [/^application\/zip$/, "zip"],
  [/^image\//, "png"],
  [/^text\//, "txt"],
];

export function fileExtension(name: string): string {
  const base = name.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

export function describeFormat(name: string, mimeType: string): FormatDescriptor {
  const byExtension = FORMATS[fileExtension(name)];
  if (byExtension) return byExtension;
  const mime = mimeType.toLowerCase();
  const matched = MIME_FALLBACK.find(([pattern]) => pattern.test(mime));
  return matched ? FORMATS[matched[1]] : UNKNOWN;
}

/** 供 `<input type="file">` 的 accept 使用，避免系统选择器把可用格式灰掉。 */
export const ACCEPT_ATTRIBUTE = Object.keys(FORMATS)
  .map(extension => `.${extension}`)
  .join(",");
