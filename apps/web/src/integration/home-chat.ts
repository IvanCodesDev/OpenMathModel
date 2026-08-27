/**
 * 首页轻量对话（对话优先接待的「聊天态」）。
 *
 * 发送先经接待判定（POST /task-intake）分流：完整题面走既有任务创建链路；
 * 闲聊或缺题面的输入进入本模块。视觉与执行页对话严格同构——复用全局的
 * user-bubble / assistant-block / assistant-id（水母 Logo）/ reply-thinking
 * （思考过程折叠块）/ analysis-copy（Markdown 正文）类与交互，不另造样式。
 * 对话历史由 agent-chat 维护（内存态、走用户配置的模型与 Auto 路由）。
 * 对话中一旦出现完整题面（接待判定升级），交回任务创建链路跳转执行页。
 *
 * DOM 由本模块动态插入（与执行计划面板同模式），不改动页面模板与路由。
 */

import { openAuthDialog } from "../auth/auth-dialog";
import { t } from "../i18n/locale";
import { renderMarkdown } from "../text/markdown";
import { typesetMath } from "../text/math-typeset";
import { ChatError, sendConversationTurn } from "./agent-chat";

const AGENT_ID_HTML =
  '<img class="project-logo assistant-logo" src="/assets/OpenMathModel_IP_Crop.png" alt="" aria-hidden="true"><span>Agent</span>';

function ensureThread(root: HTMLElement): HTMLElement | null {
  const existing = root.querySelector<HTMLElement>("[data-home-chat-thread]");
  if (existing?.isConnected) return existing;
  const composerArea = root.querySelector<HTMLElement>(".composer-area");
  if (!composerArea) return null;
  const thread = document.createElement("div");
  thread.className = "home-chat-thread";
  thread.dataset.homeChatThread = "true";
  composerArea.insertAdjacentElement("beforebegin", thread);
  return thread;
}

function appendUserBubble(thread: HTMLElement, text: string, referenceTitles: string[]): void {
  const message = document.createElement("div");
  message.className = "user-message";
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  bubble.textContent = text;
  // 与执行页同语义：引用内容只进请求正文，气泡下方以 @ 徽标如实标注引用了什么
  if (referenceTitles.length > 0) {
    const chips = document.createElement("div");
    chips.className = "user-attachment-chips";
    for (const title of referenceTitles) {
      const chip = document.createElement("span");
      chip.className = "user-attachment-chip";
      chip.innerHTML = '<i class="ph ph-at" aria-hidden="true"></i>';
      chip.append(title);
      chips.append(chip);
    }
    bubble.append(chips);
  }
  message.append(bubble);
  thread.append(message);
}

/** 与执行页同构的回复块：Agent 头 + （思考块占位）+ 「思考中…」扫光正文。 */
function appendAssistantBlock(thread: HTMLElement): HTMLElement {
  const block = document.createElement("div");
  block.className = "assistant-block follow-up-reply";
  block.innerHTML = `
    <div class="assistant-id">${AGENT_ID_HTML}</div>
    <div class="analysis-copy"><p class="thinking-plain"><span class="thinking-label thinking-shimmer">${t("思考中…")}</span></p></div>`;
  thread.append(block);
  return block;
}

/**
 * 思考过程折叠块：与执行页 createThinkingBlock 同构（同类名、同交互）。
 * 流式期间展开并跟随最新内容；回答开始后折叠为「已思考 N 秒」，可点击回看。
 */
