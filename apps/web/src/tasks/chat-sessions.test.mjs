import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const compilerOptions = { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 };

async function transpile(fileName) {
  const source = await readFile(new URL(`./${fileName}`, import.meta.url), "utf8");
  return ts.transpileModule(source, { compilerOptions }).outputText;
}

const dataUrl = code => `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`;

// 相对说明符在 data: URL 里没有基地址可解析：把依赖也转译成 data: URL 内联进去。
const logUrl = dataUrl(await transpile("conversation-log.ts"));
const moduleCode = (await transpile("chat-sessions.ts")).replace(
  '"./conversation-log"',
  JSON.stringify(logUrl),
);
const { deriveChatTitle, parseChatSessions } = await import(dataUrl(moduleCode));

const wrap = sessions => JSON.stringify({ sessions, saved_at: 1 });
const id = suffix => `chat_${suffix.repeat(32).slice(0, 32)}`;

test("derives a compact title from the first clause of the opening message", () => {
  assert.equal(deriveChatTitle("蒙特卡洛怎么用？后面再说别的"), "蒙特卡洛怎么用");
  assert.equal(deriveChatTitle("  多余   空白   会被压掉  "), "多余 空白 会被压掉");
  assert.equal(deriveChatTitle(""), "新对话");
});

test("truncates long titles on character count, not byte length", () => {
  const title = deriveChatTitle("这是一段很长的开场白".repeat(5));
  assert.equal(Array.from(title).length, 25);
  assert.ok(title.endsWith("…"));
});

test("accepts well-formed sessions and fills the missing update time", () => {
  assert.deepEqual(
    parseChatSessions(wrap([
      { id: id("a"), owner: "usr_1", title: "灵敏度分析", created_at: 10, updated_at: 20, archived_at: 0 },
      { id: id("b"), owner: "usr_1", title: "归档过的一段", created_at: 30, archived_at: 40 },
    ])),
    [
      { id: id("a"), owner: "usr_1", title: "灵敏度分析", created_at: 10, updated_at: 20, archived_at: 0 },
      { id: id("b"), owner: "usr_1", title: "归档过的一段", created_at: 30, updated_at: 30, archived_at: 40 },
    ],
  );
});

test("drops entries that could point at the wrong conversation or the wrong user", () => {
  assert.deepEqual(
    parseChatSessions(wrap([
      { id: "run_" + "a".repeat(32), owner: "usr_1", title: "任务运行不属于对话目录" },
      { id: "chat_not-hex", owner: "usr_1", title: "id 不合法" },
      { id: id("c"), title: "缺 owner" },
      { id: id("c"), owner: "", title: "owner 为空" },
      "not-an-object",
      { id: id("d"), owner: "usr_2", title: "有效", created_at: 5 },
    ])),
    [{ id: id("d"), owner: "usr_2", title: "有效", created_at: 5, updated_at: 5, archived_at: 0 }],
  );
});

test("keeps the first entry when the same id appears twice", () => {
  const sessions = parseChatSessions(wrap([
    { id: id("e"), owner: "usr_1", title: "先登记的", created_at: 1 },
    { id: id("e"), owner: "usr_1", title: "重复的", created_at: 2 },
  ]));
  assert.deepEqual(sessions.map(session => session.title), ["先登记的"]);
});

test("rejects missing or unparsable storage values", () => {
  assert.deepEqual(parseChatSessions(null), []);
  assert.deepEqual(parseChatSessions(""), []);
  assert.deepEqual(parseChatSessions("{"), []);
  assert.deepEqual(parseChatSessions(JSON.stringify({ sessions: "oops" })), []);
});
