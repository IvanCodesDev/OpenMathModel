/**
 * 聊天气泡的 Markdown 渲染：转义优先的受限子集，模型输出按不可信内容处理。
 *
 * 管线：抽出代码块与公式做占位 → 全文 HTML 转义 → 行级块解析（标题/列表/
 * 引用/表格/分隔线/段落）→ 行内标记（粗斜体/行内代码/链接）→ 回填占位。
 * 原始文本从不直接进 innerHTML；链接只放行 http(s)，其余一律当纯文本。
 *
 * 图片默认不渲染（`![alt](url)` 原样当文字）：模型输出不可信，不给远端图片加载 /
 * 追踪留口子。只有调用方显式传 `resolveImage`（论文页按 DocumentDraft.figures 把
 * 文件名解析成本站产物下载链接）且解析器给出 URL 时，独占一行的图片才渲染成
 * `<figure><img><figcaption>`；解析不到的仍是纯文本。
 *
 * 公式输出为带 data-tex 的节点（行内加 data-tex-inline），由 text/math-typeset
 * 的 KaTeX 排版器排版；加载前节点内保留 LaTeX 源码作为可读回退，与方法库一致。
 * 本文件不做任何导入，方便按仓库惯例用 data URL 转译做单元测试。
 */

export interface RenderMarkdownOptions {
  /**
   * 图片 url（Markdown 原文，未转义）→ 可加载的同源 URL；返回 null 表示不认这张图
   * （保持纯文本）。缺省 = 一律不渲染图片。
   */
  resolveImage?: (url: string, alt: string) => string | null;
}

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#039;",
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => HTML_ESCAPES[character]);
}

function mathHtml(tex: string, display: boolean): string {
  const escaped = escapeHtml(tex.trim());
  return display
    ? `<div class="md-math-block" data-tex="${escaped}">$$${escaped}$$</div>`
    : `<span class="md-math" data-tex="${escaped}" data-tex-inline="true">$${escaped}$</span>`;
}

/** 占位符用私有区字符 \uE000 包裹序号：正常文本不会出现，也不被转义碰到。 */
const STASH_MARK = "\uE000";
const STASH_TOKEN = /\uE000(\d+)\uE000/g;

interface Stash {
  entries: string[];
  /** 块级占位（代码块 / figure）的序号：行级解析遇到它们时直接作为块输出，不裹进 <p>。 */
  blocks: Set<number>;
}

function put(stash: Stash, html: string, block = false): string {
  stash.entries.push(html);
  const index = stash.entries.length - 1;
  if (block) stash.blocks.add(index);
  return `${STASH_MARK}${index}${STASH_MARK}`;
}

/**
 * 围栏语言表：[别名（空格分隔）, 规范 id, 显示名]。
 * 规范 id = highlight.js 的语法模块名，text/code-language-loaders 按它按需加载语法；
 * 同一语法可以对应多个显示名（html / svg 都走 xml 语法，标题带各写各的）。
 * 不在表里的语言：id 取小写原文（不会有语法，只做等宽排版），显示名首字母大写。
 */
