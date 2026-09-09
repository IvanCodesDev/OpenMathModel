import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./reply-actions.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});

// 相对说明符在 data: URL 里没有基地址可解析：三个运行时依赖都用最小桩内联。
// 被测的是标记生成与评价状态机，不碰剪贴板、i18n 词典与网络。
const stub = code => `data:text/javascript;charset=utf-8,${encodeURIComponent(code)}`;
const moduleCode = outputText
  .replace('"../diagnostics/system-diagnostics"', JSON.stringify(stub("export const copyTextToClipboard = async () => true;")))
  .replace('"../i18n/locale"', JSON.stringify(stub("export const t = text => text;")))
  .replace('"./chat-turns-api"', JSON.stringify(stub("export const setChatTurnFeedback = async () => ({});")));
const { createFeedbackController, nextFeedback, replyActionsMarkup } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(moduleCode)}`
);

test("再点已按下的那个撤回，点另一个改评价", () => {
  assert.equal(nextFeedback(null, "up"), "up");
  assert.equal(nextFeedback("up", "up"), null);
  assert.equal(nextFeedback("up", "down"), "down");
  assert.equal(nextFeedback("down", "down"), null);
});

test("没有服务端轮 id 只有复制；有轮 id 才有赞 / 踩，已有评价的那个按下且实心", () => {
  const copyOnly = replyActionsMarkup({ turnId: null });
  assert.match(copyOnly, /data-reply-copy/);
  assert.doesNotMatch(copyOnly, /data-reply-feedback/);

  const fresh = replyActionsMarkup({ turnId: "turn_1", feedback: null });
  assert.match(fresh, /data-reply-copy/);
  assert.match(fresh, /data-reply-feedback="up" aria-pressed="false"[^>]*><i class="ph ph-thumbs-up"/);
  assert.match(fresh, /data-reply-feedback="down" aria-pressed="false"[^>]*><i class="ph ph-thumbs-down"/);

  const liked = replyActionsMarkup({ turnId: "turn_1", feedback: "up" });
  assert.match(liked, /data-reply-feedback="up" aria-pressed="true"[^>]*><i class="ph-fill ph-thumbs-up"/);
  assert.match(liked, /data-reply-feedback="down" aria-pressed="false"/);
  // 无障碍：按钮有可读标签，图标本身对读屏隐藏
  assert.match(liked, /aria-label="赞"/);
  assert.match(liked, /aria-label="踩"/);
  assert.match(liked, /aria-label="复制回复"/);
});

test("点击先按下再落库，以服务端确认值为准", async () => {
  const painted = [];
  const saved = [];
  const controller = createFeedbackController({
    initial: null,
    save: async next => { saved.push(next); return next; },
    paint: value => painted.push(value),
    onError: () => assert.fail("不应报错"),
  });
  assert.deepEqual(painted, [null], "挂载即按初始值画一次");

  await controller.click("up");
  assert.deepEqual(saved, ["up"]);
  assert.equal(controller.current, "up");
  await controller.click("up");
  assert.deepEqual(saved, ["up", null], "再点同一个 = 撤回，落 null");
  assert.equal(controller.current, null);
  await controller.click("down");
  assert.equal(controller.current, "down");
  assert.deepEqual(painted, [null, "up", "up", null, null, "down", "down"], "每次点击：先乐观画一次，落库后再按确认值画一次");
});

test("落库失败退回原状并提示；落库期间的点击忽略", async () => {
  const painted = [];
  let errors = 0;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const controller = createFeedbackController({
    initial: "up",
    save: async () => { await gate; throw new Error("网络断了"); },
    paint: value => painted.push(value),
    onError: () => { errors += 1; },
  });

  const pending = controller.click("down");
  assert.equal(controller.current, "down", "点下去立刻按下");
  await controller.click("up");
  assert.equal(controller.current, "down", "上一次还没落库，这次点击忽略");
  release();
  await pending;
  assert.equal(controller.current, "up", "失败后退回原来的赞");
  assert.equal(errors, 1);
  assert.deepEqual(painted, ["up", "down", "up"]);

  // 恢复后可以继续操作
  await controller.click("down");
  assert.equal(controller.current, "up", "save 仍然抛错 → 仍退回");
  assert.equal(errors, 2);
});
