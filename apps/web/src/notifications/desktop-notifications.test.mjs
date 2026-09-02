import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./desktop-notifications.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});

// 相对说明符在 data: URL 里没有基地址可解析：两个运行时依赖都用最小桩内联。
// 被测的 runStatusNotificationTag 是纯函数，不碰 i18n 与偏好设置。
const stub = code => `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`;
const localeStub = stub("export const t = text => text;");
const privacyStub = stub(
  "export const notifySecurityEnabled = () => true; export const notifyTaskDoneEnabled = () => true;",
);
const moduleCode = outputText
  .replace('"../i18n/locale"', JSON.stringify(localeStub))
  .replace('"../preferences/privacy-preferences"', JSON.stringify(privacyStub));
const { runStatusNotificationTag } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(moduleCode)}`
);

const RUN = "run_0123456789abcdef0123456789abcdef";

test("同一运行两道审批门（G2 数据闸门 → G1 方案门）各自成一条提醒", () => {
  const g2 = runStatusNotificationTag(RUN, "WAITING_APPROVAL", { approvalId: "appr_g2", eventSequence: 18 });
  const g1 = runStatusNotificationTag(RUN, "WAITING_APPROVAL", { approvalId: "appr_g1", eventSequence: 41 });
  assert.notEqual(g2, g1);
  assert.ok(g2.startsWith(`omm-run-${RUN}-WAITING_APPROVAL`));
  assert.ok(g1.startsWith(`omm-run-${RUN}-WAITING_APPROVAL`));
});

test("修订回合第 2 轮完成不与第 1 轮完成同 tag（ADR-0013 第 17 项）", () => {
  const first = runStatusNotificationTag(RUN, "COMPLETED", { approvalId: null, eventSequence: 120 });
  const second = runStatusNotificationTag(RUN, "COMPLETED", { approvalId: null, eventSequence: 233 });
  assert.notEqual(first, second);
});

test("失败 → 重试 → 再失败，两次失败各自提醒", () => {
  assert.notEqual(
    runStatusNotificationTag(RUN, "FAILED", { eventSequence: 60 }),
    runStatusNotificationTag(RUN, "FAILED", { eventSequence: 75 }),
  );
});

test("同一次进入的重复快照 tag 相同：去重语义保留", () => {
  const a = runStatusNotificationTag(RUN, "WAITING_APPROVAL", { approvalId: "appr_x", eventSequence: 18 });
  // 等待期间 SSE 可能带来新的 run.log 事件，但审批 id 不变 → 仍是同一道门
  const b = runStatusNotificationTag(RUN, "WAITING_APPROVAL", { approvalId: "appr_x", eventSequence: 19 });
  assert.equal(a, b);
  assert.equal(
    runStatusNotificationTag(RUN, "COMPLETED", { eventSequence: 120 }),
    runStatusNotificationTag(RUN, "COMPLETED", { eventSequence: 120 }),
  );
});

test("等待确认缺审批 id 时退回事件序号；两者都缺时退回旧格式而不是拼出 null", () => {
  assert.equal(
    runStatusNotificationTag(RUN, "WAITING_APPROVAL", { approvalId: null, eventSequence: 7 }),
    `omm-run-${RUN}-WAITING_APPROVAL-7`,
  );
  assert.equal(runStatusNotificationTag(RUN, "COMPLETED"), `omm-run-${RUN}-COMPLETED`);
  assert.equal(
    runStatusNotificationTag(RUN, "COMPLETED", { approvalId: null, eventSequence: null }),
    `omm-run-${RUN}-COMPLETED`,
  );
});

test("非等待确认状态不拿审批 id 当标识（审批 id 属于门，不属于完成）", () => {
  assert.equal(
    runStatusNotificationTag(RUN, "COMPLETED", { approvalId: "appr_stale", eventSequence: 9 }),
    `omm-run-${RUN}-COMPLETED-9`,
  );
});
