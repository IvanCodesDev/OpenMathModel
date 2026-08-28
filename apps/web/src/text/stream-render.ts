/**
 * 流式回复的渲染调度器：解决「每个增量整段重建」的卡顿。
 *
 * 旧写法在每个 SSE 增量上执行 `innerHTML = renderMarkdown(全文)` + 全量公式
 * 排版：长回复（尤其大量公式时）每秒几十次全 DOM 重建 + 布局，页面明显卡顿。
 * 这里改成两层削峰：
 *
 * - 时间上：渲染合并到每 RENDER_INTERVAL_MS 至多一次，落在最新内容上；
 * - 空间上：markdown 渲染进离屏容器后按「块级前缀对比」增量上屏——流式内容
 *   只在尾部增长，已上屏且未变化的前缀块（含已排版的公式）原样保留，每帧
 *   只替换真正变化的尾部块。
 *
 * 公式排版仍走 text/math-typeset（KaTeX 结果缓存 + data-tex-done 跳过），
 * 每次真实渲染后只会排版新替换的尾部块。
 */

import { renderMarkdown } from "./markdown";
import { typesetMath } from "./math-typeset";

const RENDER_INTERVAL_MS = 160;

export interface StreamingMarkdownRenderer {
  /** 记录最新全文并按节流节奏渲染。 */
  update(fullText: string): void;
  /** 立即渲染最终全文（流结束/失败收尾时调用）。 */
  finish(fullText?: string): void;
  /** 放弃未落地的排队渲染（错误路径要改写容器前调用）。 */
  cancel(): void;
}

export interface StreamingMarkdownOptions {
  /** 跟随输出吸底的滚动容器：渲染前测「接近底部」，渲染后补滚动。 */
  stickTo?: HTMLElement | null;
}

export function createStreamingMarkdownRenderer(
  target: HTMLElement,
  options: StreamingMarkdownOptions = {},
): StreamingMarkdownRenderer {
  let latest = "";
  let lastBlocksHtml: string[] = [];
  let initialized = false;
  let timer: number | undefined;
  let lastRenderAt = 0;

  const apply = (): void => {
    const scroll = options.stickTo ?? null;
    const stick = scroll
      ? scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140
      : false;
    if (!initialized) {
      // 首次渲染接管容器（清掉「思考中…」占位）
      target.replaceChildren();
      initialized = true;
    }
    const host = document.createElement("div");
    host.innerHTML = renderMarkdown(latest);
    const next = [...host.children] as HTMLElement[];
    const nextHtml = next.map(node => node.outerHTML);
    // 未变化的前缀块保留（公式已排版、DOM 不动）；从第一处差异起整体替换
    let firstDiff = 0;
    while (
      firstDiff < lastBlocksHtml.length
      && firstDiff < nextHtml.length
      && lastBlocksHtml[firstDiff] === nextHtml[firstDiff]
    ) {
      firstDiff += 1;
    }
    while (target.children.length > firstDiff) target.lastElementChild!.remove();
    for (let index = firstDiff; index < next.length; index += 1) {
      target.append(next[index]);
    }
    lastBlocksHtml = nextHtml;
    typesetMath(target);
    if (stick && scroll) scroll.scrollTop = scroll.scrollHeight;
  };

  const schedule = (): void => {
    if (timer !== undefined) return;
    const wait = Math.max(0, lastRenderAt + RENDER_INTERVAL_MS - Date.now());
    timer = window.setTimeout(() => {
      timer = undefined;
      lastRenderAt = Date.now();
      apply();
    }, wait);
  };

  return {
    update(fullText: string): void {
      latest = fullText;
      schedule();
    },
    finish(fullText?: string): void {
      if (fullText !== undefined) latest = fullText;
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
      lastRenderAt = Date.now();
      apply();
    },
    cancel(): void {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    },
  };
}

/**
 * 纯文本流的节流写入（思考过程等）：文本赋值本身便宜，贵的是每次随写入的
 * 布局读写（scrollHeight/scrollTop），同样合并到节流节奏上。
 */
export function createThrottledTextSink(
  apply: (text: string) => void,
  intervalMs = 120,
): { update(text: string): void; flush(): void } {
  let latest = "";
  let timer: number | undefined;
  let lastAt = 0;
  const run = (): void => {
    timer = undefined;
    lastAt = Date.now();
    apply(latest);
  };
  return {
    update(text: string): void {
      latest = text;
      if (timer !== undefined) return;
      timer = window.setTimeout(run, Math.max(0, lastAt + intervalMs - Date.now()));
    },
    flush(): void {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
      lastAt = Date.now();
      apply(latest);
    },
  };
}
