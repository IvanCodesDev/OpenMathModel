import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./approval-options.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  applyChosenOption,
  REJECT_OPTION_ID,
  shouldOfferOptions,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const APPROVAL_ID = "appr_11111111111111111111111111111111";

/** 修订门：六个「从 X 重做」+ 一个撤回，建议项是论文撰写（ADR-0013 §3）。 */
function revisionGate() {
  return {
    id: APPROVAL_ID,
    title: "选择重做起点",
    description: null,
    // id 与文案照抄 engine_glue 的修订门构造，改这里前先确认那边没变
    options: [
      { id: "redo:PROBLEM_ANALYSIS", label: "从「题意解析」重做" },
      { id: "redo:DATA_PREPARATION", label: "从「数据准备」重做" },
      { id: "redo:MODEL_PLANNING", label: "从「建模方案」重做" },
      { id: "redo:EXPERIMENTING", label: "从「实验运行」重做" },
      { id: "redo:VALIDATING", label: "从「结果验证」重做" },
      { id: "redo:PAPER_WRITING", label: "从「论文撰写」重做", recommended: true },
      {
        id: REJECT_OPTION_ID,
        label: "撤回本次修改要求",
        description: "保留现有结果，运行回到已完成状态",
      },
    ],
  };
}

/** 节点自提的闸门（G1 方案确认）：只有一个正向选项。 */
function nodeGate() {
  return {
    id: APPROVAL_ID,
    title: "确认建模方案",
    description: null,
    options: [
      { id: "approve", label: "采用推荐方案" },
      { id: REJECT_OPTION_ID, label: "退回重做" },
    ],
  };
}

function approveAction(optionId) {
  return {
    kind: "approve",
    label: "确认重做起点",
    target_route: "/task/running",
    approval_id: APPROVAL_ID,
    option_id: optionId,
  };
}

const NAVIGATE_ACTION = {
  kind: "navigate",
  label: "前往论文撰写",
  target_route: "/workspace/paper-editor",
  approval_id: null,
  option_id: null,
};

test("修订门摆出选项，单正向选项的节点闸门不摆", () => {
  assert.equal(shouldOfferOptions(revisionGate(), approveAction("redo:PAPER_WRITING")), true);
  assert.equal(shouldOfferOptions(nodeGate(), approveAction("approve")), false);
  assert.equal(shouldOfferOptions(null, approveAction("approve")), false);
});

test("跨屏时 CTA 是导航，不摆选项——选了也无处提交", () => {
  assert.equal(shouldOfferOptions(revisionGate(), NAVIGATE_ACTION), false);
});

test("手选项覆盖服务端预选，其余字段原样保留", () => {
  const action = approveAction("redo:PAPER_WRITING");
  const next = applyChosenOption(revisionGate(), action, {
    approvalId: APPROVAL_ID,
    optionId: "redo:PROBLEM_ANALYSIS",
  });
  assert.equal(next.option_id, "redo:PROBLEM_ANALYSIS");
  assert.equal(next.label, "确认重做起点");
  assert.equal(next.approval_id, APPROVAL_ID);
});

test("选中撤回时按钮改说撤回，不再顶着「确认重做起点」执行相反的事", () => {
  const next = applyChosenOption(revisionGate(), approveAction("redo:PAPER_WRITING"), {
    approvalId: APPROVAL_ID,
    optionId: REJECT_OPTION_ID,
  });
  assert.equal(next.option_id, REJECT_OPTION_ID);
  assert.equal(next.label, "撤回本次修改要求");
});

test("选择只对当道门生效：换一道门就回落服务端预选，不串门", () => {
  const action = approveAction("redo:PAPER_WRITING");
  const stale = applyChosenOption(revisionGate(), action, {
    approvalId: "appr_99999999999999999999999999999999",
    optionId: "redo:PROBLEM_ANALYSIS",
  });
  assert.equal(stale.option_id, "redo:PAPER_WRITING");
});

test("手选项不在本门选项里就忽略，不把野 id 提交给服务端", () => {
  const next = applyChosenOption(revisionGate(), approveAction("redo:PAPER_WRITING"), {
    approvalId: APPROVAL_ID,
    optionId: "redo:NOT_A_STAGE",
  });
  assert.equal(next.option_id, "redo:PAPER_WRITING");
});

test("服务端未预选（推荐不唯一）且用户未选时保持 null，由 CTA 自行禁用", () => {
  const action = approveAction(null);
  assert.equal(applyChosenOption(revisionGate(), action, null).option_id, null);
});

test("导航动作不被改写", () => {
  const next = applyChosenOption(revisionGate(), NAVIGATE_ACTION, {
    approvalId: APPROVAL_ID,
    optionId: "redo:MODEL_PLANNING",
  });
  assert.equal(next, NAVIGATE_ACTION);
});
