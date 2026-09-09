/**
 * 语法加载器与围栏语言表的一致性门禁：表里的每个规范 id 都得有加载器，加载器指向的
 * highlight.js 模块都得真的存在——否则某种语言会静默退化成"有标签、没颜色"，肉眼很难发现。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

async function load(relativePath) {
  const source = await readFile(new URL(relativePath, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  return import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);
}

const { LANGUAGE_LOADERS, LANGUAGE_DEPENDENCIES } = await load("./code-language-loaders.ts");
const { codeLanguageIds } = await load("./markdown.ts");
const require = createRequire(import.meta.url);

test("every language id in the fence table has a loader (plaintext is the only deliberate exception)", () => {
  const missing = codeLanguageIds().filter(id => id !== "plaintext" && !(id in LANGUAGE_LOADERS));
  assert.deepEqual(missing, [], "these ids would render a label but never get highlighted");
});

test("every loader points at a highlight.js grammar that actually ships", () => {
  const broken = Object.keys(LANGUAGE_LOADERS).filter(id => {
    try {
      require.resolve(`highlight.js/lib/languages/${id}`);
      return false;
    } catch {
      return true;
    }
  });
  assert.deepEqual(broken, [], "typo in a grammar module name");
  assert.ok(Object.keys(LANGUAGE_LOADERS).length >= 100, "the point of the table is breadth");
});

test("embedded-language dependencies only reference loadable grammars", () => {
  const dangling = Object.entries(LANGUAGE_DEPENDENCIES)
    .flatMap(([host, deps]) => [host, ...deps])
    .filter(id => !(id in LANGUAGE_LOADERS));
  assert.deepEqual([...new Set(dangling)], []);
});
