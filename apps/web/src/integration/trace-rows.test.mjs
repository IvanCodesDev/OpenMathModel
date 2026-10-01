import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./trace-rows.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  activityLabel,
  describeToolCall,
  paperProgressText,
  paperRewriteTitle,
  isQuietTool,
  processGroupTitle,
  reprField,
  reprList,
  reviewText,
  subagentGroupTitle,
  subagentOutcome,
  thinkingStage,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

test("reprField reads string values out of a Python repr summary", () => {
  assert.equal(reprField("{'path': 'data/clean.csv'}", "path"), "data/clean.csv");
  assert.equal(reprField(`{'query': "it's fine", 'limit': 6}`, "query"), "it's fine");
  assert.equal(reprField("{'text': 'a\\nb\\t\\'c\\''}", "text"), "a\nb\t'c'");
  assert.equal(reprField("{'text': 'import os...(+120 chars)", "text"), "import os…", "截断的摘要读到末尾并标省略");
  assert.equal(reprField("{'code': 'x'}", "path"), null);
});

test("reprList reports whether the list was cut off by the summary limit", () => {
  assert.deepEqual(reprList("{'files': ['data/a.csv', 'experiment.py']}", "files"), {
    items: ["data/a.csv", "experiment.py"],
    truncated: false,
  });
  assert.deepEqual(reprList("{'files': []}", "files"), { items: [], truncated: false });
  assert.deepEqual(reprList("{'files': ['a.csv', 'b.csv', 'c.cs...(+90 chars)", "files"), {
    items: ["a.csv", "b.csv"],
    truncated: true,
  });
});

test("workspace tools read like sentences instead of tool names", () => {
  const read = describeToolCall({
    tool: "ws_read",
    status: "succeeded",
    duration_ms: 16,
    input_summary: "{'path': 'experiment.py'}",
    output_summary: "{'path': 'experiment.py', 'text': 'import os\\nprint(1)\\n', 'truncated': False, 'total_chars': 19}",
  });
  assert.equal(read.title, "读取 experiment.py");
  assert.equal(read.detail, "import os\nprint(1)\n");
  assert.equal(read.step, "read");
  assert.equal(read.elapsedMs, undefined, "不到 1 秒的读文件不显示用时");

  const list = describeToolCall({
    tool: "ws_list",
    status: "succeeded",
    duration_ms: 0,
    input_summary: "{}",
    output_summary: "{'files': ['data/a.csv', 'experiment.py', 'figures/acc.png']}",
  });
  assert.equal(list.title, "查看工作区文件（3 个）");
  assert.equal(list.detail, "data/a.csv\nexperiment.py\nfigures/acc.png");

  const cut = describeToolCall({
    tool: "ws_list",
    status: "succeeded",
    input_summary: "{'prefix': 'data/'}",
    output_summary: "{'files': ['data/a.csv', 'data/b.c...(+300 chars)",
  });
  assert.equal(cut.title, "查看 data/ 下的文件", "条目数不全时不报数字");

  const write = describeToolCall({
    tool: "ws_write",
    status: "failed",
    input_summary: "{}",
    output_summary: "'missing required argument: path'",
  });
  assert.equal(write.title, "写入工作区文件（失败）");
  assert.equal(write.detail, "missing required argument: path");
  assert.equal(write.icon, "warning-circle");
  assert.equal(write.failed, true);

  const missing = describeToolCall({
    tool: "ws_read",
    status: "failed",
    input_summary: "{'path': 'validation/checks.csv'}",
    output_summary: "'文件不存在：validation/checks.csv'",
  });
  assert.equal(missing.title, "读取 validation/checks.csv（失败）");
  assert.equal(missing.detail, "文件不存在：validation/checks.csv");
});

test("sandbox runs keep their wording and always show how long they took", () => {
  const ok = describeToolCall({ tool: "python_run", status: "succeeded", duration_ms: 593, input_summary: "{'code': 'x'}" });
  assert.equal(ok.title, "已在沙箱执行实验代码");
  assert.equal(ok.elapsedMs, 593);
  assert.equal(ok.step, "run");
  assert.equal(ok.mono, true);

  const crashed = describeToolCall({
    tool: "python_run",
    status: "failed",
    duration_ms: 157,
    failure_detail: "Traceback (most recent call last):\nValueError: bad shape",
  });
  assert.equal(crashed.title, "实验代码执行失败（准备修复重试）");
  assert.match(crashed.detail, /报错详情：\nTraceback/);

  const slow = describeToolCall({ tool: "python_run", status: "timeout", duration_ms: 120625 });
  assert.equal(slow.title, "实验代码运行超时（准备修复重试）");

  const r = describeToolCall({ tool: "code_run", status: "succeeded", duration_ms: 3000, input_summary: "{'code': 'x', 'language': 'r'}" });
  assert.equal(r.title, "已在沙箱执行 R 代码");
});

