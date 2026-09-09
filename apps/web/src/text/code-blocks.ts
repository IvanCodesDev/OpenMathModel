/**
 * 对话回复里代码块的装饰：复制按钮 + 语法高亮（作用于 renderMarkdown 输出的 `.md-code` 容器）。
 *
 * 结构与样式分工：renderMarkdown 只给出 `.md-code[data-lang][data-label] > pre > code`；
 * 标题带与语言标签由 CSS（::before 取 data-label）画出；这里负责两件需要 JS 的事——
 * 把复制按钮插进容器、把源码换成带 token 的高亮 HTML。
 *
 * 高亮走 highlight.js：核心与每种语法都是独立分包，页面第一次遇到某种语言的代码块才下载
 * 那一种（code-language-loaders），与 KaTeX 同一姿态：加载前源码原样可读，就绪后对 scope 里
 * 届时存在的代码块补高亮。围栏没标语言或语言没有加载器就只做等宽排版，不做自动猜测——
 * 流式期间猜测结果会随文本增长来回变，反而闪。
 *
 * 流式渲染每帧重建尾部那个代码块，所以按 (语言, 源码) 缓存高亮结果：已定格的块再次出现时
 * 只剩字符串赋值。缓存有上限，半截源码留下的键不会无限堆积。
 */

import { copyTextToClipboard } from "../diagnostics/system-diagnostics";
import { t } from "../i18n/locale";
import { LANGUAGE_DEPENDENCIES, LANGUAGE_LOADERS } from "./code-language-loaders";

type Hljs = typeof import("highlight.js/lib/core").default;

const CACHE_LIMIT = 160;

let hljs: Hljs | null = null;
let corePromise: Promise<Hljs> | null = null;
/** 每种语法只装一次；同一语法在流式期间被反复请求时复用同一个 promise。 */
const languagePromises = new Map<string, Promise<void>>();
const cache = new Map<string, string>();

function remember(key: string, html: string): void {
  if (cache.size >= CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(key, html);
}

function languageOf(code: HTMLElement): string {
  return code.closest<HTMLElement>(".md-code")?.dataset.lang ?? "";
}

/**
 * 给一个 code 节点上色。语法已注册就同步完成；返回 false 表示这一块还等着语法加载，
 * 调用方去触发加载并在就绪后回头再来。没有加载器的语言直接标记完成（保持等宽源码）。
 */
function highlight(code: HTMLElement): boolean {
  if (code.dataset.hlDone === "true") return true;
  const language = languageOf(code);
  if (!language || !(language in LANGUAGE_LOADERS)) {
    code.dataset.hlDone = "true";
    return true;
  }
  if (!hljs?.getLanguage(language)) return false;
  const source = code.textContent ?? "";
  const key = `${language}\u0000${source}`;
  let html = cache.get(key);
  if (html === undefined) {
    try {
      // ignoreIllegals：流式半截代码随时可能停在语法非法处，不能因此放弃整块高亮
      html = hljs.highlight(source, { language, ignoreIllegals: true }).value;
    } catch {
      code.dataset.hlDone = "true";
      return true;
    }
    remember(key, html);
  }
  code.innerHTML = html;
  code.dataset.hlDone = "true";
  return true;
}

/** 给 scope 里所有待上色的块上色；返回仍在等待语法的语言集合。 */
function highlightAll(scope: ParentNode): Set<string> {
  const waiting = new Set<string>();
  scope.querySelectorAll<HTMLElement>(".md-code > pre > code:not([data-hl-done])").forEach(code => {
    if (!highlight(code)) waiting.add(languageOf(code));
  });
  return waiting;
}

function loadCore(): Promise<Hljs> {
  corePromise ??= import("highlight.js/lib/core").then(module => {
    hljs = module.default;
    return hljs;
  });
  return corePromise;
}

/** 装一种语法（核心 + 模块 + 注册）；没有加载器返回 null。不递归装依赖，避免互相引用死锁。 */
function loadModule(language: string): Promise<void> | null {
  const load = LANGUAGE_LOADERS[language];
  if (!load) return null;
  let pending = languagePromises.get(language);
  if (!pending) {
    pending = Promise.all([loadCore(), load()]).then(([core, module]) => {
      if (!core.getLanguage(language)) core.registerLanguage(language, module.default);
    });
    languagePromises.set(language, pending);
  }
  return pending;
}

/** 装一种语法连同它一层的内嵌子语言，全部就位后再上色，嵌入部分才不会先灰后彩地闪一下。 */
function loadLanguage(language: string): Promise<void> | null {
  const main = loadModule(language);
  if (!main) return null;
  const dependencies = (LANGUAGE_DEPENDENCIES[language] ?? [])
    .map(loadModule)
    .filter((pending): pending is Promise<void> => pending !== null);
  return Promise.all([main, ...dependencies]).then(() => undefined);
}

// ── 复制按钮 ─────────────────────────────────────────────────────

const RESET_DELAY_MS = 1400;

function copyButtonHtml(): string {
  const label = t("复制代码");
  return `<button type="button" class="md-code-copy" data-code-copy title="${label}" aria-label="${label}">`
    + `<i class="ph ph-copy" aria-hidden="true"></i><span>${t("复制")}</span></button>`;
}

let delegateBound = false;

/** 复制按钮的点击只在 document 上委托一次：代码块随流式渲染反复重建，逐个绑定既漏又重。 */
function bindCopyDelegate(): void {
  if (delegateBound) return;
  delegateBound = true;
  document.addEventListener("click", event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest<HTMLButtonElement>("[data-code-copy]");
    if (!button) return;
    const code = button.closest(".md-code")?.querySelector("pre > code");
    if (!code) return;
    // textContent 不受高亮 span 影响，复制到的就是源码本身
    void copyTextToClipboard(code.textContent ?? "").then(copied => {
      button.classList.toggle("is-copied", copied);
      button.innerHTML = copied
        ? `<i class="ph ph-check" aria-hidden="true"></i><span>${t("已复制")}</span>`
        : `<i class="ph ph-copy" aria-hidden="true"></i><span>${t("复制失败")}</span>`;
      window.setTimeout(() => {
        button.classList.remove("is-copied");
        button.innerHTML = `<i class="ph ph-copy" aria-hidden="true"></i><span>${t("复制")}</span>`;
      }, RESET_DELAY_MS);
    });
  });
}

/**
 * 装饰 scope 内所有代码块：插复制按钮（幂等）、补高亮（语法已装好的同步上色，没装的按语言
 * 触发加载、就绪后回头补）。调用时机与 typesetMath 一致——每次把 Markdown 写进对话气泡之后。
 */
export function decorateCodeBlocks(scope: ParentNode): void {
  const blocks = scope.querySelectorAll<HTMLElement>(".md-code:not([data-code-ready])");
  if (blocks.length) {
    bindCopyDelegate();
    blocks.forEach(block => {
      block.insertAdjacentHTML("afterbegin", copyButtonHtml());
      block.dataset.codeReady = "true";
    });
  }
  for (const language of highlightAll(scope)) {
    // 就绪回调重新查询 scope：流式期间 DOM 可能已被新增量替换，不能沿用触发时的节点
    loadLanguage(language)?.then(() => { highlightAll(scope); }).catch(() => {
      // 离线或加载失败：源码本来就在 DOM 里，等宽排版照常。标记完成，别每帧再试一次。
      scope.querySelectorAll<HTMLElement>(".md-code > pre > code:not([data-hl-done])").forEach(code => {
        if (languageOf(code) === language) code.dataset.hlDone = "true";
      });
    });
  }
}
