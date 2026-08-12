/**
 * 英文词典的质量门禁：漏译、空译和重复键都会静默降级成中文界面，
 * 只靠人眼很难发现，这里用断言把它们挡住。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const sourcePath = new URL("./en-US.ts", import.meta.url);
const source = await readFile(sourcePath, "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { EN_US_DICTIONARY } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

const CJK = /[\u4e00-\u9fff]/;
const entries = Object.entries(EN_US_DICTIONARY);

test("dictionary is not empty", () => {
  assert.ok(entries.length > 800, `expected a broad dictionary, got ${entries.length} entries`);
});

test("keys match the on-screen text exactly", () => {
  const untrimmed = entries.filter(([key]) => key !== key.trim() || key.length === 0);
  assert.deepEqual(untrimmed, [], "keys must be trimmed and non-empty to match whole text nodes");
});

test("every value is a real translation", () => {
  const noop = entries.filter(([key, value]) => value.trim() === "" || value === key);
  assert.deepEqual(noop, [], "empty or identical values silently fall back to Chinese");
});

test("no value leaves untranslated Chinese behind", () => {
  const leftovers = entries.filter(([, value]) => CJK.test(value));
  assert.deepEqual(leftovers, [], "these values still contain Chinese characters");
});

test("no duplicate keys shadow each other", () => {
  // 对象字面量会静默覆盖重复键，因此必须回到源码层面检查。
  const seen = new Map();
  const duplicates = [];
  for (const match of source.matchAll(/^ {2}"((?:[^"\\]|\\.)*)":/gm)) {
    const key = match[1];
    if (seen.has(key)) duplicates.push(key);
    else seen.set(key, true);
  }
  assert.deepEqual(duplicates, [], "duplicate keys must be merged into one entry");
});
