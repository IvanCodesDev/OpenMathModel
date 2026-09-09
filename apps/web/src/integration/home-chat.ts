/**
 * 首页轻量对话（对话优先接待的「聊天态」）。
 *
 * 发送先经接待判定（POST /task-intake）分流：完整题面走既有任务创建链路；
 * 闲聊或缺题面的输入进入本模块。视觉与执行页对话严格同构——复用全局的
 * user-bubble / assistant-block / assistant-id（水母 Logo）/ reply-thinking
 * （思考过程折叠块）/ analysis-copy（Markdown 正文）类与交互，不另造样式。
 * 对话历史由 agent-chat 维护（走用户配置的模型与 Auto 路由）。
 * 对话中一旦出现完整题面（接待判定升级），交回任务创建链路跳转执行页。
 *
 * 这条链不建项目、不起运行。首轮发送时向 tasks/chat-sessions 领一个 `chat_…`
 * 身份并绑定给 agent-chat：这一轮起对话走服务端托管轮（ADR-0016），生成在
 * 服务端后台进行、记录落服务端 chat_turns；侧栏「最近任务」按本机会话清单列出
 * 对话，点回来由 restoreHomeChat 从服务端拉全部轮重建现场——仍在生成的那一轮
 * 原地续接直播（同受「保存任务历史」开关管辖：关闭时不领身份、不落盘）。
 *
 * DOM 由本模块动态插入（与执行计划面板同模式），不改动页面模板与路由。
 */

import { fetchMe } from "../auth/api";
import { openAuthDialog } from "../auth/auth-dialog";
import { t } from "../i18n/locale";
import { saveHistoryEnabled } from "../preferences/privacy-preferences";
import {
  createChatSession,
  findChatSession,
  newChatSessionId,
  touchChatSession,
} from "../tasks/chat-sessions";
import { loadConversationLog, type ConversationLogEntry } from "../tasks/conversation-log";
import { decorateCodeBlocks } from "../text/code-blocks";
import { renderMarkdown } from "../text/markdown";
import { typesetMath } from "../text/math-typeset";
import {
  createRestoredThinkingBlock,
  createStreamingMarkdownRenderer,
  createThrottledTextSink,
} from "../text/stream-render";
import {
  ChatError,
  attachConversationTurn,
  configureConversation,
  conversationSnapshot,
  entryFromTurn,
  hydrateConversation,
  resetConversation,
  sendConversationTurn,
  type ChatHandlers,
  type ChatTurnResult,
} from "./agent-chat";
import { hydrateRecentTasks } from "./recent-tasks";
import { mountReplyActions } from "./reply-actions";

const AGENT_ID_HTML =
  '<img class="project-logo assistant-logo" src="/assets/OpenMathModel_IP_Crop.png" alt="" aria-hidden="true"><span>Agent</span>';

/** 最后一段回复与输入区上沿之间的呼吸距（执行页 chat-scroll 的口径约 20–28px）。 */
const DOCK_GAP_PX = 24;

/**
 * 对话态里线程铺满整个主区、输入区（composer-area + 免责声明）浮在它上面
 * （与执行页 .chat-scroll + .chat-composer 同一手法，滚动条才能从顶到底）。
 * 因此要把「输入区顶边到主区底边」的实际高度让给线程的 padding-bottom /
 * scroll-padding-bottom（CSS 变量 --home-chat-dock），否则最后一段回复会被
 * 输入框盖住。附件托盘、接待状态行都会改变输入区高度，所以按实际几何量、
 * 并随尺寸变化重算——与执行计划面板（task-todo-panel）的让位逻辑同模式。
 */
function reserveDockSpace(root: HTMLElement, composerArea: HTMLElement): void {
  const rootRect = root.getBoundingClientRect();
  const areaRect = composerArea.getBoundingClientRect();
  if (rootRect.height === 0 || areaRect.height === 0) return;
  const reserved = Math.round(rootRect.bottom - areaRect.top + DOCK_GAP_PX);
  root.style.setProperty("--home-chat-dock", `${reserved}px`);
}

function observeDock(root: HTMLElement, thread: HTMLElement, composerArea: HTMLElement): void {
  const update = (): void => reserveDockSpace(root, composerArea);
  update();
  if (typeof ResizeObserver === "undefined") {
    window.addEventListener("resize", update);
    return;
  }
  const observer = new ResizeObserver(() => {
    // 整页重渲染后旧线程被丢弃：随之停表，不给已脱离文档的节点白算
    if (!thread.isConnected) {
      observer.disconnect();
      return;
    }
    update();
  });
  observer.observe(composerArea);
  observer.observe(root);
}

