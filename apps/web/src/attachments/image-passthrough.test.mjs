import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

async function moduleFromSource(source) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  return `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`;
}

const formatsUrl = await moduleFromSource(
  await readFile(new URL("./formats.ts", import.meta.url), "utf8"),
);
const passthroughSource = (await readFile(new URL("./image-passthrough.ts", import.meta.url), "utf8"))
  .replace('from "./formats"', `from "${formatsUrl}"`);
const { planImagePassthrough, encodePassthroughImages, MAX_PASSTHROUGH_IMAGE_BYTES } = await import(
  await moduleFromSource(passthroughSource)
);
const { describeFormat } = await import(formatsUrl);

let nextId = 0;
function attachmentOf(name, bytes) {
  const file = new File([bytes], name);
  return { id: `att_${nextId += 1}`, file, descriptor: describeFormat(name, ""), phase: "parsed" };
}

const PNG = new Uint8Array([137, 80, 78, 71]);

test("picks bitmap images and leaves other attachments alone", () => {
  const items = [
    attachmentOf("题面.png", PNG),
    attachmentOf("数据.csv", new Uint8Array([49, 44, 50])),
    attachmentOf("照片.jpg", PNG),
  ];
  const plan = planImagePassthrough(items);
  assert.deepEqual(plan.send.map(item => item.file.name), ["题面.png", "照片.jpg"]);
  assert.equal(plan.skipped.length, 0, "非图片附件不进 skipped，照走文本通道");
});

test("oversized or provider-unsupported images fall back to OCR with a reason", () => {
  const oversized = attachmentOf("巨图.png", new Uint8Array(MAX_PASSTHROUGH_IMAGE_BYTES + 1));
  const tiff = attachmentOf("扫描.tiff", PNG);
  const plan = planImagePassthrough([oversized, tiff]);
  assert.equal(plan.send.length, 0);
  assert.match(plan.skipped[0].reason, /4MB/);
  assert.match(plan.skipped[1].reason, /格式/);
});

test("caps the number of passthrough images per message", () => {
  const items = Array.from({ length: 5 }, (_, index) => attachmentOf(`图${index}.png`, PNG));
  const plan = planImagePassthrough(items);
  assert.equal(plan.send.length, 4);
  assert.equal(plan.skipped.length, 1);
  assert.match(plan.skipped[0].reason, /最多直通/);
});

test("encodes files as plain base64 with extension-derived media type", async () => {
  const payloads = await encodePassthroughImages([attachmentOf("photo.JPG", "hello")]);
  assert.deepEqual(payloads, [{ media_type: "image/jpeg", data: "aGVsbG8=", name: "photo.JPG" }]);
});
