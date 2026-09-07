import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./conversation-log.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
// load/clear 走 localStorage：Node 里用 Map 充当同步存储，行为与浏览器一致
const store = new Map();
globalThis.localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => { store.set(key, String(value)); },
  removeItem: key => { store.delete(key); },
  key: index => [...store.keys()][index] ?? null,
  get length() { return store.size; },
};

const {
  clearAllConversationLogs,
  clearConversationLog,
  loadConversationLog,
  parseConversationLog,
  sanitizeTrace,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const wrap = entries => JSON.stringify({ entries, saved_at: 1 });
const RUN = "run_0123456789abcdef0123456789abcdef";

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

test("interrupted replies may be empty and keep their note; other empty entries still drop", () => {
  assert.deepEqual(
    parseConversationLog(wrap([
      { role: "user", text: "问题" },
      { role: "assistant", text: "", interrupted: true, note: "回复生成中断：接口超时" },
      { role: "assistant", text: "半截", interrupted: true, reasoning: "想到一半", note: 42 },
      { role: "assistant", text: "", interrupted: "yes" },
      { role: "user", text: "", interrupted: true },
      { role: "assistant", text: "完整回复", note: "非中断条目不带 note" },
    ])),
    [
      { role: "user", text: "问题" },
      { role: "assistant", text: "", interrupted: true, note: "回复生成中断：接口超时" },
      { role: "assistant", text: "半截", reasoning: "想到一半", interrupted: true },
      { role: "assistant", text: "完整回复" },
    ],
  );
});

test("trace rows are capped at 8 and junk rows dropped (shared with the server-side PATCH payload)", () => {
  const rows = sanitizeTrace([
    { icon: "paperclip", title: "已解析并注入附件", suffix: " ×2", detail: "a.pdf\nb.csv", elapsed: "1.2s" },
    ...Array.from({ length: 10 }, (_, index) => ({ icon: "check-circle", title: `多余行 ${index}` })),
  ]);
  assert.equal(rows.length, 8, "最多 8 行");
  assert.deepEqual(rows[0], { icon: "paperclip", title: "已解析并注入附件", suffix: " ×2", detail: "a.pdf\nb.csv", elapsed: "1.2s" });
  assert.ok(rows.slice(1).every(row => row.title.startsWith("多余行")));
  assert.deepEqual(
    sanitizeTrace([{ icon: "gauge", title: "" }, { title: "缺 icon" }, "junk", { icon: "check-circle", title: "有效", suffix: 3 }]),
    [{ icon: "check-circle", title: "有效" }],
  );
  assert.deepEqual(sanitizeTrace("not-a-list"), []);
});

test("clearing a scope or all logs also drops legacy pending records", () => {
  store.clear();
  store.set(`openmathmodel.chatLog.v1.${RUN}`, wrap([{ role: "user", text: "旧记录" }]));
  store.set(`openmathmodel.chatPending.v1.${RUN}`, "{}");
  assert.equal(loadConversationLog(RUN).length, 1);
  clearConversationLog(RUN);
  assert.equal(store.size, 0);
  store.set(`openmathmodel.chatLog.v1.${RUN}`, wrap([{ role: "user", text: "旧记录" }]));
  store.set("openmathmodel.chatPending.v1.chat_0123456789abcdef0123456789abcdef", "{}");
  store.set("unrelated.key", "keep");
  clearAllConversationLogs();
  assert.deepEqual([...store.keys()], ["unrelated.key"]);
});