function ensureThread(root: HTMLElement): HTMLElement | null {
  const existing = root.querySelector<HTMLElement>("[data-home-chat-thread]");
  if (existing?.isConnected) return existing;
  const composerArea = root.querySelector<HTMLElement>(".composer-area");
  if (!composerArea) return null;
  const thread = document.createElement("div");
  thread.className = "home-chat-thread";
  thread.dataset.homeChatThread = "true";
  composerArea.insertAdjacentElement("beforebegin", thread);
  observeDock(root, thread, composerArea);
  return thread;
}

function appendUserBubble(thread: HTMLElement, text: string, referenceTitles: string[]): HTMLElement {
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
  return message;
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
    if (open) {
      viewport.scrollTop = 0;
      // 回看盒高与流式矮窗不同：按当前盒高重算是否可滚，渐隐遮罩才如实
      viewport.classList.toggle("is-capped", viewport.scrollHeight > viewport.clientHeight + 1);
    }
  });
  // 文本赋值与布局读写按节流节奏合并：高频 reasoning 增量不再逐条触发重排
  const sink = createThrottledTextSink(fullText => {
    stream.textContent = fullText;
    viewport.classList.toggle("is-capped", viewport.scrollHeight > viewport.clientHeight + 1);
    viewport.scrollTop = viewport.scrollHeight;
  });
  return {
    append(fullText: string): void {
      sink.update(fullText);
    },
    finish(): void {
      if (done) return;
      done = true;
      sink.flush();
      // 思考已完整落地：切换回看态——盒子放宽但仍限高内滚（styles.css 的 is-done 规则），
      // 不整段铺开（2026-08-31 用户要求）
      host.classList.add("is-done");
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

// ── 对话归属：不建项目，用本机会话 id 承载记录 ──────────────────────────────

let activeChatId: string | null = null;

/**
 * 绑定本轮对话的归属：首轮领一个 `chat_…` 身份并登记标题（取首条消息），
 * 之后沿用同一个。未登录或「保存任务历史」关闭时不登记——不落盘的对话
 * 列进「最近任务」只会点开一片空白，页面内存里照常聊完这一程。
 * 返回本轮是否新建了会话（新条目要立刻出现在侧栏）。
 */
async function ensureChatSession(firstText: string): Promise<boolean> {
  if (activeChatId || !saveHistoryEnabled()) return false;
  // 只认领还没往返过的对话：聊到一半才开启「保存任务历史」时再绑定，会触发
  // agent-chat 的换归属清空，把已聊的上下文抹掉，这一程就保持不落盘。
  if (conversationSnapshot().turns > 0) return false;
  const me = await fetchMe().catch(() => null);
  if (!me) return false;
  const id = newChatSessionId();
  if (!createChatSession(id, me.user.id, firstText)) return false;
  activeChatId = id;
  configureConversation(id);
  return true;
}

/** 首页回到欢迎态（地址栏没带 ?chat=）：下一条消息另开一段对话。 */
export function resetHomeChat(): void {
  activeChatId = null;
  resetConversation();
}

/** 重建一条已定格的回复（本机旧记录与服务端已结束的轮同一形态）。 */
function appendSettledReply(thread: HTMLElement, entry: ConversationLogEntry): void {
  const block = appendAssistantBlock(thread);
  const copy = block.querySelector<HTMLElement>(".analysis-copy")!;
  // 思考过程随记录一起回来：正文上方重建折叠的「已思考」回看盒
  if (entry.reasoning) block.insertBefore(createRestoredThinkingBlock(entry.reasoning), copy);
  copy.innerHTML = entry.text ? renderMarkdown(entry.text) : "";
  // 被打断的回复（暂停 / 接口报错 / 服务重启）：半截正文之下一行灰字说明原因
  if (entry.interrupted) {
    const note = document.createElement("p");
    note.className = "muted reply-interrupted";
    note.textContent = t(entry.note || "回复生成中断，请重新发送。");
    copy.append(note);
  }
  typesetMath(copy);
  decorateCodeBlocks(copy);
  // 一字未收的中断回复没有可复制的内容，不挂操作区（与任务页同口径）
  if (entry.text) mountReplyActions(block, { text: entry.text, turnId: entry.turnId, feedback: entry.feedback });
}

/**
 * 重建一段对话现场：侧栏「最近任务」点开对话条目时调用。服务端的轮（ADR-0016）
 * 在本机旧记录之后按时间顺序重建；仍在生成的那一轮原地续接直播——生成在服务端
 * 从未中断，页面只是回来接着看。两边都没有记录（关过「保存任务历史」）时返回
 * false，页面留在欢迎态。
 */
export async function restoreHomeChat(root: HTMLElement, chatId: string): Promise<boolean> {
  if (!findChatSession(chatId)) return false;
  activeChatId = chatId;
  configureConversation(chatId);
  const legacyEntries = loadConversationLog(chatId);
  const turns = await hydrateConversation(chatId);
  // 等待期间用户已切到别的对话 / 回到欢迎态：这批记录不再属于眼前的页面
  if (activeChatId !== chatId) return false;
  if (legacyEntries.length === 0 && turns.length === 0) return false;
  const thread = ensureThread(root);
  if (!thread) return false;
  thread.replaceChildren();
  root.dataset.homeChat = "on";
  for (const entry of legacyEntries) {
    if (entry.role === "user") appendUserBubble(thread, entry.text, entry.attachments ?? []);
    else appendSettledReply(thread, entry);
  }
  for (const turn of turns) {
    appendUserBubble(thread, turn.text, turn.attachments);
    if (turn.status === "running") {
      const block = appendAssistantBlock(thread);
      void presentReply(root, thread, block, (handlers, signal) => attachConversationTurn(turn, handlers, signal))
        .then(ok => { if (ok && activeChatId) touchChatSession(activeChatId); });
      continue;
    }
    appendSettledReply(thread, entryFromTurn(turn));
  }
  thread.scrollTop = thread.scrollHeight;
  return true;
}

// ── 暂停生成：回复流式期间发送键变为暂停键（与执行页同语义） ────────────────

let activeAbort: AbortController | null = null;

/** 首页对话是否在生成中；点暂停键时由 task-start-controller 调用。 */
export function stopHomeChatGeneration(): boolean {
  if (!activeAbort) return false;
  activeAbort.abort();
  return true;
}

function setSendButtonGenerating(root: HTMLElement, on: boolean): void {
  root.querySelectorAll<HTMLButtonElement>('.composer [data-action="send"]').forEach(button => {
    if (on) {
      button.dataset.mode = "stop";
      button.innerHTML = '<i class="ph-fill ph-stop" aria-hidden="true"></i>';
      button.title = t("暂停生成");
      button.setAttribute("aria-label", t("暂停生成"));
    } else {
      delete button.dataset.mode;
      button.innerHTML = '<i class="ph ph-arrow-up" aria-hidden="true"></i>';
      button.title = t("发送（Enter）");
      button.setAttribute("aria-label", t("发送"));
    }
  });
}

/**
 * 发送瞬间先落地的一对「用户气泡 + 思考中占位」：接待判定还在后台跑，页面已经有反应。
 * 判定为对话 → 交给 runHomeChatTurn 在同一个占位块里续写；判定放行任务 → 过场遮罩接管、
 * 随后跳转，占位无需处置；需要登录 / 出错 → discard() 撤回，页面回到发送前的样子。
 */
export interface PendingHomeTurn {
  readonly thread: HTMLElement;
  readonly block: HTMLElement;
  discard(): void;
}

export function beginHomeChatTurn(
  root: HTMLElement,
  text: string,
  referenceTitles: string[] = [],
): PendingHomeTurn | null {
  const thread = ensureThread(root);
  if (!thread) return null;
  const wasChatting = root.dataset.homeChat === "on";
  root.dataset.homeChat = "on";
  const message = appendUserBubble(thread, text, referenceTitles);
  const block = appendAssistantBlock(thread);
  scrollIntoView(block);
  return {
    thread,
    block,
    discard() {
      message.remove();
      block.remove();
      // 这一对是欢迎态下的第一条：撤回后线程空了，把 hero 也还回来
      if (!wasChatting && !thread.querySelector(".user-message, .assistant-block")) {
        thread.remove();
        delete root.dataset.homeChat;
      }
    },
  };
}

export interface HomeChatTurnOptions {
  /** @ 引用资料的上下文块（composerReferenceBlock）：只进请求正文，不进气泡。 */
  referenceContext?: string;
  /** 引用标题：气泡下方的 @ 徽标如实标注。 */
  referenceTitles?: string[];
  /** 发送瞬间已落地的占位（beginHomeChatTurn）：回复写进它，不再另起一对。 */
  pending?: PendingHomeTurn | null;
}

/**
 * 一轮回复从流式呈现到落定的公共骨架：首发（sendConversationTurn）与重进续接
 * （attachConversationTurn）共用——半截续上的回复与从头看着生成的长得一样。
 * 生成期间发送键变暂停键（中止 → 服务端真正停止生成，不是本页不看了）。
 * 返回是否成功收到回复。
 */
async function presentReply(
  root: HTMLElement,
  thread: HTMLElement,
  block: HTMLElement,
  run: (handlers: ChatHandlers, signal: AbortSignal) => Promise<ChatTurnResult>,
): Promise<boolean> {
  const copy = block.querySelector<HTMLElement>(".analysis-copy")!;
  // 容器对象而非裸 let：闭包内的赋值不参与 TS 控制流收窄，避免外部读取被推成 never
  const state: {
    thinking: ReturnType<typeof createThinkingBlock> | null;
    sawDelta: boolean;
  } = { thinking: null, sawDelta: false };
  // 渲染节流 + 块级增量上屏（长回复不再逐增量整段重建），公式排版随之削峰。
  // stickTo=thread：对话态滚动在线程内部（composer 常驻视口底部），
  // 流式期间近底自动跟随，与执行页 chat-scroll 同语义。
  const renderer = createStreamingMarkdownRenderer(copy, { stickTo: thread });
  const abort = new AbortController();
  activeAbort = abort;
  setSendButtonGenerating(root, true);
  try {
    const { text: reply, turnId } = await run(
      {
        onReasoning: (_delta, full) => {
          // 思考流入场也跟随滚动（与执行页同节奏）：用户已在底部才吸底，翻上去回看不打扰
          const stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
          if (!state.thinking) {
            state.thinking = createThinkingBlock(block);
            // 思考块自带「思考中…」标签，回答区占位不必重复（与执行页同语义）：
            // 不清会出现上下两个「思考中…」同屏。渲染器首次上屏自会接管容器，
            // 这里提前清空不影响其增量状态。
            if (!state.sawDelta) copy.innerHTML = "";
          }
          state.thinking.append(full);
          if (stick) thread.scrollTop = thread.scrollHeight;
        },
        onDelta: (_delta, full) => {
          if (!state.sawDelta) {
            state.sawDelta = true;
            state.thinking?.finish();
          }
          renderer.update(full);
        },
      },
      abort.signal,
    );
    state.thinking?.finish();
    // 收尾不再强制滚到底：跟随交给 stickTo 的近底吸附——用户翻上去回看时，
    // 回复完成不该把视口拽走（执行页同语义）。
    renderer.finish(reply);
    // 回复右下角：复制 + 赞 / 踩（评价落在这一轮托管对话上；与任务页同一组件）
    if (reply) mountReplyActions(block, { text: reply, turnId });
    return true;
  } catch (error) {
    state.thinking?.finish();
    renderer.cancel();
    if (error instanceof ChatError && error.code === "GENERATION_STOPPED") {
      copy.innerHTML = `<p class="muted">${t("已暂停生成。")}</p>`;
      return false;
    }
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
  } finally {
    if (activeAbort === abort) {
      activeAbort = null;
      setSendButtonGenerating(root, false);
    }
  }
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
  let pending = options.pending?.block.isConnected ? options.pending : null;
  if (!pending) {
    pending = beginHomeChatTurn(root, text, options.referenceTitles ?? []);
    if (!pending) return false;
  }
  const { thread, block } = pending;
  // 领身份期间发送键就已是暂停键（与之前的节奏一致）
  setSendButtonGenerating(root, true);
  // 归属要在发送前定下：有归属这一轮才走服务端托管（生成与记录都在服务端）。
  const startedNewSession = await ensureChatSession(text);
  const delivered = await presentReply(root, thread, block, (handlers, signal) => sendConversationTurn(
    text,
    handlers,
    {
      ...(options.referenceContext ? { attachmentContext: options.referenceContext } : {}),
      ...(options.referenceTitles?.length ? { attachmentNames: options.referenceTitles } : {}),
      signal,
    },
  ));
  if (delivered && activeChatId) {
    touchChatSession(activeChatId);
    // 新对话要立刻出现在侧栏；后续轮次只更新本机时间戳，不为每条消息重拉清单
    if (startedNewSession) void hydrateRecentTasks();
  }
  return delivered;
}
