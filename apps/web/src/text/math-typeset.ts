/**
 * KaTeX 公式排版器（懒加载 + 结果缓存，全站共享）。
 *
 * KaTeX 体积远大于页面其余代码，因此首次遇到公式才动态加载模块与样式，
 * 加载前 data-tex 节点保留 LaTeX 源码作为可读回退（与方法库一致）。
 *
 * 为支持流式回复的「公式闭合即渲染」做了两件事：
 * - 模块就绪后排版全程同步：流式增量每次都整体重建气泡 innerHTML，若排版
 *   异步完成，已排版公式会先闪回源码再变回公式；同步排版与 innerHTML 赋值
 *   落在同一帧，公式以成品形态直接出现，没有肉眼可见的抖动；
 * - 同一（公式, 展示模式）的 KaTeX HTML 只解析一次：流式重排反复遇到相同
 *   公式时只剩缓存字符串赋值，成本不随增量次数与公式数量累积。
 */

type KatexModule = typeof import("katex").default;

let katex: KatexModule | null = null;
let loader: Promise<void> | null = null;
const cache = new Map<string, string>();

function typeset(node: HTMLElement): void {
  if (!katex || node.dataset.texDone === "true") return;
  const tex = node.dataset.tex ?? "";
  // 聊天气泡里的行内公式带 data-tex-inline；方法库等块级公式保持展示模式
  const display = node.dataset.texInline !== "true";
  const key = `${display ? "D" : "I"}\u0000${tex}`;
  let html = cache.get(key);
  if (html === undefined) {
    try {
      html = katex.renderToString(tex, { throwOnError: false, displayMode: display });
    } catch {
      // 渲染失败时保留 LaTeX 源码文本，不让公式区变空白
      return;
    }
    cache.set(key, html);
  }
  node.innerHTML = html;
  node.dataset.texDone = "true";
}

/**
 * 排版 scope 内所有待排版的 data-tex 节点。KaTeX 已就绪时同步完成；未就绪时
 * 触发加载，就绪后对 scope 里届时存在的节点补排——流式期间 DOM 可能已被新
 * 增量整体重建，因此完成回调重新查询 scope，而不是沿用触发时的节点列表。
 */
export function typesetMath(scope: ParentNode = document): void {
  if (!scope.querySelector("[data-tex]")) return;
  if (katex) {
    scope.querySelectorAll<HTMLElement>("[data-tex]").forEach(typeset);
    return;
  }
  loader ??= Promise.all([
    import("katex"),
    import("katex/dist/katex.min.css"),
  ]).then(([module]) => {
    katex = module.default;
  });
  loader.then(() => typesetMath(scope)).catch(() => {
    // 离线或加载失败：LaTeX 源码回退已经在 DOM 里，无需额外处理
  });
}
