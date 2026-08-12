/**
 * 界面文案翻译适配层。
 *
 * 现有 14 个页面由 legacy 模板一次性生成中文 markup，把它们改写成 key 驱动会动到
 * 受保护的页面结构。这里改为在渲染结果上做适配：按“整段完全匹配”替换词典命中的
 * 文案，未命中的原样保留。整段匹配是安全边界——项目名、赛题标题、用户输入这类
 * 真实数据不会出现在词典里，因此永远不会被改写。
 *
 * 切回中文时用记录的原文还原，不需要刷新页面。
 */

export type TranslationDictionary = Readonly<Record<string, string>>;

/** 这些属性会直接呈现给用户，需与正文一起翻译。 */
const TRANSLATABLE_ATTRIBUTES = [
  "placeholder",
  "title",
  "aria-label",
  "alt",
  "data-title",
  "data-subtitle",
] as const;

/** 代码、样式与表单初始值不属于界面文案：翻译它们会改变用户实际提交的内容。 */
const SKIPPED_TAGS = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA", "NOSCRIPT"]);

/**
 * 可编辑区域（论文正文等）里的文字是用户数据而不是界面文案。用户如果正好输入了
 * 与词典键相同的词，替换会直接改掉他写的内容，因此整块跳过。
 */
function isEditableHost(element: Element): boolean {
  return element instanceof HTMLElement && element.isContentEditable;
}

const originalText = new Map<Text, string>();
const originalAttributes = new Map<Element, Map<string, string>>();

let dictionary: TranslationDictionary | null = null;
let observer: MutationObserver | null = null;

function translationFor(value: string): string | null {
  if (!dictionary) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const translated = dictionary[trimmed];
  return translated && translated !== trimmed ? translated : null;
}

function isSkipped(node: Node): boolean {
  for (let current = node.parentElement; current; current = current.parentElement) {
    if (SKIPPED_TAGS.has(current.tagName) || isEditableHost(current)) return true;
  }
  return false;
}

function translateTextNode(node: Text): void {
  const source = originalText.get(node) ?? node.nodeValue ?? "";
  const translated = translationFor(source);
  if (translated === null) return;
  if (!originalText.has(node)) originalText.set(node, source);

  const parent = node.parentElement;
  // 没有显式 value 的 <option>，其 value 等于可见文本；先把原文固化成 value，
  // 否则翻译后保存设置会写入英文，切回中文时选项就对不上了。
  if (parent instanceof HTMLOptionElement && !parent.hasAttribute("value")) {
    parent.setAttribute("value", source.trim());
  }

  const leading = source.slice(0, source.length - source.trimStart().length);
  const trailing = source.slice(source.trimEnd().length);
  node.nodeValue = `${leading}${translated}${trailing}`;
}

function translateAttributes(element: Element): void {
  for (const attribute of TRANSLATABLE_ATTRIBUTES) {
    const stored = originalAttributes.get(element)?.get(attribute);
    const source = stored ?? element.getAttribute(attribute);
    if (source === null || source === undefined) continue;
    const translated = translationFor(source);
    if (translated === null) continue;
    if (stored === undefined) {
      const bucket = originalAttributes.get(element) ?? new Map<string, string>();
      bucket.set(attribute, source);
      originalAttributes.set(element, bucket);
    }
    element.setAttribute(attribute, translated);
  }
}

function translateTree(root: Node): void {
  if (root.nodeType === Node.TEXT_NODE) {
    if (!isSkipped(root)) translateTextNode(root as Text);
    return;
  }
  if (!(root instanceof Element) && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
  if (root instanceof Element) {
    if (SKIPPED_TAGS.has(root.tagName) || isEditableHost(root)) return;
    translateAttributes(root);
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node instanceof Element && (SKIPPED_TAGS.has(node.tagName) || isEditableHost(node))) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node instanceof Element) translateAttributes(node);
    else translateTextNode(node as Text);
  }
}

/** 暂停观察后写 DOM：避免自己的改写再次触发观察回调形成回环。 */
function withoutObserving(work: () => void): void {
  const active = observer;
  active?.disconnect();
  try {
    work();
  } finally {
    if (active) startObserving(active);
  }
}

function startObserving(target: MutationObserver): void {
  target.observe(document.documentElement, {
    subtree: true,
    childList: true,
    characterData: true,
    attributeFilter: [...TRANSLATABLE_ATTRIBUTES],
  });
}

function handleMutations(records: MutationRecord[]): void {
  withoutObserving(() => {
    for (const record of records) {
      if (record.type === "childList") {
        record.addedNodes.forEach(node => translateTree(node));
      } else if (record.type === "characterData" && record.target.nodeType === Node.TEXT_NODE) {
        if (!isSkipped(record.target)) translateTextNode(record.target as Text);
      } else if (record.type === "attributes" && record.target instanceof Element) {
        translateAttributes(record.target);
      }
    }
  });
}

/** 按词典翻译当前文档，并持续翻译后续渲染出来的节点。 */
export function activateTranslation(next: TranslationDictionary): void {
  dictionary = next;
  if (!observer) observer = new MutationObserver(handleMutations);
  withoutObserving(() => translateTree(document.documentElement));
}

/** 还原全部已翻译内容并停止观察（切回源语言）。 */
export function deactivateTranslation(): void {
  observer?.disconnect();
  observer = null;
  dictionary = null;
  for (const [node, source] of originalText) {
    if (node.isConnected) node.nodeValue = source;
  }
  originalText.clear();
  for (const [element, attributes] of originalAttributes) {
    if (!element.isConnected) continue;
    for (const [attribute, source] of attributes) element.setAttribute(attribute, source);
  }
  originalAttributes.clear();
}

/** 翻译单条文案（用于 document.title 等不在 DOM 正文里的位置）。 */
export function translateText(value: string): string {
  return translationFor(value) ?? value;
}