test("knowledge tools and probes are described, unknown tools fall back", () => {
  assert.equal(
    describeToolCall({ tool: "knowledge_search", status: "succeeded", input_summary: "{'query': '卷积神经网络 超参数', 'limit': 6}" }).title,
    "检索知识库：卷积神经网络 超参数",
  );
  assert.equal(
    describeToolCall({ tool: "knowledge_read", status: "succeeded", input_summary: "{'card_id': 'problem:cumcm-2021-c'}" }).title,
    "查阅知识卡片 problem:cumcm-2021-c",
  );
  assert.equal(describeToolCall({ tool: "env_probe", status: "succeeded", duration_ms: 1953 }).elapsedMs, 1953);
  assert.equal(describeToolCall({ tool: "brand_new_tool", status: "succeeded" }), null);
});

test("subagent groups are titled by role and task", () => {
  assert.equal(
    subagentGroupTitle([{ kind: "reviewer", goal: "独立核查实验代码与结果能否作为后续检验与论文的依据" }]),
    "审稿子代理：独立核查实验代码与结果能否作为后续检验与论文的依据",
  );
  assert.equal(
    subagentGroupTitle([
      { kind: "proposer:mechanism", goal: "从「机理建模」视角提出一套可执行的建模方案" },
      { kind: "proposer:data_driven", goal: "从「数据驱动」视角提出一套可执行的建模方案" },
      { kind: "proposer:operations_research", goal: "从「运筹优化」视角提出一套可执行的建模方案" },
    ]),
    "方案提议人 ×3：机理建模、数据驱动、运筹优化",
  );
  assert.equal(subagentGroupTitle([{ kind: "sandbox", goal: "" }]), "沙盒子代理");
  assert.deepEqual(subagentOutcome("done"), { failed: false, label: "" });
  assert.deepEqual(subagentOutcome("exhausted"), { failed: true, label: "预算耗尽" });
});

test("reviewer verdicts become a sentence plus the reviewer's own summary", () => {
  assert.deepEqual(
    reviewText("robustness_review", {
      round: 1,
      verdict: "reject",
      blockers: 1,
      findings: 7,
      summary: "这组检查多数是真扰动，但 passed 与数值对不上。",
    }),
    {
      heading: "稳健性审稿（第 1 轮）：不通过，1 个阻断问题、7 条意见。",
      summary: "这组检查多数是真扰动，但 passed 与数值对不上。",
    },
  );
  assert.equal(reviewText("experiment_review", { round: 2, verdict: "accept", blockers: 0, findings: 0 }).heading, "实验审稿（第 2 轮）：通过。");
  assert.equal(reviewText("cleaning_review", { verdict: "static_reject", blockers: 2 }).heading, "清洗审稿：静态检查未通过，2 个阻断问题。");
  assert.equal(reviewText("experiment", {}), null);
});

test("paper progress events read as one sentence", () => {
  assert.equal(
    paperProgressText("paper_figures", { planned: 3, rendered: 2, missing: ["fig3"] }),
    "论文补图：规划 3 张，画出 2 张，1 张没画成。",
  );
  assert.equal(
    paperProgressText("paper_published", { chapters: 6, chars: 18000, audit_findings: 0 }),
    "论文草稿成稿：6 章，约 18000 字。",
  );
  assert.equal(paperProgressText("paper_section", {}), null);
  assert.equal(
    paperRewriteTitle({ index: 3, heading: "3 模型检验", before: 4, after: 1, adopted: true }),
    "定向回改第 3 章「3 模型检验」：待修问题 4 → 1",
  );
});

test("directory listings, knowledge lookups and environment probes stay out of the trace", () => {
  for (const tool of ["ws_list", "knowledge_search", "knowledge_read", "env_probe"]) {
    assert.equal(isQuietTool(tool), true, tool);
  }
  for (const tool of ["ws_read", "ws_write", "table_profile", "python_run", "code_run", ""]) {
    assert.equal(isQuietTool(tool), false, tool);
  }
});

test("process groups say what the agent was doing", () => {
  assert.equal(processGroupTitle(["", "experiment_code.sandbox"], ["probe", "think", "run"]), "编写并调试实验代码");
  assert.equal(processGroupTitle(["experiment_code.sandbox.r"], ["think"]), "编写并调试实验代码", "多语言模板后缀同口径");
  assert.equal(processGroupTitle([], ["list", "read", "probe"]), "查看工作区");
  assert.equal(processGroupTitle([], ["run", "list"]), "执行步骤");
  assert.equal(thinkingStage("validating.sandbox"), "稳健性检验");
  assert.equal(thinkingStage("unknown.prompt"), "");
  assert.equal(activityLabel("model_planning.proposer"), "提出候选方案");
});
