/**
 * 可读性偏好的取值规范化：设置来自 localStorage 与表单控件，
 * 可能是字符串、越界数字或被手改过的脏数据，落到 CSS 变量前必须先夹紧。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./display-preferences.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { normalizeDisplayPreferences, TEXT_BASE_PX, TEXT_MIN_PX, TEXT_MAX_PX } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

test("falls back to the base size when nothing is stored", () => {
  assert.deepEqual(normalizeDisplayPreferences({}), {
    fontSize: TEXT_BASE_PX,
    reduceMotion: false,
    highContrast: false,
  });
  assert.equal(normalizeDisplayPreferences(null).fontSize, TEXT_BASE_PX);
});

test("accepts the string values that range inputs produce", () => {
  assert.equal(normalizeDisplayPreferences({ fontSize: "17" }).fontSize, 17);
});

test("clamps sizes outside the slider range", () => {
  assert.equal(normalizeDisplayPreferences({ fontSize: 2 }).fontSize, TEXT_MIN_PX);
  assert.equal(normalizeDisplayPreferences({ fontSize: 999 }).fontSize, TEXT_MAX_PX);
});

test("rejects unusable sizes instead of writing NaN into CSS", () => {
  for (const bad of ["", "abc", NaN, Infinity, undefined]) {
    assert.equal(normalizeDisplayPreferences({ fontSize: bad }).fontSize, TEXT_BASE_PX, String(bad));
  }
});

test("only an explicit true enables a toggle", () => {
  assert.equal(normalizeDisplayPreferences({ reduceMotion: true }).reduceMotion, true);
  assert.equal(normalizeDisplayPreferences({ reduceMotion: "true" }).reduceMotion, false);
  assert.equal(normalizeDisplayPreferences({ highContrast: 1 }).highContrast, false);
});
