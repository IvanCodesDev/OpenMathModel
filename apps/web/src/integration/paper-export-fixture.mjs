/**
 * 论文导出序列化器测试共用的文档模型样例（不是测试文件本身，node --test 只跑 *.test.mjs）。
 * 覆盖：摘要 / 关键词、长英文串、公式后续段、列表、宽表 + 表题、图 + 图题、代码、引用、分隔线。
 */
import { readFile } from "node:fs/promises";
import { URL } from "node:url";
import ts from "typescript";

const compilerOptions = { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 };
const dataUrl = code => `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`;

/** 转译一个 TS 模块并把它的相对 / 包名 import 改写成给定 URL，返回可 import() 的 data: URL。 */
export async function transpiled(relative, replacements = {}) {
  const source = await readFile(new URL(relative, import.meta.url), "utf8");
  let code = ts.transpileModule(source, { compilerOptions }).outputText;
  for (const [specifier, url] of Object.entries(replacements)) {
    code = code.replaceAll(`from "${specifier}"`, `from ${JSON.stringify(url)}`);
  }
  return dataUrl(code);
}

export const documentUrl = await transpiled("./paper-document.ts");
export const imagesUrl = await transpiled("./paper-export-images.ts");

export const text = (value, marks = {}) => ({ kind: "text", text: value, marks });
export const math = tex => ({ kind: "math", tex });
const cell = (inlines, header = false) => ({ inlines, header });

/** 1200×800 的 PNG 文件头（尺寸只从 IHDR 读，内容无关紧要）。 */
export const PNG_1200x800 = new Uint8Array([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52,
  0, 0, 0x04, 0xb0, 0, 0, 0x03, 0x20, 8, 6, 0, 0, 0, 0x1d, 0x2f, 0x3a, 0x4e,
]);

export const LONG_DESCRIPTION = "网络待估参数集合，包含所有权重与偏置；前向传播、反向传播、梯度检验与泛化评估都以它为准";

export function sampleDocument() {
  return {
    title: "从零实现反向传播：手写 MLP 的推导与检验",
    images: [{ name: "fit vs baseline.png", mediaType: "image/png", bytes: PNG_1200x800 }],
    blocks: [
      { kind: "heading", level: 1, role: "abstract", inlines: [text("摘要")] },
      { kind: "paragraph", role: "abstract", inlines: [text("梯度检验 metrics.grad_check_max_abs_err=5.9e-11，结论可信。")] },
      { kind: "paragraph", role: "keywords", inlines: [text("关键词：", { bold: true }), text("反向传播；梯度检验")] },
      { kind: "heading", level: 1, inlines: [text("1 问题重述")] },
      { kind: "paragraph", inlines: [text("学习目标为")] },
      { kind: "math", tex: "\\min_{\\theta} J(\\theta)" },
      { kind: "paragraph", continued: true, inlines: [text("其中 "), math("J_i"), text(" 为交叉熵，100% 的 R&D_x 成本 #1。")] },
      { kind: "heading", level: 2, inlines: [text("1.1 子问题")] },
      { kind: "list", ordered: true, items: [[text("前向传播")], [text("反向传播 "), text("加粗", { bold: true })]] },
      {
        kind: "table",
        caption: [text("表 1 全文符号说明")],
        rows: [
          [cell([text("符号")], true), cell([text("含义")], true), cell([text("单位")], true)],
          [cell([math("\\theta")]), cell([text(LONG_DESCRIPTION)]), cell([text("无量纲")])],
        ],
      },
      { kind: "figure", image: 0, caption: [text("图 1 训练收敛曲线")], alt: "图 1 训练收敛曲线" },
      { kind: "figure", image: null, caption: [text("图 2 丢失的图")], alt: "图 2" },
      { kind: "code", text: "def f(x):\n\treturn x  # 注释" },
      { kind: "quote", inlines: [text("引用一句")] },
      { kind: "rule" },
      { kind: "paragraph", inlines: [text("见 "), text("文档", { href: "https://example.org/a?b=1#c" }), text("。\u0001")] },
    ],
  };
}
