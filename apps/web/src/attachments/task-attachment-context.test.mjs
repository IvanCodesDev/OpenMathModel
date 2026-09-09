import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { setTimeout as nodeSetTimeout } from "node:timers";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

// 浏览器全局桩：模块只用到 window.setTimeout（等待预算）、sessionStorage（摘录交接）与 fetch。
// setTimeout 立即触发，让「等服务端解析」的预算在测试里瞬间到期。
const { Response } = globalThis;
const storage = new Map();
globalThis.window = { setTimeout: (fn) => nodeSetTimeout(fn, 0) };
globalThis.sessionStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};

/** 每个运行的工作台：附件名 → 正文（null = 服务端仍在解析，请求永不返回）。 */
const workspaces = new Map();
const requested = [];
globalThis.fetch = (path) => {
  requested.push(path);
  const workspace = path.match(/^\/api\/v1\/task-runs\/([^/]+)\/workspace$/);
  if (workspace) {
    const files = workspaces.get(decodeURIComponent(workspace[1])) ?? new Map();
    const artifacts = [...files.keys()].map((name) => ({
      id: `art_${name}`, name, status: "READY", producer_node: null,
    }));
    return Promise.resolve(new Response(JSON.stringify({ artifacts }), { status: 200 }));
  }
  const text = path.match(/^\/api\/v1\/artifacts\/([^/]+)\/text$/);
  if (text) {
    const name = decodeURIComponent(text[1]).replace(/^art_/, "");
    for (const files of workspaces.values()) {
      if (!files.has(name)) continue;
      const body = files.get(name);
      if (body === null) return new Promise(() => {});
      return Promise.resolve(new Response(JSON.stringify({
        artifact_id: `art_${name}`, name, media_type: "application/pdf", status: "READY",
        engine: "pdf", characters: body.length, text: body,
      }), { status: 200 }));
    }
  }
  return Promise.resolve(new Response("not found", { status: 404 }));
};

const source = await readFile(new URL("./task-attachment-context.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  collectTaskAttachmentContext,
  persistTaskAttachmentExcerpts,
  resetTaskAttachmentContext,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const RUN_A = `run_${"a".repeat(32)}`;
const RUN_B = `run_${"b".repeat(32)}`;

function reset() {
  storage.clear();
  workspaces.clear();
  requested.length = 0;
  resetTaskAttachmentContext();
}

test("非运行归属（首页对话 / 演示态）不产生任务附件，也不去读标签页里残留的活动任务", async () => {
  reset();
  workspaces.set(RUN_A, new Map([["B题.pdf", "机器人竞技策略的优化问题……"]]));
  // 上一个任务页留下的标签页级身份：此前正是它把别的任务的附件串进新对话
  storage.set("openmathmodel.activeRunId", RUN_A);

  assert.equal(await collectTaskAttachmentContext("chat_1725700000000_abcd"), null);
  assert.equal(await collectTaskAttachmentContext(""), null);
  assert.deepEqual(requested, [], "不该发出任何工作台 / 正文请求");
});

test("附件按传入的运行取；换运行时清单缓存作废、不带上一个运行的附件", async () => {
  reset();
  workspaces.set(RUN_A, new Map([["A题.pdf", "A 题题面全文"]]));
  workspaces.set(RUN_B, new Map([["B题.pdf", "B 题题面全文"]]));

  const first = await collectTaskAttachmentContext(RUN_A);
  assert.ok(first, "运行 A 首轮应并入自己的附件");
  assert.match(first.block, /【任务附件：A题\.pdf】/);
  assert.doesNotMatch(first.block, /B题/);
  first.commit();
  assert.equal(await collectTaskAttachmentContext(RUN_A), null, "A 的附件已注入，同一运行不重复占上下文");

  const second = await collectTaskAttachmentContext(RUN_B);
  assert.ok(second, "切到运行 B 后应拉 B 自己的清单，而不是沿用 A 的缓存");
  assert.match(second.block, /【任务附件：B题\.pdf】/);
  assert.doesNotMatch(second.block, /A题/);
  assert.ok(
    requested.includes(`/api/v1/task-runs/${RUN_B}/workspace`),
    "B 的工作台清单必须真的请求过",
  );
  second.commit();

  // 切回 A：对话上下文已由 configureConversation 重建、不含此前注入的附件正文，
  // 附件也要重新并入一次，而不是沿用「已注入」标记
  const back = await collectTaskAttachmentContext(RUN_A);
  assert.ok(back);
  assert.match(back.block, /【任务附件：A题\.pdf】/);
});

test("浏览器解析摘录的兜底同样按运行隔离", async () => {
  reset();
  // A 有交接摘录；B 的服务端正文还在解析、也没有自己的摘录
  persistTaskAttachmentExcerpts(RUN_A, [{ name: "A题.pdf", excerpt: "A 题摘录", characters: 5000 }]);
  workspaces.set(RUN_A, new Map([["A题.pdf", null]]));
  workspaces.set(RUN_B, new Map([["B数据.xlsx", null]]));

  const a = await collectTaskAttachmentContext(RUN_A);
  assert.ok(a);
  assert.match(a.block, /【任务附件：A题\.pdf】（浏览器初步解析/);
  a.commit();

  const b = await collectTaskAttachmentContext(RUN_B);
  assert.ok(b);
  assert.match(b.block, /「B数据\.xlsx」，正文仍在解析中/);
  assert.doesNotMatch(b.block, /A题/, "B 的对话不能拿 A 的摘录兜底");
});
