/**
 * composer「添加上下文」引用：从赛题库 / 优秀论文 / 方法库挑选资料，
 * 作为上下文块随下一条消息送给模型。
 *
 * 与附件上下文（ADR-0010 批次三）同一姿态：内容只并入请求正文、不进气泡
 * 展示；气泡下方以 @ 徽标如实标注引用了什么。选中项存页面内存（切屏清空），
 * 发送成功后清空，失败保留以便重试。
 */

import type { MethodEntry } from "../data/method-library";
import { t } from "../i18n/locale";
import type { KnowledgePaper, KnowledgeProblem } from "../types/knowledge-library";

export interface ComposerReference {
  key: string;
  kind: "problem" | "paper" | "method";
  title: string;
  text: string;
}

export const MAX_REFERENCES = 4;
/** 单份资料与合计的字符预算：与附件上下文（8000/20000）同级但略收紧。 */
const PER_REFERENCE_CHARS = 6000;
const TOTAL_REFERENCE_CHARS = 18_000;

const KIND_LABELS: Record<ComposerReference["kind"], string> = {
  problem: "赛题",
  paper: "优秀论文",
  method: "方法",
};

let selected: ComposerReference[] = [];
let chipsHost: HTMLElement | null = null;

const escapeHtml = (value: string): string =>
  value.replace(/[&<>"']/g, character => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" } as Record<string, string>
  )[character] ?? character);

// ── 资料 → 引用内容 ──────────────────────────────────────────────

interface ProblemContentBlock {
  type?: string;
  text?: string;
}

/** 赛题：完整题面（content_blocks 已是结构化文本，标题/段落/列表逐块拼接）。 */
export function problemReference(problem: KnowledgeProblem): ComposerReference {
  const blocks = Array.isArray(problem.content_blocks)
    ? (problem.content_blocks as ProblemContentBlock[])
    : [];
  const body = blocks
    .map(block => {
      const text = typeof block.text === "string" ? block.text.trim() : "";
      if (!text) return "";
      if (block.type === "heading") return `# ${text}`;
      if (block.type === "list_item") return `- ${text}`;
      return text;
    })
    .filter(Boolean)
    .join("\n");
  return {
    key: `problem:${problem.id}`,
    kind: "problem",
    title: `${problem.code} ${problem.title}`,
    text: body || problem.summary as string || "",
  };
}

/** 优秀论文：库里保存的是结构化元信息（获奖、模型、创新点、摘要），如实给出。 */
export function paperReference(paper: KnowledgePaper): ComposerReference {
  const lines = [
    `论文题目：${paper.title}`,
    `竞赛：${paper.competition} ${paper.year} · ${paper.category}`,
    paper.award ? `奖项：${paper.award}` : "",
    paper.institution ? `单位：${paper.institution}` : "",
    paper.distinctions.length ? `亮点：${paper.distinctions.join("；")}` : "",
    paper.models.length ? `使用模型：${paper.models.join("、")}` : "",
    paper.innovation ? `创新点：${paper.innovation}` : "",
    typeof paper.summary === "string" && paper.summary ? `摘要：${paper.summary}` : "",
  ].filter(Boolean);
  return {
    key: `paper:${paper.problem_code}:${paper.title}`,
    kind: "paper",
    title: paper.title,
    text: lines.join("\n"),
  };
}

/** 方法库：完整方法卡（场景、禁忌、流程、坑与稳健性），公式与代码配方不随行。 */
export function methodReference(method: MethodEntry): ComposerReference {
  const section = (label: string, items: string[]): string =>
    items.length ? `${label}：\n${items.map(item => `- ${item}`).join("\n")}` : "";
  const parts = [
    `方法：${method.name}（${method.subtitle}）｜分类：${method.category}`,
    method.introduction,
    section("适用场景", method.scenarios),
    section("选型禁忌", method.antipatterns),
    section("建模流程", method.workflow),
    `输入：${method.input}\n输出：${method.output}`,
    section("关键假设", method.assumptions),
    section("优势", method.advantages),
    section("局限", method.limitations),
    section("常见坑与修正", method.pitfalls),
    section("稳健性检查", method.robustness),
    method.metrics.length ? `评价指标：${method.metrics.join("、")}` : "",
  ].filter(Boolean);
  return {
    key: `method:${method.id}`,
    kind: "method",
    title: method.name,
    text: parts.join("\n\n"),
  };
}

// ── 选中项管理 ───────────────────────────────────────────────────

export function listComposerReferences(): readonly ComposerReference[] {
  return selected;
}

export type AddReferenceResult = "added" | "duplicate" | "full";