function createThinkingBlock(replyBlock: HTMLElement): {
  append: (fullText: string) => void;
  finish: () => void;
} {
  const host = document.createElement("div");
  host.className = "reply-thinking";
  host.innerHTML = `
    <button type="button" class="thinking-header" aria-expanded="true" aria-label="${t("展开或收起思考过程")}">
      <span class="thinking-label thinking-shimmer">${t("思考中…")}</span>
      <i class="ph ph-caret-up thinking-chevron" aria-hidden="true"></i>
    </button>
    <div class="thinking-collapsible">
      <div class="thinking-inner">
        <div class="thinking-viewport"><div class="thinking-stream"></div></div>
      </div>
    </div>`;
  replyBlock.insertBefore(host, replyBlock.querySelector(".analysis-copy"));
  const header = host.querySelector<HTMLElement>(".thinking-header")!;
  const label = host.querySelector<HTMLElement>(".thinking-label")!;
  const collapsible = host.querySelector<HTMLElement>(".thinking-collapsible")!;
  const viewport = host.querySelector<HTMLElement>(".thinking-viewport")!;
  const stream = host.querySelector<HTMLElement>(".thinking-stream")!;
  const startedAt = Date.now();
  let done = false;
  let open = true;
  const applyOpen = (): void => {
    header.setAttribute("aria-expanded", String(open));
    collapsible.classList.toggle("is-collapsed", !open);
  };
  header.addEventListener("click", () => {
    if (!done) return;
    open = !open;
    applyOpen();
    if (open) viewport.scrollTop = 0;
  });
  return {
    append(fullText: string): void {
      stream.textContent = fullText;
      viewport.classList.toggle("is-capped", viewport.scrollHeight > viewport.clientHeight + 1);
      viewport.scrollTop = viewport.scrollHeight;
    },
    finish(): void {
      if (done) return;
      done = true;
      const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
      label.classList.remove("thinking-shimmer");
      label.innerHTML = `<span class="thinking-verb">${t("已思考")}</span> ${seconds} ${t("秒")}`;
      header.classList.add("is-clickable");
      open = false;
      applyOpen();
    },
  };
}

function scrollIntoView(element: HTMLElement): void {
  element.scrollIntoView({ block: "end", behavior: "smooth" });
}

export function isHomeChatActive(root: HTMLElement): boolean {
  return root.dataset.homeChat === "on";
}

export interface HomeChatTurnOptions {
  /** @ 引用资料的上下文块（composerReferenceBlock）：只进请求正文，不进气泡。 */
  referenceContext?: string;
  /** 引用标题：气泡下方的 @ 徽标如实标注。 */
  referenceTitles?: string[];
}

/**
 * 渲染一轮首页对话：用户气泡 + 与执行页同构的流式回复（思考块 + Markdown 正文）。
 * 返回是否成功收到回复（登录失效时弹登录框并返回 false；调用方据此决定
 * 是否清空引用——失败保留以便重试，与执行页同语义）。
 */
export async function runHomeChatTurn(
  root: HTMLElement,
  text: string,
  options: HomeChatTurnOptions = {},
): Promise<boolean> {
  const thread = ensureThread(root);
  if (!thread) return false;
  root.dataset.homeChat = "on";
  appendUserBubble(thread, text, options.referenceTitles ?? []);
  const block = appendAssistantBlock(thread);
  const copy = block.querySelector<HTMLElement>(".analysis-copy")!;
  scrollIntoView(block);

  // 容器对象而非裸 let：闭包内的赋值不参与 TS 控制流收窄，避免外部读取被推成 never
  const state: {
    thinking: ReturnType<typeof createThinkingBlock> | null;
    sawDelta: boolean;
  } = { thinking: null, sawDelta: false };
  try {
    await sendConversationTurn(
      text,
      {
        onReasoning: (_delta, full) => {
          state.thinking ??= createThinkingBlock(block);
          state.thinking.append(full);
        },
        onDelta: (_delta, full) => {
          if (!state.sawDelta) {
            state.sawDelta = true;
            state.thinking?.finish();
          }
          copy.innerHTML = renderMarkdown(full);
          // 已闭合的公式随增量立即排版，与执行页对话同语义（实时渲染）
          typesetMath(copy);
        },
      },
      {
        ...(options.referenceContext ? { attachmentContext: options.referenceContext } : {}),
        ...(options.referenceTitles?.length ? { attachmentNames: options.referenceTitles } : {}),
      },
    );
    state.thinking?.finish();
    scrollIntoView(block);
    return true;
  } catch (error) {
    state.thinking?.finish();
    if (error instanceof ChatError && error.code === "AUTH_REQUIRED") {
      copy.innerHTML = `<p class="muted">${t("请先登录后再继续对话。")}</p>`;
      openAuthDialog({});
      return false;
    }
    const message = error instanceof Error && error.message ? error.message : t("对话请求失败，请稍后再试");
    copy.innerHTML = "";
    const failure = document.createElement("p");
    failure.className = "home-chat-error";
    failure.textContent = message;
    copy.append(failure);
    return false;
  }
}