const CODE_LANGUAGE_TABLE: ReadonlyArray<readonly [string, string, string]> = [
  // 科学计算 / 统计
  ["python py python3 py3", "python", "Python"],
  ["r rlang", "r", "R"],
  ["julia jl", "julia", "Julia"],
  ["matlab m", "matlab", "MATLAB"],
  ["octave", "matlab", "Octave"],
  ["scilab sci", "scilab", "Scilab"],
  ["mathematica wolfram wl mma", "mathematica", "Mathematica"],
  ["maxima", "maxima", "Maxima"],
  ["stan", "stan", "Stan"],
  ["sas", "sas", "SAS"],
  ["stata do", "stata", "Stata"],
  ["gams", "gams", "GAMS"],
  ["gauss", "gauss", "GAUSS"],
  ["fortran f90 f95 f03 f08 for", "fortran", "Fortran"],
  ["excel xls xlsx", "excel", "Excel"],
  // 通用编程
  ["c h", "c", "C"],
  ["cpp c++ cc cxx hpp hh hxx", "cpp", "C++"],
  ["csharp cs c#", "csharp", "C#"],
  ["java", "java", "Java"],
  ["kotlin kt kts", "kotlin", "Kotlin"],
  ["scala sc", "scala", "Scala"],
  ["go golang", "go", "Go"],
  ["rust rs", "rust", "Rust"],
  ["swift", "swift", "Swift"],
  ["objectivec objc objective-c mm", "objectivec", "Objective-C"],
  ["javascript js mjs cjs", "javascript", "JavaScript"],
  ["jsx", "javascript", "JSX"],
  ["typescript ts mts cts", "typescript", "TypeScript"],
  ["tsx", "typescript", "TSX"],
  ["coffeescript coffee", "coffeescript", "CoffeeScript"],
  ["livescript ls", "livescript", "LiveScript"],
  ["dart", "dart", "Dart"],
  ["php php5 php7 php8", "php", "PHP"],
  ["ruby rb", "ruby", "Ruby"],
  ["perl pl pm", "perl", "Perl"],
  ["lua", "lua", "Lua"],
  ["groovy", "groovy", "Groovy"],
  ["gradle", "gradle", "Gradle"],
  ["haskell hs", "haskell", "Haskell"],
  ["erlang erl", "erlang", "Erlang"],
  ["elixir ex exs", "elixir", "Elixir"],
  ["clojure clj cljs edn", "clojure", "Clojure"],
  ["lisp elisp emacs-lisp cl", "lisp", "Lisp"],
  ["scheme scm racket rkt", "scheme", "Scheme"],
  ["ocaml ml", "ocaml", "OCaml"],
  ["reasonml reason re", "reasonml", "ReasonML"],
  ["fsharp fs f#", "fsharp", "F#"],
  ["sml", "sml", "Standard ML"],
  ["elm", "elm", "Elm"],
  ["haxe hx", "haxe", "Haxe"],
  ["nim", "nim", "Nim"],
  ["crystal cr", "crystal", "Crystal"],
  ["d dlang", "d", "D"],
  ["delphi", "delphi", "Delphi"],
  ["pascal pas", "delphi", "Pascal"],
  ["ada adb ads", "ada", "Ada"],
  ["vbnet vb vb.net", "vbnet", "VB.NET"],
  ["vbscript vbs", "vbscript", "VBScript"],
  ["basic", "basic", "BASIC"],
  ["prolog", "prolog", "Prolog"],
  ["coq", "coq", "Coq"],
  ["smalltalk st", "smalltalk", "Smalltalk"],
  ["tcl tk", "tcl", "Tcl"],
  ["awk", "awk", "AWK"],
  ["applescript", "applescript", "AppleScript"],
  ["actionscript as as3", "actionscript", "ActionScript"],
  ["autohotkey ahk", "autohotkey", "AutoHotkey"],
  ["vala", "vala", "Vala"],
  ["qml", "qml", "QML"],
  ["processing pde", "processing", "Processing"],
  ["arduino ino", "arduino", "Arduino"],
  ["glsl vert frag", "glsl", "GLSL"],
  ["openscad scad", "openscad", "OpenSCAD"],
  ["q kdb", "q", "q"],
  ["brainfuck bf", "brainfuck", "Brainfuck"],
  // 硬件 / 底层
  ["verilog v sv systemverilog", "verilog", "Verilog"],
  ["vhdl", "vhdl", "VHDL"],
  ["llvm", "llvm", "LLVM IR"],
  ["wasm wat", "wasm", "WebAssembly"],
  ["x86asm asm nasm", "x86asm", "x86 Assembly"],
  ["armasm arm", "armasm", "ARM Assembly"],
  ["mipsasm mips", "mipsasm", "MIPS Assembly"],
  ["avrasm avr", "avrasm", "AVR Assembly"],
  ["gcode nc", "gcode", "G-code"],
  // Shell / 系统 / 构建
  ["bash sh shell zsh", "bash", "Bash"],
  ["console shellsession", "shell", "Shell"],
  ["powershell ps ps1 pwsh", "powershell", "PowerShell"],
  ["dos bat cmd batch", "dos", "Batch"],
  ["makefile make mk", "makefile", "Makefile"],
  ["cmake", "cmake", "CMake"],
  ["dockerfile docker", "dockerfile", "Dockerfile"],
  ["nginx nginxconf", "nginx", "Nginx"],
  ["apache apacheconf htaccess", "apache", "Apache"],
  ["nix", "nix", "Nix"],
  ["puppet pp", "puppet", "Puppet"],
  ["vim viml vimscript", "vim", "Vim Script"],
  ["nsis", "nsis", "NSIS"],
  // 数据 / 配置 / 标记
  ["json jsonc json5", "json", "JSON"],
  ["yaml yml", "yaml", "YAML"],
  ["ini cfg conf", "ini", "INI"],
  ["toml", "ini", "TOML"],
  ["properties", "properties", "Properties"],
  ["xml xsl xslt xsd plist wsdl rss atom", "xml", "XML"],
  ["html htm xhtml", "xml", "HTML"],
  ["svg", "xml", "SVG"],
  ["vue", "xml", "Vue"],
  ["css", "css", "CSS"],
  ["scss", "scss", "SCSS"],
  ["less", "less", "Less"],
  ["stylus styl", "stylus", "Stylus"],
  ["sql", "sql", "SQL"],
  ["mysql", "sql", "MySQL"],
  ["sqlite", "sql", "SQLite"],
  ["pgsql postgres postgresql plpgsql", "pgsql", "PostgreSQL"],
  ["graphql gql", "graphql", "GraphQL"],
  ["protobuf proto", "protobuf", "Protocol Buffers"],
  ["thrift", "thrift", "Thrift"],
  ["http https", "http", "HTTP"],
  ["diff patch udiff", "diff", "Diff"],
  ["markdown md mkd", "markdown", "Markdown"],
  ["latex tex sty cls", "latex", "LaTeX"],
  ["asciidoc adoc", "asciidoc", "AsciiDoc"],
  ["django jinja jinja2", "django", "Django"],
  ["handlebars hbs mustache", "handlebars", "Handlebars"],
  ["twig", "twig", "Twig"],
  ["erb", "erb", "ERB"],
  ["gherkin feature cucumber", "gherkin", "Gherkin"],
  ["dns zone bind", "dns", "DNS Zone"],
  ["abnf", "abnf", "ABNF"],
  ["bnf", "bnf", "BNF"],
  ["ebnf", "ebnf", "EBNF"],
  // 纯文本：不高亮，但要有标签，读者知道这一块就是原样文字
  ["text txt plain plaintext", "plaintext", "Text"],
  ["log", "plaintext", "Log"],
  ["output out", "plaintext", "Output"],
  ["csv", "plaintext", "CSV"],
  ["tsv", "plaintext", "TSV"],
];

