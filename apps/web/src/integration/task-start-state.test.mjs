import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./task-start-state.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  buildRunningUrl,
  deriveProjectName,
  normalizeTaskDescription,
  parseTaskDraft,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

test("normalizes line endings without collapsing task paragraphs", () => {
  assert.equal(normalizeTaskDescription("  第一行\r\n第二行\r  "), "第一行\n第二行");
});

test("derives a compact project name from the first clause", () => {
  assert.equal(
    deriveProjectName("请帮我分析城市共享单车需求。还要输出调度方案。"),
    "分析城市共享单车需求",
  );
  assert.equal(Array.from(deriveProjectName("甲".repeat(60))).length, 25);
});

test("parses a saved draft and rejects unsafe remote identifiers", () => {
  const draft = parseTaskDraft(JSON.stringify({
    version: 1,
    description: "建立预测模型",
    task_type: "数据分析",
    selected_model: "auto",
    project_id: "not-a-project",
    run_request_token: "short",
    attachments: [
      { name: "data.csv", size: 42, type: "text/csv", last_modified: 123 },
      { name: "", size: -1 },
    ],
  }));
  assert.ok(draft);
  assert.equal(draft.project_id, undefined);
  assert.equal(draft.run_request_token, undefined);
  assert.deepEqual(draft.attachments, [
    { name: "data.csv", size: 42, type: "text/csv", last_modified: 123 },
  ]);
  assert.equal(parseTaskDraft("{"), null);
});

test("builds the canonical run-aware workspace URL", () => {
  assert.equal(
    buildRunningUrl(`run_${"a".repeat(32)}`, `proj_${"b".repeat(32)}`),
    `/task/running?run_id=run_${"a".repeat(32)}&project_id=proj_${"b".repeat(32)}`,
  );
});
