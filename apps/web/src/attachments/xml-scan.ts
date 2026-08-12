/**
 * 面向 OOXML / OpenDocument 的极简 XML 扫描器。
 *
 * 这里刻意不用 DOMParser：抽文本只需要按文档顺序看到标签和文字，为一份几十兆
 * 的 sheet1.xml 建整棵 DOM 既费内存又慢，而且 DOMParser 只存在于浏览器里，
 * 解析逻辑就没法在 Node 里做单元测试。
 *
 * 支持的范围只覆盖 Office 生成的 XML：元素、属性、实体、CDATA、注释与处理指令。
 * 不做 DTD、命名空间归一和格式良好性校验——标签名连同前缀原样给出。
 */

const NAMED_ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: "\"",
  apos: "'",
  nbsp: "\u00a0",
};

export function decodeEntities(value: string): string {
  if (!value.includes("&")) return value;
  return value.replace(/&(#[Xx][0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);/g, (match, body: string) => {
    if (body.startsWith("#")) {
      const hex = body[1] === "x" || body[1] === "X";
      const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10);
      if (!Number.isInteger(code) || code < 0 || code > 0x10ffff) return match;
      try {
        return String.fromCodePoint(code);
      } catch {
        return match;
      }
    }
    return NAMED_ENTITIES[body] ?? match;
  });
}

/** 属性值里允许出现 `>`，找结束尖括号时必须跳过引号包住的片段。 */
function findTagEnd(source: string, start: number): number {
  let quote = "";
  for (let index = start + 1; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (character === quote) quote = "";
    } else if (character === "\"" || character === "'") {
      quote = character;
    } else if (character === ">") {
      return index;
    }
  }
  return -1;
}

export function parseAttributes(raw: string): Map<string, string> {
  const attributes = new Map<string, string>();
  let index = 0;
  while (index < raw.length) {
    while (index < raw.length && /\s/.test(raw[index])) index += 1;
    const nameStart = index;
    while (index < raw.length && !/[\s=]/.test(raw[index])) index += 1;
    if (index === nameStart) break;
    const name = raw.slice(nameStart, index);
    while (index < raw.length && /\s/.test(raw[index])) index += 1;
    if (raw[index] !== "=") {
      attributes.set(name, "");
      continue;
    }
    index += 1;
    while (index < raw.length && /\s/.test(raw[index])) index += 1;
    const quote = raw[index];
    if (quote !== "\"" && quote !== "'") break;
    const valueStart = index + 1;
    const valueEnd = raw.indexOf(quote, valueStart);
    if (valueEnd < 0) break;
    attributes.set(name, decodeEntities(raw.slice(valueStart, valueEnd)));
    index = valueEnd + 1;
  }
  return attributes;
}

export interface XmlVisitor {
  /** `attributes` 是原始属性串，需要时再交给 parseAttributes，避免逐元素建 Map。 */
  onOpen?: (name: string, attributes: string, selfClosing: boolean) => void;
  onClose?: (name: string) => void;
  onText?: (text: string) => void;
}

export function scanXml(source: string, visitor: XmlVisitor): void {
  let index = 0;
  while (index < source.length) {
    const start = source.indexOf("<", index);
    if (start < 0) {
      if (visitor.onText && index < source.length) visitor.onText(decodeEntities(source.slice(index)));
      return;
    }
    if (start > index && visitor.onText) visitor.onText(decodeEntities(source.slice(index, start)));

    if (source.startsWith("<!--", start)) {
      const end = source.indexOf("-->", start + 4);
      index = end < 0 ? source.length : end + 3;
      continue;
    }
    if (source.startsWith("<![CDATA[", start)) {
      const end = source.indexOf("]]>", start + 9);
      // CDATA 内部按字面量处理，不能再解实体。
      visitor.onText?.(source.slice(start + 9, end < 0 ? source.length : end));
      index = end < 0 ? source.length : end + 3;
      continue;
    }
    if (source.startsWith("<?", start) || source.startsWith("<!", start)) {
      const end = source.indexOf(">", start);
      index = end < 0 ? source.length : end + 1;
      continue;
    }

    const end = findTagEnd(source, start);
    if (end < 0) return;
    let body = source.slice(start + 1, end);
    index = end + 1;

    if (body.startsWith("/")) {
      visitor.onClose?.(body.slice(1).trim());
      continue;
    }
    const selfClosing = body.endsWith("/");
    if (selfClosing) body = body.slice(0, -1);
    const nameEnd = body.search(/\s/);
    const name = nameEnd < 0 ? body : body.slice(0, nameEnd);
    if (!name) continue;
    visitor.onOpen?.(name, nameEnd < 0 ? "" : body.slice(nameEnd + 1), selfClosing);
    // 自闭合元素补一次 close，调用方就不必到处判断两种写法。
    if (selfClosing) visitor.onClose?.(name);
  }
}

/** 收集某类元素内部的全部文字，用于 `<si>`、`<a:p>` 这类纯文本容器。 */
export function collectText(source: string, container: string, leaf: string): string[] {
  const collected: string[] = [];
  let containerDepth = 0;
  let leafDepth = 0;
  let buffer = "";
  scanXml(source, {
    onOpen(name) {
      if (name === container) containerDepth += 1;
      else if (name === leaf && containerDepth > 0) leafDepth += 1;
    },
    onText(text) {
      if (leafDepth > 0) buffer += text;
    },
    onClose(name) {
      if (name === leaf && leafDepth > 0) leafDepth -= 1;
      else if (name === container && containerDepth > 0) {
        containerDepth -= 1;
        if (containerDepth === 0) {
          collected.push(buffer);
          buffer = "";
        }
      }
    },
  });
  return collected;
}