const CODE_LANGUAGES: Record<string, { id: string; label: string }> = {};
for (const [aliases, id, label] of CODE_LANGUAGE_TABLE) {
  for (const alias of aliases.split(" ")) CODE_LANGUAGES[alias] = { id, label };
}

/** 表里出现过的全部规范 id（去重）：供测试核对每个 id 都有对应的语法加载器。 */
export function codeLanguageIds(): string[] {
  return [...new Set(CODE_LANGUAGE_TABLE.map(([, id]) => id))];
}

export function codeLanguage(raw: string): { id: string; label: string } {
  const key = raw.trim().toLowerCase();
  // 没标语言的围栏按纯文本处理：标题带显示 Text，与显式写 ```txt 完全一致。
  // 不猜语言——猜错比不猜更误导，且流式期间猜测结果会随文本增长来回跳。
  if (!key) return CODE_LANGUAGES.plaintext;
  const known = CODE_LANGUAGES[key];
  if (known) return known;
  return { id: key, label: key.charAt(0).toUpperCase() + key.slice(1) };
}

/**
 * 代码块：包裹容器承载标题带（语言标签、复制按钮由 CSS 与 text/code-blocks 补上），
 * `<pre><code>` 只放转义后的源码。data-lang 是规范 id，data-label 是显示名——
 * 每一块都有标签（没标语言的按 Text），标题带不会出现只剩一个复制按钮的空带。
 */
function codeBlockHtml(lang: string, code: string): string {
  const language = codeLanguage(lang);
  const attrs = language.id ? ` data-lang="${escapeHtml(language.id)}"` : "";
  const label = language.label ? ` data-label="${escapeHtml(language.label)}"` : "";
  return `<div class="md-code"${attrs}${label}><pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre></div>`;
}

/** 独占一行的 Markdown 图片：`![alt](url)`，url 不含空白与右括号，可带 "title"。 */
const IMAGE_LINE = /^[^\S\n]*!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"\n]*")?\)[^\S\n]*$/gm;

function figureHtml(src: string, alt: string): string {
  const caption = alt.trim();
  return `<figure class="md-figure"><img src="${escapeHtml(src)}" alt="${escapeHtml(caption)}" loading="lazy">`
    + (caption ? `<figcaption>${escapeHtml(caption)}</figcaption>` : "")
    + "</figure>";
}

function restore(stash: Stash, text: string): string {
  return text.replace(STASH_TOKEN, (_match, index) => stash.entries[Number(index)] ?? "");
}

