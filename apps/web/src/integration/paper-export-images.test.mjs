import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-export-images.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { bytesToBase64, dataUrl, exportFileName, imagePixelSize } = await import(
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

test("pixel size: page-measured size wins, otherwise read from PNG / GIF / BMP / JPEG headers", () => {
  assert.deepEqual(imagePixelSize({ ...png, width: 640, height: 480 }), { width: 640, height: 480 });
  const pngHeader = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52, 0, 0, 0x04, 0xb0, 0, 0, 0x03, 0x20]);
  assert.deepEqual(imagePixelSize({ ...png, bytes: pngHeader }), { width: 1200, height: 800 });
  const gif = new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x40, 0x01, 0xf0, 0x00]);
  assert.deepEqual(imagePixelSize({ name: "a.gif", mediaType: "image/gif", bytes: gif }), { width: 320, height: 240 });
  const bmp = new Uint8Array(26);
  bmp.set([0x42, 0x4d]);
  new DataView(bmp.buffer).setInt32(18, 100, true);
  new DataView(bmp.buffer).setInt32(22, -50, true);
  assert.deepEqual(imagePixelSize({ name: "a.bmp", mediaType: "image/bmp", bytes: bmp }), { width: 100, height: 50 }, "自上而下存储的 BMP 高度为负");
  // JPEG：SOI + APP0（长 16）+ SOF0（高 600、宽 900）
  const jpeg = new Uint8Array([
    0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00,
    0xff, 0xc0, 0x00, 0x11, 0x08, 0x02, 0x58, 0x03, 0x84, 0x03, 0x01, 0x22, 0x00,
  ]);
  assert.deepEqual(imagePixelSize({ name: "a.jpg", mediaType: "image/jpeg", bytes: jpeg }), { width: 900, height: 600 });
  assert.equal(imagePixelSize({ name: "a.svg", mediaType: "image/svg+xml", bytes: new globalThis.TextEncoder().encode("<svg/>") }), null);
});
