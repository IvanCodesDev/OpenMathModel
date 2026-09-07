import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./run-control-view.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  CONFIRM_REPLY_TEXT,
  DISMISSED_TITLE,
  PROPOSED_TITLE,
  REJECTED_TITLE,
  actionKindLabel,
  actionTraceRow,
  actionTraceTitle,
  isExecutedAction,
  lastPendingProposal,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const dictionarySource = await readFile(new URL("../i18n/en-US.ts", import.meta.url), "utf8");

test("已生效的动作：静态标题 + 阶段 / 选项名进后缀 + 服务端说明进详情", () => {
  const retry = actionTraceRow({
    kind: "retry",
    status: "executed",
    stage: "EXPERIMENTING",
    stage_label: "实验运行",
    note_id: "note_1",
    message: "已重试「实验运行」阶段，并把你的要求作为备注注入该阶段的执行提示词",
  });
  assert.deepEqual(retry, {
    icon: "check-circle",
    title: "已重试阶段",
    suffix: " · 实验运行",
    detail: "已重试「实验运行」阶段，并把你的要求作为备注注入该阶段的执行提示词",
  });

  const approve = actionTraceRow({
    kind: "approve",
    status: "executed",
    approval_id: "appr_1",
    approval_title: "确认建模方案",
    option_id: "adopt:plan_b",
    option_label: "改用备选：方案 B（启发式搜索）",
    message: "已在「确认建模方案」中选择「改用备选：方案 B（启发式搜索）」",
  });
  assert.equal(approve.title, "已选定审批选项");
  assert.equal(approve.suffix, " · 改用备选：方案 B（启发式搜索）");

  const revision = actionTraceRow({
    kind: "revision",
    status: "executed",
    round: 2,
    stage: "DATA_PREPARATION",
    stage_label: "数据准备",
    approval_id: "appr_2",
    note_id: "note_2",
    message: "已受理第 2 轮修改要求",
  });
  assert.equal(revision.title, "已受理修改要求");
  assert.equal(revision.suffix, " · 第 2 轮 · 数据准备");

  const redo = actionTraceRow({
    kind: "redo",
    status: "executed",
    stage: "MODEL_PLANNING",
    stage_label: "建模方案",
    note_id: "note_3",
    message: "已从「建模方案」重做，该阶段及其之后的阶段会整段重跑",
  });
  assert.equal(redo.title, "已从阶段重做");
  assert.equal(redo.suffix, " · 建模方案");
  assert.equal(redo.icon, "check-circle");

  assert.equal(actionTraceRow({ kind: "cancel", status: "executed", message: "已取消任务" }).suffix, "");
  assert.equal(actionTraceRow({ kind: "resume", status: "executed" }).suffix, "", "没有阶段名就不留孤零零的分隔符");
  assert.equal(actionTraceRow({ kind: "unknown_kind", status: "executed" }).title, "已执行运行操作");
  assert.equal("detail" in actionTraceRow({ kind: "pause", status: "executed", message: "   " }), false);
});

test("重做提案（ADR-0019）：后缀带回到的阶段，确认提示并入详情，放弃后按放弃计", () => {
  const proposal = {
    kind: "redo",
    status: "proposed",
    stage: "MODEL_PLANNING",
    stage_label: "建模方案",
    text: "换成随机森林重新做",
    message: "要从「建模方案」重做吗？该阶段及其之后的阶段会整段重跑并重新计费",
    confirm_hint: "回复「确认」或点下方按钮执行；回复其它内容则不重做",
  };
  const proposed = actionTraceRow(proposal);
  assert.equal(proposed.title, PROPOSED_TITLE);
  assert.equal(proposed.icon, "question");
  assert.equal(proposed.suffix, " · 从阶段重做 · 建模方案");
  assert.equal(
    proposed.detail,
    "要从「建模方案」重做吗？该阶段及其之后的阶段会整段重跑并重新计费\n回复「确认」或点下方按钮执行；回复其它内容则不重做",
  );
  assert.equal(actionTraceRow({ kind: "redo", status: "proposed" }).suffix, " · 从阶段重做", "没有阶段名就只留类别");
  assert.deepEqual(lastPendingProposal([proposal]), proposal, "重做提案要等下一句确认");
  assert.equal(isExecutedAction(proposal), false);

  const dismissed = actionTraceRow({ kind: "redo", status: "dismissed", stage_label: "建模方案", message: "已放弃从「建模方案」重做，运行状态不变。" });
  assert.equal(dismissed.title, DISMISSED_TITLE);
  assert.equal(dismissed.suffix, " · 从阶段重做 · 建模方案");
});

test("提案 / 拒绝 / 放弃：按状态取标题，类别进后缀，确认提示并入详情", () => {
  const proposed = actionTraceRow({
    kind: "cancel",
    status: "proposed",
    message: "要取消这个任务吗？取消后运行立即结束、不可恢复。",
    confirm_hint: "回复「确认」或点下方按钮执行",
  });
  assert.equal(proposed.title, PROPOSED_TITLE);
  assert.equal(proposed.icon, "question");
  assert.equal(proposed.suffix, " · 取消任务");
  assert.equal(proposed.detail, "要取消这个任务吗？取消后运行立即结束、不可恢复。\n回复「确认」或点下方按钮执行");

  const rejected = actionTraceRow({ kind: "retry", status: "rejected", code: "INVALID_ACTION", message: "当前状态不允许重试" });
  assert.equal(rejected.title, REJECTED_TITLE);
  assert.equal(rejected.icon, "warning-circle");
  assert.equal(rejected.suffix, " · 重试阶段");

  const dismissed = actionTraceRow({ kind: "cancel", status: "dismissed", message: "已放弃取消任务，运行状态不变。" });
  assert.equal(dismissed.title, DISMISSED_TITLE);
  assert.equal(dismissed.icon, "x-circle");
  assert.equal(actionKindLabel("approve"), "选定审批选项");
  assert.equal(actionKindLabel("mystery"), "mystery");
});

test("只有 executed 才算改变了运行；待确认提案只看最后一条", () => {
  assert.equal(isExecutedAction({ kind: "retry", status: "executed" }), true);
  assert.equal(isExecutedAction({ kind: "cancel", status: "proposed" }), false);
  assert.equal(isExecutedAction({ kind: "retry", status: "rejected" }), false);

  const proposal = { kind: "cancel", status: "proposed", message: "要取消吗" };
  assert.deepEqual(lastPendingProposal([{ kind: "retry", status: "executed" }, proposal]), proposal);
  assert.equal(lastPendingProposal([proposal, { kind: "cancel", status: "dismissed" }]), null);
  assert.equal(lastPendingProposal([]), null);
  assert.equal(lastPendingProposal(undefined), null);
  assert.equal(lastPendingProposal(null), null);
  assert.equal(CONFIRM_REPLY_TEXT, "确认");
});

test("全部轨迹行标题都是静态短语且有英文词条（语言切换按整句翻译）", () => {
  const titles = new Set([
    PROPOSED_TITLE,
    REJECTED_TITLE,
    DISMISSED_TITLE,
    ...["retry", "resume", "pause", "cancel", "approve", "reject", "revision", "redo", "other"].map(kind =>
      actionTraceTitle({ kind, status: "executed" }),
    ),
  ]);
  for (const title of titles) {
    assert.doesNotMatch(title, /[0-9A-Za-z「」·]/, `标题不得带动态内容：${title}`);
    assert.ok(dictionarySource.includes(`"${title}":`), `en-US 缺少词条：${title}`);
  }
});
