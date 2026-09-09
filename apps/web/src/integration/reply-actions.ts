/**
 * 回复右下角操作区：复制原文 + 赞 / 踩（任务页与首页对话共用一套 DOM 与交互）。
 *
 * 复制的是回复的 Markdown 源码（便于粘进论文与笔记）。赞 / 踩落在服务端托管的这一轮
 * 对话上（PUT /api/chat/turns/{id}/feedback，ADR-0016 的轮视图多出 feedback 字段）：
 * 刷新、重进都按轮视图回显；再点同一个撤回，点另一个改评价。没有服务端轮 id 的回复
 * （本机旧记录、无状态通道）只有复制——评价没有落点就不摆出来骗人。
 *
 * 操作区紧跟正文（`.analysis-copy`）之后插入而不是追加到块尾：对话触发的运行动作
 * （ADR-0018）会让工作台在这条回复块内部续写执行步骤，按钮要贴着正文，不能掉到步骤
 * 区下面。
 *
 * 纯逻辑（标记生成、评价状态机）与 DOM 挂载分开：前者可在 Node 测试里直接验证。
 */

import { copyTextToClipboard } from "../diagnostics/system-diagnostics";
import { t } from "../i18n/locale";
import { setChatTurnFeedback, type ChatTurnFeedback } from "./chat-turns-api";

export interface ReplyActionsOptions {
  /** 复制按钮复制的原始回复文本。 */
  text: string;
  /** 服务端托管轮 id；缺省 / null 时不显示赞 / 踩。 */
  turnId?: string | null;
  /** 已有评价，回显按下态。 */
  feedback?: ChatTurnFeedback;
}

export type FeedbackChoice = Exclude<ChatTurnFeedback, null>;

const FEEDBACK_CHOICES: readonly FeedbackChoice[] = ["up", "down"];
const FEEDBACK_ICON: Record<FeedbackChoice, string> = { up: "thumbs-up", down: "thumbs-down" };
const FEEDBACK_LABEL: Record<FeedbackChoice, string> = { up: "赞", down: "踩" };

/** 点击后的下一状态：再点已按下的那个 = 撤回；点另一个 = 改评价。 */
export function nextFeedback(current: ChatTurnFeedback, clicked: FeedbackChoice): ChatTurnFeedback {
  return current === clicked ? null : clicked;
}

function iconHtml(name: string, filled = false): string {
  return `<i class="${filled ? "ph-fill" : "ph"} ph-${name}" aria-hidden="true"></i>`;
}

function feedbackButtonHtml(choice: FeedbackChoice, pressed: boolean): string {
  const label = t(FEEDBACK_LABEL[choice]);
  return `<button type="button" class="reply-action-button" data-reply-feedback="${choice}" aria-pressed="${pressed}" title="${label}" aria-label="${label}">${iconHtml(FEEDBACK_ICON[choice], pressed)}</button>`;
}

/** 操作区内部标记：复制按钮总在；有轮 id 才有赞 / 踩，已有评价的那个按下。 */
export function replyActionsMarkup(options: Pick<ReplyActionsOptions, "turnId" | "feedback">): string {
  const copyLabel = t("复制回复");
  const copyButton = `<button type="button" class="reply-action-button" data-reply-copy title="${copyLabel}" aria-label="${copyLabel}">${iconHtml("copy")}</button>`;
  if (!options.turnId) return copyButton;
  const current = options.feedback ?? null;
  return copyButton + FEEDBACK_CHOICES.map(choice => feedbackButtonHtml(choice, current === choice)).join("");
}

export interface FeedbackController {
  readonly current: ChatTurnFeedback;
  /** 处理一次点击：先按下再落库，失败退回原状；落库期间的点击忽略。 */
  click(choice: FeedbackChoice): Promise<void>;
}

/**
 * 评价状态机（与 DOM 无关）：`save` 是落库调用，返回服务端确认后的值；`paint` 把
 * 状态画到按钮上；`onError` 在落库失败、已退回原状后提示用户。
 */
export function createFeedbackController(options: {
  initial: ChatTurnFeedback;
  save: (next: ChatTurnFeedback) => Promise<ChatTurnFeedback>;
  paint: (value: ChatTurnFeedback) => void;
  onError: () => void;
}): FeedbackController {
  let current: ChatTurnFeedback = options.initial;
  let saving = false;
  options.paint(current);
  return {
    get current() {
      return current;
    },
    async click(choice) {
      if (saving) return;
      const previous = current;
      current = nextFeedback(current, choice);
      options.paint(current);
      saving = true;
      try {
        current = await options.save(current);
        options.paint(current);
      } catch {
        current = previous;
        options.paint(previous);
        options.onError();
      } finally {
        saving = false;
      }
    },
  };
}

/** 与执行页同一形态的轻提示（样式见 styles.css `.toast`）。 */
function toast(message: string, duration = 1900): void {
  document.querySelector(".toast")?.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  window.setTimeout(() => node.remove(), duration);
}

function paintFeedback(actions: HTMLElement, value: ChatTurnFeedback): void {
  actions.querySelectorAll<HTMLButtonElement>("[data-reply-feedback]").forEach(button => {
    const choice = button.dataset.replyFeedback as FeedbackChoice;
    const pressed = value === choice;
    button.setAttribute("aria-pressed", pressed ? "true" : "false");
    button.innerHTML = iconHtml(FEEDBACK_ICON[choice], pressed);
  });
}

/**
 * 在回复块里挂操作区并接好交互；重复调用会替换已有的操作区（同一块只有一组按钮）。
 * 返回操作区节点。
 */
export function mountReplyActions(replyBlock: HTMLElement, options: ReplyActionsOptions): HTMLElement {
  replyBlock.querySelector(":scope > .reply-actions")?.remove();
  const actions = document.createElement("div");
  actions.className = "reply-actions";
  actions.innerHTML = replyActionsMarkup(options);
  const copy = replyBlock.querySelector(".analysis-copy");
  if (copy) copy.insertAdjacentElement("afterend", actions);
  else replyBlock.appendChild(actions);

  const copyButton = actions.querySelector<HTMLButtonElement>("[data-reply-copy]")!;
  let resetTimer: number | undefined;
  copyButton.addEventListener("click", async () => {
    const copied = await copyTextToClipboard(options.text);
    copyButton.innerHTML = iconHtml(copied ? "check" : "copy");
    toast(t(copied ? "回复已复制" : "复制失败，请手动选择文本"));
    window.clearTimeout(resetTimer);
    resetTimer = window.setTimeout(() => { copyButton.innerHTML = iconHtml("copy"); }, 1400);
  });

  const turnId = options.turnId ?? null;
  if (!turnId) return actions;
  const controller = createFeedbackController({
    initial: options.feedback ?? null,
    save: async next => (await setChatTurnFeedback(turnId, next)).feedback ?? null,
    paint: value => paintFeedback(actions, value),
    onError: () => toast(t("反馈保存失败，请稍后再试")),
  });
  actions.querySelectorAll<HTMLButtonElement>("[data-reply-feedback]").forEach(button => {
    button.addEventListener("click", () => {
      void controller.click(button.dataset.replyFeedback as FeedbackChoice);
    });
  });
  return actions;
}
