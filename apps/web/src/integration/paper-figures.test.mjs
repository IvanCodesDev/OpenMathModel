import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-figures.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { artifactDownloadUrl, figureImageResolver, summarizeFigures } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture document-draft.4：三张真实图件（两张已插入、一张没有产物 id）。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/document-draft.4.json", import.meta.url),
    "utf8",
  ),
);

test("resolver maps listed file names (or url basenames) to artifact download links", () => {
  const resolve = figureImageResolver(fixture.figures);
  assert.equal(resolve("fit_vs_baseline.png"), "/api/v1/artifacts/art_8f2a1c3d4e5b/download");
  assert.equal(resolve("figures/fit_vs_baseline.png"), "/api/v1/artifacts/art_8f2a1c3d4e5b/download", "带路径按 basename 命中");
  assert.equal(resolve("./convergence.svg?v=2"), "/api/v1/artifacts/art_9a3b2d4e5f6c/download");
  assert.equal(resolve("sensitivity.png"), null, "没有产物 id 的图件不解析（不拼 404 链接）");
  assert.equal(resolve("https://evil.example/track.png"), null, "清单外的 url 一律不出图");
  assert.equal(resolve("FIT_VS_BASELINE.PNG"), null, "文件名大小写敏感，与产物 name 一致");
});

test("resolver is inert without figures and encodes the artifact id", () => {
  assert.equal(figureImageResolver(null)("fit.png"), null);
  assert.equal(figureImageResolver(undefined)("fit.png"), null);
  assert.equal(figureImageResolver([])("fit.png"), null);
  assert.equal(artifactDownloadUrl("art_1"), "/api/v1/artifacts/art_1/download");
  assert.equal(artifactDownloadUrl("a/b c"), "/api/v1/artifacts/a%2Fb%20c/download");
  // 同名条目先到先得
  const resolve = figureImageResolver([
    { number: 1, name: "a.png", artifact_id: "art_first", caption: "", source_stage: "EXPERIMENTING", inserted: true },
    { number: 2, name: "a.png", artifact_id: "art_second", caption: "", source_stage: "VALIDATING", inserted: false },
  ]);
  assert.equal(resolve("a.png"), "/api/v1/artifacts/art_first/download");
});

test("figure summary counts total and inserted; absent or empty list yields null", () => {
  assert.deepEqual(summarizeFigures(fixture.figures), { total: 3, inserted: 2 });
  assert.equal(summarizeFigures(null), null);
  assert.equal(summarizeFigures(undefined), null);
  assert.equal(summarizeFigures([]), null);
});
