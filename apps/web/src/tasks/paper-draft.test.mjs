import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { beforeEach, test } from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-draft.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const store = new Map();
let storageBroken = false;
globalThis.localStorage = {
  getItem: key => {
    if (storageBroken) throw new Error("storage disabled");
    return store.has(key) ? store.get(key) : null;
  },
  setItem: (key, value) => {
    if (storageBroken) throw new Error("storage disabled");
    store.set(key, String(value));
  },
  removeItem: key => {
    if (storageBroken) throw new Error("storage disabled");
    store.delete(key);
  },
};
globalThis.window = { location: { href: "" } };

const { clearPaperDraft, demoteLeakedPaperDraft, readUserPaperDraft, writeUserPaperDraft } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

const PROJECT_A = `proj_${"a".repeat(32)}`;
const PROJECT_B = `proj_${"b".repeat(32)}`;
const LEGACY_SHARED_KEY = "openmathmodelPaperDraft.v1";
const LEAKED = "<h2>2 问题分析</h2><p>潜水器采用无缆部署……</p>";

function openProject(projectId) {
  const url = new URL("http://localhost/workspace/paper-editor");
  if (projectId) url.searchParams.set("project_id", projectId);
  globalThis.window.location.href = url.href;
}

function record(projectId) {
  return JSON.parse(store.get(`openmathmodel.paperDraft.v1.${projectId}`));
}

beforeEach(() => {
  store.clear();
  storageBroken = false;
  openProject(PROJECT_A);
});

test("a draft written in one project is only read back in that project", () => {
  assert.equal(writeUserPaperDraft("<p>A 的草稿</p>"), true);
  assert.equal(readUserPaperDraft()?.html, "<p>A 的草稿</p>");
  assert.equal(typeof readUserPaperDraft()?.savedAt, "number");

  openProject(PROJECT_B);
  assert.equal(readUserPaperDraft(), null);

  openProject("../evil");
  assert.equal(readUserPaperDraft(), null);
  writeUserPaperDraft("<p>没有项目</p>");
  assert.ok(store.has("openmathmodel.paperDraft.v1.demo"));
});

test("the retired shared key is never restored as a draft", () => {
  store.set(LEGACY_SHARED_KEY, LEAKED);
  assert.equal(readUserPaperDraft(), null);
  openProject(PROJECT_B);
  assert.equal(readUserPaperDraft(), null);
  writeUserPaperDraft("<p>新草稿</p>");
  assert.equal(store.get(LEGACY_SHARED_KEY), LEAKED, "saving must not touch the shared key");
});

test("records that are not user edits are ignored", () => {
  const key = `openmathmodel.paperDraft.v1.${PROJECT_A}`;
  store.set(key, JSON.stringify({ html: "<p>模板快照</p>", saved_at: 1 }));
  assert.equal(readUserPaperDraft(), null);
  store.set(key, JSON.stringify({ html: "   ", saved_at: 1, user_edited: true }));
  assert.equal(readUserPaperDraft(), null);
  store.set(key, "{");
  assert.equal(readUserPaperDraft(), null);
  store.set(key, "null");
  assert.equal(readUserPaperDraft(), null);
  store.set(key, JSON.stringify({ html: "<p>ok</p>", saved_at: "yesterday", user_edited: true }));
  assert.deepEqual(readUserPaperDraft(), { html: "<p>ok</p>", savedAt: null });
});

test("clearing discards this project's draft and the shared key, not other projects", () => {
  writeUserPaperDraft("<p>A</p>");
  openProject(PROJECT_B);
  writeUserPaperDraft("<p>B</p>");
  store.set(LEGACY_SHARED_KEY, LEAKED);

  openProject(PROJECT_A);
  clearPaperDraft();
  assert.equal(readUserPaperDraft(), null);
  assert.equal(store.has(LEGACY_SHARED_KEY), false);
  openProject(PROJECT_B);
  assert.equal(readUserPaperDraft()?.html, "<p>B</p>");
});

test("a copy identical to the shared key is demoted but its text is kept", () => {
  store.set(LEGACY_SHARED_KEY, LEAKED);
  writeUserPaperDraft(LEAKED);
  const before = record(PROJECT_A);

  assert.equal(demoteLeakedPaperDraft(), true);
  assert.equal(readUserPaperDraft(), null);
  assert.deepEqual(record(PROJECT_A), { ...before, user_edited: false });
  assert.equal(store.get(LEGACY_SHARED_KEY), LEAKED);
  assert.equal(demoteLeakedPaperDraft(), false, "already demoted");
});

test("the user's own drafts are never demoted", () => {
  writeUserPaperDraft("<p>我自己写的</p>");
  assert.equal(demoteLeakedPaperDraft(), false, "no shared key at all");

  store.set(LEGACY_SHARED_KEY, LEAKED);
  assert.equal(demoteLeakedPaperDraft(), false, "different text");
  writeUserPaperDraft(`${LEAKED}<p>又补了一段</p>`);
  assert.equal(demoteLeakedPaperDraft(), false, "edited copy");
  assert.equal(readUserPaperDraft()?.html, `${LEAKED}<p>又补了一段</p>`);

  openProject(PROJECT_B);
  assert.equal(demoteLeakedPaperDraft(), false, "no record");
});

test("disabled storage degrades to no draft instead of throwing", () => {
  storageBroken = true;
  assert.equal(writeUserPaperDraft("<p>x</p>"), false);
  assert.equal(readUserPaperDraft(), null);
  assert.equal(demoteLeakedPaperDraft(), false);
  assert.doesNotThrow(() => clearPaperDraft());
});
