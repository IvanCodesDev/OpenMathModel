/**
 * 字节流转文本。
 *
 * 国内赛题附件里 GB18030 编码的 CSV/TXT 非常常见，直接按 UTF-8 解会解出满屏
 * 替换字符。这里先认 BOM，再用 `fatal` 模式试 UTF-8，失败才回落到 GB18030——
 * 反过来先试 GB18030 不行，它对任意字节都不报错，会把合法 UTF-8 解成乱码。
 */

function decodeWith(bytes: Uint8Array, encoding: string, fatal: boolean): string | null {
  try {
    return new TextDecoder(encoding, { fatal }).decode(bytes);
  } catch {
    return null;
  }
}

export function decodeBytes(bytes: Uint8Array): string {
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    return decodeWith(bytes.subarray(3), "utf-8", false) ?? "";
  }
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return decodeWith(bytes.subarray(2), "utf-16le", false) ?? "";
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return decodeWith(bytes.subarray(2), "utf-16be", false) ?? "";
  }
  return decodeWith(bytes, "utf-8", true)
    ?? decodeWith(bytes, "gb18030", false)
    ?? decodeWith(bytes, "utf-8", false)
    ?? "";
}

export async function decodeBlob(blob: Blob): Promise<string> {
  return decodeBytes(new Uint8Array(await blob.arrayBuffer()));
}

/** 折叠解析过程中产生的空行与行尾空白，避免卡片上的字数被排版噪声撑大。 */
export function tidyText(value: string): string {
  return value
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