/** 行内标记；输入已经过 HTML 转义。 */
function renderInline(stash: Stash, text: string): string {
  let result = text;
  // 行内代码优先占位，避免其中的 * _ ~ 被后续规则改写
  result = result.replace(/`([^`\n]+)`/g, (_match, code: string) =>
    put(stash, `<code class="md-inline-code">${code}</code>`));
  // 链接只认显式 http(s)，javascript: 等一律保持纯文本
  result = result.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_match, label: string, href: string) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  result = result.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  result = result.replace(/(?<![*\w])\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
  result = result.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return result;
}

// GFM 分隔行允许每格单条横线（| - | - |），部分模型确实这么输出，故用 -+ 而非 -{2,}
const TABLE_DIVIDER = /^\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/;

function tableCells(line: string): string[] {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(cell => cell.trim());
}

/** 把模型回复渲染成安全的 HTML 片段。 */
export function renderMarkdown(source: string, options: RenderMarkdownOptions = {}): string {
  const stash: Stash = { entries: [], blocks: new Set() };
  let text = String(source ?? "").replace(/\r\n?/g, "\n");

  // 1. 代码块（未闭合的按到文末处理，流式渲染时代码块随增量增长）。
  //    块级占位：不裹进 <p>——否则浏览器会在 <pre> 前掐断段落，前后各留一个游离的
  //    <br>、尾部多一个空段落，代码块上下间距忽大忽小。
  text = text.replace(/```([\w+#.-]*)[^\S\n]*\n?([\s\S]*?)(?:```|$)/g, (_match, lang: string, code: string) =>
    put(stash, codeBlockHtml(lang, code), true));

  // 1b. 图片：只在调用方给了解析器、且解析器认这张图时成块；其余保持纯文本
  const { resolveImage } = options;
  if (resolveImage) {
    text = text.replace(IMAGE_LINE, (match, alt: string, url: string) => {
      const src = resolveImage(url, alt);
      return src ? put(stash, figureHtml(src, alt), true) : match;
    });
  }

  // 2. 公式：块级（$$…$$、\[…\]）与行内（\(…\)、$…$）
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_match, tex: string) => put(stash, mathHtml(tex, true)));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_match, tex: string) => put(stash, mathHtml(tex, true)));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_match, tex: string) => put(stash, mathHtml(tex, false)));
  // 行内 $…$ 要求两侧紧贴内容，避免把「$5 和 $10」当成公式
  text = text.replace(/\$(?!\s)([^$\n]*?[^\s$])\$/g, (_match, tex: string) => put(stash, mathHtml(tex, false)));

  // 3. 剩余文本统一转义；此后所有替换只针对已转义文本
  text = escapeHtml(text);

  // 4. 行级块解析
  const lines = text.split("\n");
  const blocks: string[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(`<p>${paragraph.map(line => renderInline(stash, line)).join("<br>")}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map(item => `<li>${renderInline(stash, item)}</li>`).join("");
      blocks.push(list.ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`);
      list = null;
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      blocks.push(`<blockquote>${quote.map(line => renderInline(stash, line)).join("<br>")}</blockquote>`);
      quote = [];
    }
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();

    if (!trimmed) {
      flushAll();
      continue;
    }

    // 块级占位（代码块 / 图）：直接成块，不裹进段落。通常独占一行；模型偶尔把说明
    // 写在闭合围栏同一行（"``` 见上"），这时把两侧文字各自当段落、占位仍然成块。
    const blockTokens = [...trimmed.matchAll(STASH_TOKEN)].filter(match => stash.blocks.has(Number(match[1])));
    if (blockTokens.length) {
      let cursor = 0;
      for (const match of blockTokens) {
        const before = trimmed.slice(cursor, match.index).trim();
        if (before) paragraph.push(before);
        flushAll();
        blocks.push(match[0]);
        cursor = match.index + match[0].length;
      }
      const after = trimmed.slice(cursor).trim();
      if (after) paragraph.push(after);
      continue;
    }

    // 表格：表头行 + 分隔行
    if (trimmed.startsWith("|") && TABLE_DIVIDER.test(lines[index + 1]?.trim() ?? "")) {
      flushAll();
      const head = tableCells(trimmed).map(cell => `<th>${renderInline(stash, cell)}</th>`).join("");
      const rows: string[] = [];
      let cursor = index + 2;
      while (cursor < lines.length && lines[cursor].trim().startsWith("|")) {
        const cells = tableCells(lines[cursor].trim()).map(cell => `<td>${renderInline(stash, cell)}</td>`).join("");
        rows.push(`<tr>${cells}</tr>`);
        cursor += 1;
      }
      blocks.push(`<table class="md-table"><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
      index = cursor - 1;
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInline(stash, heading[2])}</h${level}>`);
      continue;
    }

    if (/^(?:-{3,}|\*{3,})$/.test(trimmed)) {
      flushAll();
      blocks.push("<hr>");
      continue;
    }

    // 引用：原文 "> " 转义后是 "&gt; "
    const quoted = /^&gt;\s?(.*)$/.exec(trimmed);
    if (quoted) {
      flushParagraph();
      flushList();
      quote.push(quoted[1]);
      continue;
    }

    const unordered = /^[-*]\s+(.*)$/.exec(trimmed);
    const ordered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (unordered || ordered) {
      flushParagraph();
      flushQuote();
      const isOrdered = Boolean(ordered);
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push((unordered ?? ordered)![1]);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(trimmed);
  }
  flushAll();

  return restore(stash, blocks.join(""));
}
