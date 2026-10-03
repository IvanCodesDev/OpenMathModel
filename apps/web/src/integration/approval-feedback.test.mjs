import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./approval-feedback.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { describeApprovalFeedback } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

const APPROVAL_ID = "appr_22222222222222222222222222222222";

function gate(description) {
  return {
    id: APPROVAL_ID,
    title: "稳健性检查 6 项中 2 项未通过。请确认实验结果的处置方式",
    description,
    options: [{ id: "accept_with_limits", label: "接受并记录局限" }],
  };
}

test("首轮闸门与没有待批的门不给反馈块", () => {
  assert.equal(describeApprovalFeedback(null), null);
  assert.equal(describeApprovalFeedback(undefined), null);
  assert.equal(describeApprovalFeedback(gate(null)), null);
  assert.equal(describeApprovalFeedback(gate("   \n  ")), null);
});

test("第一行是来由，「- 」开头的行逐条成列表（服务端原文，不改字）", () => {
  // 正文照抄 backend/api/tests/test_approval_feedback.py 的 G3 自动回退样例（截两条）
  const text = [
    "本轮是从「实验运行」起的回退重做（图按条件边自动回退，第 1 轮）",
    "- 回退原因：稳健性检查 6 项中 5 项未通过。请确认实验结果的处置方式",
    "- 作废的上一轮产出：实验运行、结果验证",
  ].join("\n");
  const feedback = describeApprovalFeedback(gate(text));
  assert.deepEqual(feedback, {
    approvalId: APPROVAL_ID,
    lead: "本轮是从「实验运行」起的回退重做（图按条件边自动回退，第 1 轮）",
    items: ["回退原因：稳健性检查 6 项中 5 项未通过。请确认实验结果的处置方式", "作废的上一轮产出：实验运行、结果验证"],
    stamp: `${APPROVAL_ID}:${text}`,
  });
});

test("CRLF、空行、按整行截断留下的省略行都不打乱结构", () => {
  const feedback = describeApprovalFeedback(gate("来由\r\n\r\n- 甲\r\n-   \r\n- 乙\n…"));
  assert.equal(feedback.lead, "来由 …");
  assert.deepEqual(feedback.items, ["甲", "乙"]);
});

test("只有逐条事实、没有来由时照样成块；同一份正文的戳不变，正文一变戳就变", () => {
  const first = describeApprovalFeedback(gate("- 作废的上一轮产出：论文撰写"));
  assert.equal(first.lead, "");
  assert.deepEqual(first.items, ["作废的上一轮产出：论文撰写"]);
  assert.equal(describeApprovalFeedback(gate("- 作废的上一轮产出：论文撰写")).stamp, first.stamp);
  assert.notEqual(describeApprovalFeedback(gate("- 作废的上一轮产出：结果验证")).stamp, first.stamp);
});
