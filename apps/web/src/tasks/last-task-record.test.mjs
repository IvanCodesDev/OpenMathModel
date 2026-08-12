import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./last-task-record.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { parseLastTaskRecord } = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const RUN_ID = `run_${"a".repeat(32)}`;
const PROJECT_ID = `proj_${"b".repeat(32)}`;

test("accepts a well-formed record", () => {
  assert.deepEqual(
    parseLastTaskRecord(JSON.stringify({ run_id: RUN_ID, project_id: PROJECT_ID, saved_at: 1 })),
    { run_id: RUN_ID, project_id: PROJECT_ID },
  );
});

test("rejects malformed identifiers so startup never redirects to a bogus URL", () => {
  assert.equal(parseLastTaskRecord(JSON.stringify({ run_id: "run_short", project_id: PROJECT_ID })), null);
  assert.equal(parseLastTaskRecord(JSON.stringify({ run_id: RUN_ID, project_id: "../evil" })), null);
  assert.equal(parseLastTaskRecord(JSON.stringify({ run_id: 42, project_id: PROJECT_ID })), null);
});

test("rejects missing or unparsable storage values", () => {
  assert.equal(parseLastTaskRecord(null), null);
  assert.equal(parseLastTaskRecord(""), null);
  assert.equal(parseLastTaskRecord("{"), null);
  assert.equal(parseLastTaskRecord(JSON.stringify("run_only-a-string")), null);
});