export function addComposerReference(reference: ComposerReference): AddReferenceResult {
  if (selected.some(item => item.key === reference.key)) return "duplicate";
  if (selected.length >= MAX_REFERENCES) return "full";
  selected.push(reference);
  renderChips();
  return "added";
}

export function clearComposerReferences(): void {
  selected = [];
  renderChips();
}

/** 切屏时调用：composer 随页面重建，选中项与 chips 宿主一并复位。 */
export function resetComposerReferences(): void {
  selected = [];
  chipsHost = null;
}

// ── 上下文块 ─────────────────────────────────────────────────────

/** 选中项 → 随消息发送的上下文块；超预算时截断并如实标注。 */
export function composerReferenceBlock(): string {
  if (selected.length === 0) return "";
  const parts: string[] = [];
  let used = 0;
  selected.forEach((reference, index) => {
    const budget = Math.min(PER_REFERENCE_CHARS, TOTAL_REFERENCE_CHARS - used);
    if (budget <= 0) return;
    let text = reference.text;
    if (text.length > budget) {
      text = `${text.slice(0, budget)}\n（内容超出预算，已截断）`;
    }
    used += text.length;
    parts.push(
      `【引用资料 ${index + 1}/${selected.length} · ${KIND_LABELS[reference.kind]}】${reference.title}\n${text}`,
    );
  });
  return parts.join("\n\n");
}

// ── chips 渲染（composer 模块自有 DOM 槽位，不动页面骨架） ─────────

function chipHtml(reference: ComposerReference): string {
  return `<span class="composer-context-chip" data-reference-key="${escapeHtml(reference.key)}">
    <i class="ph ph-at" aria-hidden="true"></i>
    <span>${escapeHtml(reference.title)}</span>
    <button type="button" data-reference-remove aria-label="${t("移除引用")}" title="${t("移除引用")}">
      <i class="ph ph-x" aria-hidden="true"></i>
    </button>
  </span>`;
}

function renderChips(): void {
  if (!chipsHost?.isConnected) return;
  chipsHost.innerHTML = selected.map(chipHtml).join("");
  chipsHost.hidden = selected.length === 0;
}

/** 把 chips 挂到输入行左侧（文字起点的左边，幂等）；选中项变化时自动重绘。 */
export function mountReferenceChips(composer: HTMLElement): void {
  let host = composer.querySelector<HTMLElement>(".composer-context-chips");
  if (!host) {
    host = document.createElement("div");
    host.className = "composer-context-chips";
    host.hidden = true;
    const inputRow = composer.querySelector<HTMLElement>(".composer-input-row");
    if (inputRow) inputRow.insertBefore(host, inputRow.firstChild);
    else composer.insertBefore(host, composer.firstChild);
    host.addEventListener("click", event => {
      const remove = (event.target as Element).closest("[data-reference-remove]");
      if (!remove) return;
      const chip = remove.closest<HTMLElement>("[data-reference-key]");
      if (!chip) return;
      selected = selected.filter(item => item.key !== chip.dataset.referenceKey);
      renderChips();
    });
  }
  chipsHost = host;
  renderChips();
}

// ── 首页 → 运行页的引用交接 ──────────────────────────────────────

const PENDING_PREFIX = "openmathmodel.taskReferences.";

/**
 * 任务创建成功后调用（task-start-controller）：把首页挂着的引用按 run 存进
 * sessionStorage，运行页对话挂载时取回。文本按单份预算截断，控制存储占用。
 */
export function persistPendingTaskReferences(runId: string): void {
  if (selected.length === 0) return;
  const payload = selected.map(reference => ({
    ...reference,
    text: reference.text.slice(0, PER_REFERENCE_CHARS),
  }));
  try {
    sessionStorage.setItem(PENDING_PREFIX + runId, JSON.stringify(payload));
  } catch {
    // 会话存储不可用时引用只随当前页面存在，任务本体不受影响。
  }
  selected = [];
  renderChips();
}

/** 运行页对话挂载时调用：取回该运行的待接引用并挂成 chips（一次性消费）。 */
export function restorePendingTaskReferences(runId: string, composer: HTMLElement): void {
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(PENDING_PREFIX + runId);
    if (raw) sessionStorage.removeItem(PENDING_PREFIX + runId);
  } catch {
    return;
  }
  if (!raw) return;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return;
  }
  if (!Array.isArray(payload)) return;
  mountReferenceChips(composer);
  for (const item of payload) {
    const reference = item as ComposerReference;
    if (
      (reference?.kind === "problem" || reference?.kind === "paper" || reference?.kind === "method")
      && typeof reference.key === "string"
      && typeof reference.title === "string"
      && typeof reference.text === "string"
      && reference.text
    ) {
      addComposerReference({
        key: reference.key,
        kind: reference.kind,
        title: reference.title,
        text: reference.text,
      });
    }
  }
}
