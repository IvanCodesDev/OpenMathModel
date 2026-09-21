import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-export-images.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { MHTML_BASE, bytesToBase64, dataUrl, exportFileName, latexFigure, mhtmlDocument, mhtmlImageLocation } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

const png = { name: "fit.png", mediaType: "image/png", bytes: new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) };

test("export file names: basename only, unsafe characters replaced, extension from media type, duplicates numbered", () => {
  const used = new Set();
  assert.equal(exportFileName("figures/fit.png", 1, "image/png", used), "fit.png");
  assert.equal(exportFileName("fit.png", 2, "image/png", used), "fit-2.png", "重名加序号");
  assert.equal(exportFileName("C:\\tmp\\a:b*c.png?v=1", 3, "image/png", used), "a_b_c.png", "路径、查询串与不安全字符");
  assert.equal(exportFileName("图 4 灵敏度", 4, "image/png", used), "图 4 灵敏度.png", "缺后缀按媒体类型补");
  assert.equal(exportFileName("", 5, "image/svg+xml", used), "figure-5.svg", "没名字按序号造");
  assert.equal(exportFileName("..", 6, "application/octet-stream", used), "figure-6", "未知媒体类型不补后缀");
});

test("base64 and data URL round-trip the bytes", () => {
  assert.equal(bytesToBase64(png.bytes), "iVBORw0KGgo=");
  assert.equal(dataUrl(png), "data:image/png;base64,iVBORw0KGgo=");
  // 十万字节跨越分块边界（0x8000）：与一次性编码结果逐字节一致
  const big = new Uint8Array(100_000).map((_, index) => index % 251);
  const reference = globalThis.btoa(Array.from(big, byte => String.fromCharCode(byte)).join(""));
  assert.equal(bytesToBase64(big), reference, "分块编码不丢字节");
});

test("MHTML document: html part first, one base64 part per image, locations match the img src", () => {
  const location = mhtmlImageLocation(png);
  assert.equal(location, `${MHTML_BASE}figures/fit.png`);
  const html = `<html><body><p>正文</p><figure><img src="${location}" width="600" height="300"></figure></body></html>`;
  const mhtml = mhtmlDocument(html, [png]);
  assert.ok(mhtml.startsWith("MIME-Version: 1.0\r\nContent-Type: multipart/related; type=\"text/html\"; boundary="));
  const boundary = /boundary="([^"]+)"/.exec(mhtml)[1];
  assert.ok(mhtml.endsWith(`\r\n--${boundary}--\r\n`), "以收尾边界结束");
  const parts = mhtml.slice(0, -(`\r\n--${boundary}--\r\n`.length)).split(`\r\n--${boundary}\r\n`);
  assert.equal(parts.length, 3, "头 + html 部件 + 图部件");
  assert.match(parts[1], /^Content-Type: text\/html; charset="utf-8"\r\nContent-Location: file:\/\/\/C:\/OpenMathModel\/paper\.htm\r\n\r\n<html>/);
  assert.match(parts[2], /^Content-Type: image\/png\r\nContent-Transfer-Encoding: base64\r\nContent-Location: file:\/\/\/C:\/OpenMathModel\/figures\/fit\.png\r\n\r\niVBORw0KGgo=/);
});

test("MHTML base64 bodies wrap at 76 columns; non-ascii names are percent-encoded in locations", () => {
  const long = { name: "图 4 灵敏度.png", mediaType: "image/png", bytes: new Uint8Array(200).fill(7) };
  const mhtml = mhtmlDocument("<html></html>", [long]);
  const body = mhtml.split("Content-Location: ").pop().split("\r\n\r\n")[1].split("\r\n").filter(Boolean);
  assert.ok(body.length > 1 && body.slice(0, -1).every(line => line.length === 76));
  assert.ok(mhtml.includes(`${MHTML_BASE}figures/${encodeURIComponent("图 4 灵敏度.png")}`));
});

test("LaTeX figure block references figures/<name> and escapes the caption", () => {
  assert.equal(
    latexFigure("fit_vs_baseline.png", "图 1 拟合曲线（R^2 & 残差）"),
    '\\begin{figure}[htbp]\n\\centering\n\\includegraphics[width=0.8\\textwidth]{"figures/fit_vs_baseline.png"}'
    + "\n\\caption*{图 1 拟合曲线（R\\textasciicircum{}2 \\& 残差）}\n\\end{figure}",
  );
  assert.ok(!latexFigure("a.png", "   ").includes("\\caption"), "没有图题就不写 caption");
});
