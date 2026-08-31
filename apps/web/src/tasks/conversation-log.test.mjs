import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./conversation-log.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { parseConversationLog } = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const wrap = entries => JSON.stringify({ entries, saved_at: 1 });

test("accepts well-formed user/assistant entries in order", () => {
  assert.deepEqual(
    parseConversationLog(wrap([
      { role: "user", text: "帮我分析题目", attachments: ["A题.pdf"] },
      { role: "assistant", text: "好的，先看约束。" },
      { role: "assistant", text: "开场分析……", opening: true },
    ])),
    [
      { role: "user", text: "帮我分析题目", attachments: ["A题.pdf"] },
      { role: "assistant", text: "好的，先看约束。" },
      { role: "assistant", text: "开场分析……", opening: true },
    ],
  );
});

test("drops malformed entries instead of failing the whole log", () => {
  assert.deepEqual(
    parseConversationLog(wrap([
      { role: "system", text: "不认识的角色" },
      { role: "user", text: "" },
      { role: "user" },
      { role: "assistant", text: "有效回复" },
      "not-an-object",
    ])),
    [{ role: "assistant", text: "有效回复" }],
  );
});

test("sanitizes attachment names and opening flags", () => {
  assert.deepEqual(
    parseConversationLog(wrap([
      { role: "user", text: "带杂质的附件", attachments: ["ok.csv", 42, null] },
      { role: "assistant", text: "回复", opening: "yes" },
    ])),
    [
      { role: "user", text: "带杂质的附件", attachments: ["ok.csv"] },
      { role: "assistant", text: "回复" },
    ],
  );
});

test("rejects missing or unparsable storage values", () => {
  assert.deepEqual(parseConversationLog(null), []);
  assert.deepEqual(parseConversationLog(""), []);
  assert.deepEqual(parseConversationLog("{"), []);
  assert.deepEqual(parseConversationLog(JSON.stringify({ entries: "oops" })), []);
});

test("keeps reply reasoning for the thought-review box and drops junk values", () => {
  assert.deepEqual(
    parseConversationLog(wrap([
      { role: "assistant", text: "结论", reasoning: "先设变量再消元……" },
      { role: "assistant", text: "开场分析", opening: true, reasoning: "题面拆解……" },
      { role: "assistant", text: "无思考的普通回复", reasoning: "" },
      { role: "assistant", text: "思考字段是杂质", reasoning: 42 },
    ])),
    [
      { role: "assistant", text: "结论", reasoning: "先设变量再消元……" },
      { role: "assistant", text: "开场分析", opening: true, reasoning: "题面拆解……" },
      { role: "assistant", text: "无思考的普通回复" },
      { role: "assistant", text: "思考字段是杂质" },
    ],
  );
});
