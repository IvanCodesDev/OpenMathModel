import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath, URL } from "node:url";
import ts from "typescript";
import { build } from "esbuild";

// network-preferences 无依赖，走单文件转译；account-preferences 依赖 auth/api，
// 用 esbuild 打包后再导入——两条路径都测的是浏览器里真正会跑的代码。
const source = await readFile(new URL("./network-preferences.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  DEFAULT_REQUEST_TIMEOUT_SECONDS,
  MAX_REQUEST_TIMEOUT_SECONDS,
  MIN_REQUEST_TIMEOUT_SECONDS,
  normalizeTimeoutSeconds,
  requestTimeoutSeconds,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const accountBundle = await build({
  entryPoints: [fileURLToPath(new URL("./account-preferences.ts", import.meta.url))],
  bundle: true,
  format: "esm",
  platform: "neutral",
  target: "es2022",
  write: false,
});
const { parseMaxConcurrency } = await import(
  `data:text/javascript;base64,${Buffer.from(accountBundle.outputFiles[0].text).toString("base64")}`
);

test("accepts the string values that number inputs produce", () => {
  assert.equal(normalizeTimeoutSeconds("120"), 120);
  assert.equal(normalizeTimeoutSeconds("45.6"), 46);
  assert.equal(normalizeTimeoutSeconds(90), 90);
});

test("clamps timeouts outside the safe range", () => {
  assert.equal(normalizeTimeoutSeconds("1"), MIN_REQUEST_TIMEOUT_SECONDS);
  assert.equal(normalizeTimeoutSeconds("99999"), MAX_REQUEST_TIMEOUT_SECONDS);
  assert.equal(normalizeTimeoutSeconds(-30), MIN_REQUEST_TIMEOUT_SECONDS);
});

test("falls back to the default instead of NaN or empty values", () => {
  for (const junk of ["", "  ", "abc", null, undefined, {}, Number.NaN]) {
    assert.equal(normalizeTimeoutSeconds(junk), DEFAULT_REQUEST_TIMEOUT_SECONDS);
  }
});

test("survives environments without localStorage", () => {
  // Node 里没有 localStorage：读取器必须回落默认值而不是抛 ReferenceError。
  assert.equal(requestTimeoutSeconds(), DEFAULT_REQUEST_TIMEOUT_SECONDS);
});

test("parses the panel labels for max concurrency", () => {
  assert.equal(parseMaxConcurrency("3 个"), 3);
  assert.equal(parseMaxConcurrency("8 个"), 8);
  assert.equal(parseMaxConcurrency(1), 1);
});

test("rejects out-of-range or malformed concurrency values", () => {
  for (const junk of ["0 个", "9 个", "", "个", null, undefined, 2.5]) {
    assert.equal(parseMaxConcurrency(junk), null);
  }
});
