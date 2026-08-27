/**
 * 五个流程页面的正文绑定：stage-outputs（五类页面正文契约）→ 现有 DOM 槽位。
 *
 * 原则（界面基线 ADR-0006 / AGENTS 第 5 节）：
 * - 只做内容填充与「无真实数据源的演示区」隐藏，不改页面骨架、路由与交互；
 * - 契约为 null（阶段未完成 / 模拟链路）时保留演示模板内容原样；
 * - 五个阶段面板同存于合并工作台的 DOM 中（隐藏切换），一次调用全部填充；
 * - 每个面板按 updated_at 做幂等签名，SSE 高频刷新不重复渲染；
 * - 论文编辑器让位于用户主权：用户编辑过的本机草稿（task-autosave 带
 *   user_edited 标记）绝不覆盖；新到的论文正文按打字机节奏流式呈现。
 */

import type {
  DatasetProfile,
  DeliveryManifest,
  DocumentDraft,
  ExperimentSummary,
  PlanProposal,
} from "@openmathmodel/contracts";
import { t } from "../i18n/locale";
import { renderMarkdown } from "../text/markdown";
import { typesetMath } from "../text/math-typeset";
import type { StageOutputsPayload } from "./modeling-workspace-api";

const PAPER_DRAFT_PREFIX = "openmathmodel.paperDraft.v1.";

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name: string): HTMLElement {
  const node = el("i", `ph ph-${name}`);
  node.setAttribute("aria-hidden", "true");
  return node;
}

/**
 * LLM 富文本 → 块级内容：模型产出的思路/理由/总结常带 markdown 标记与公式，
 * 直接 textContent 会把 **加粗**、`代码`、$公式$ 原样示人。渲染为受限 markdown，
 * 公式排版由各面板渲染完后统一 typesetMath 触发。
 */
function richBlock(text: string): HTMLElement {
  const host = el("div", "stage-rich-text");
  host.innerHTML = renderMarkdown(text);
  return host;
}

/** 行内富文本（列表项、结论条这类单句槽位）：解包 renderMarkdown 的外层 <p>。 */
function richInline(text: string): HTMLElement {
  const host = el("span", "stage-rich-inline");
  const block = el("div");
  block.innerHTML = renderMarkdown(text);
  if (block.childElementCount === 1 && block.firstElementChild instanceof HTMLParagraphElement) {
    host.append(...block.firstElementChild.childNodes);
  } else {
    // 罕见：单句槽位里出现多块结构（多段/列表），退回块级呈现避免内容丢失
    host.classList.add("stage-rich-multi");
    host.append(...block.childNodes);
  }
  return host;
}

/** 面板级幂等：同一份 updated_at 只渲染一次（SSE 每 80ms 可能触发刷新）。 */
function shouldRender(panel: HTMLElement, key: string, stamp: string): boolean {
  const marker = `${key}:${stamp}`;
  if (panel.dataset.stageContentStamp === marker) return false;
  panel.dataset.stageContentStamp = marker;
  panel.dataset.stageContentSource = "api";
  return true;
}

// ── 数据准备页（DatasetProfile → data-report 面板） ─────────────────────────

function renderDataPanel(root: HTMLElement, profile: DatasetProfile): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="data-report"]');
  if (!panel || !shouldRender(panel, "data", profile.updated_at)) return;

  const conclusion = panel.querySelector<HTMLElement>(".focused-conclusion-strip");
  if (conclusion) {
    conclusion.replaceChildren(icon("check-circle"), el("strong", "", `${t("数据画像")}：`), richInline(profile.profile_summary));
  }

  const fieldsTotal = profile.datasets.reduce((total, item) => total + item.fields.length, 0);
  const metrics = panel.querySelector<HTMLElement>(".focused-metrics");
  if (metrics) {
    metrics.replaceChildren(
      ...([
        [t("数据集"), String(profile.datasets.length)],
        [t("字段数"), String(fieldsTotal)],
        [t("准备步骤"), String(profile.preparation_steps.length)],
      ] as const).map(([label, value]) => {
        const article = el("article");
        article.append(el("span", "", label), el("strong", "", value));
        return article;
      }),
    );
  }

  // 「数据问题与处理建议」表 → 真实数据清单（名称/来源/字段/质量风险）
  const issueSection = panel.querySelector<HTMLElement>(".focused-section.compact:not(.raw-preview-section)");
  const issueTable = issueSection?.querySelector<HTMLElement>("table");
  if (issueSection && issueTable) {
    issueSection.querySelector("h2")?.replaceChildren(t("数据清单与质量风险"));
    const thead = el("thead");
    const headRow = el("tr");
    [t("数据集"), t("来源"), t("字段"), t("质量风险")].forEach(label => headRow.append(el("th", "", label)));
    thead.append(headRow);
    const tbody = el("tbody");
    for (const dataset of profile.datasets) {
      const row = el("tr");
      row.append(
        el("td", "", dataset.name),
        el("td", "", dataset.source),
        el("td", "", dataset.fields.join("、")),
        el("td", "", dataset.quality_risks.join("；") || "—"),
      );
      tbody.append(row);
    }
    issueTable.replaceChildren(thead, tbody);
    issueSection.hidden = profile.datasets.length === 0;
  }

  // 「原始数据预览」演示表 → 准备步骤与清洗策略（真实数据文件下发前的诚实内容）
  const preview = panel.querySelector<HTMLElement>(".raw-preview-section");
  if (preview) {
    preview.replaceChildren();
    preview.append(el("h2", "", t("准备步骤与清洗策略")));
    const steps = el("ol", "stage-step-list");
    profile.preparation_steps.forEach(step => {
      const item = el("li");
      item.append(richInline(step));
      steps.append(item);
    });
    preview.append(steps);
    const strategies = el("ul", "stage-strategy-list");
    const strategyItem = (label: string, value: string): HTMLElement => {
      const item = el("li");
      item.append(el("strong", "", `${label}：`), richInline(value));
      return item;
    };
    if (profile.missing_value_strategy) {
      strategies.append(strategyItem(t("缺失值策略"), profile.missing_value_strategy));
    }
    if (profile.outlier_strategy) {
      strategies.append(strategyItem(t("异常值策略"), profile.outlier_strategy));
    }
    if (profile.derived_features.length) {
      strategies.append(strategyItem(t("衍生变量"), profile.derived_features.join("、")));
    }
    if (strategies.childElementCount) preview.append(strategies);
    preview.hidden = false;
  }
  typesetMath(panel);
}

// ── 建模方案页（PlanProposal → model-plan 面板） ────────────────────────────

function planDetail(plan: PlanProposal["plans"][number], rationale: string | null, recommended: boolean): HTMLElement {
  const detail = el("section", "focused-plan-detail selected-plan-overview");
  const listBlock = (label: string, items: string[]): HTMLElement => {
    const block = el("div");
    const list = el("ul");
    items.forEach(text => {
      const item = el("li");
      item.append(richInline(text));
      list.append(item);
    });
    block.append(el("h2", "", label), list);
    return block;
  };

  const approach = el("div");
  approach.append(el("h2", "", t("建模思路")), richBlock(plan.approach));
  detail.append(approach);

  if (plan.steps.length) detail.append(listBlock(t("实验步骤"), plan.steps));
  if (plan.risks.length) detail.append(listBlock(t("主要风险"), plan.risks));
  if (recommended && rationale) {
    const why = el("div");
    why.append(el("h2", "", t("推荐理由")), richBlock(rationale));
    detail.append(why);
  }
  return detail;
}

function renderModelPanel(root: HTMLElement, proposal: PlanProposal): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="model-plan"]');
  if (!panel || !shouldRender(panel, "model", proposal.updated_at)) return;

  const recommendedPlan = proposal.plans.find(plan => plan.id === proposal.recommended_plan_id)
    ?? proposal.plans[0];

  // 结论条只放一句可扫读的推荐结论；完整推荐理由在下方详情的「推荐理由」栏，
  // 长理由塞进横条会把整块顶成一面文字墙。
  const strip = panel.querySelector<HTMLElement>(".focused-conclusion-strip");
  if (strip && recommendedPlan) {
    strip.replaceChildren(
      icon("check-circle"),
      el("span", "", `${t("建议采用方案")} ${recommendedPlan.id}（${recommendedPlan.name}），${t("推荐理由与风险见下方方案详情")}`),
    );
  }

  const list = panel.querySelector<HTMLElement>(".focused-plan-list");
  const detailHost = panel.querySelector<HTMLElement>(".focused-plan-detail");
  if (!list || !detailHost || !recommendedPlan) return;

  const renderDetail = (plan: PlanProposal["plans"][number]): void => {
    detailHost.replaceChildren(
      ...planDetail(plan, proposal.rationale, plan.id === proposal.recommended_plan_id).children,
    );
    typesetMath(detailHost);
  };

  list.replaceChildren(
    ...proposal.plans.map(plan => {
      const row = el("button", "focused-plan-row");
      row.type = "button";
      row.dataset.stagePlanId = plan.id;
      if (plan.id === recommendedPlan.id) row.classList.add("selected");
      const title = el("strong", "", `${t("方案")} ${plan.id} `);
      const small = el("small", "", plan.id === proposal.recommended_plan_id ? `（${t("推荐主方案")}）` : `（${t("备选方案")}）`);
      title.append(small);
      row.append(
        el("span", "plan-radio"),
        title,
        el("span", "", `${t("核心方法")}：${plan.name}`),
        el("span", "", `${t("主要风险")}：${plan.risks[0] ?? "—"}`),
      );
      row.addEventListener("click", () => {
        list.querySelectorAll(".focused-plan-row").forEach(item => item.classList.toggle("selected", item === row));
        renderDetail(plan);
      });
      return row;
    }),
  );
  renderDetail(recommendedPlan);
}

// ── 实验与验证页（ExperimentSummary → experiment-report 面板） ──────────────

const VERDICT_COPY: Record<string, { icon: string; label: string }> = {
  pass: { icon: "check-circle", label: "结果通过" },
  concerns: { icon: "warning-circle", label: "结果可用（有保留意见）" },
  fail: { icon: "x-circle", label: "结果未通过" },
};

function renderExperimentsPanel(root: HTMLElement, summary: ExperimentSummary): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="experiment-report"]');
  if (!panel || !shouldRender(panel, "experiments", summary.updated_at)) return;

  const validation = summary.validation;
  const conclusion = panel.querySelector<HTMLElement>(".focused-report-conclusion");
  if (conclusion) {
    const verdict = VERDICT_COPY[validation?.verdict ?? ""] ?? { icon: "check-circle", label: "实验已完成" };
    conclusion.replaceChildren(
      icon(verdict.icon),
      el("strong", "", `${t(verdict.label)}：`),
      richInline(validation?.validation_summary || summary.approach_summary),
    );
  }

  const metricsHost = panel.querySelector<HTMLElement>(".focused-metrics");
  if (metricsHost) {
    const entries = Object.entries(summary.metrics ?? {});
    metricsHost.hidden = entries.length === 0;
    metricsHost.replaceChildren(
      ...entries.slice(0, 6).map(([key, value]) => {
        const article = el("article");
        article.append(el("span", "", key), el("strong", "", String(value)));
        return article;
      }),
    );
  }

  // 演示图表没有真实数据序列：真实链路下隐藏，指标以上方卡片呈现
  const chart = panel.querySelector<HTMLElement>(".focused-experiment-chart");
  if (chart) chart.hidden = true;

  const notes = panel.querySelectorAll<HTMLElement>(".focused-experiment-notes article");
  const robustness = notes[0];
  if (robustness) {
    const list = el("ul");
    const noteItem = (iconName: string, label: string, detail: string): HTMLElement => {
      const item = el("li");
      const body = richInline(detail);
      body.prepend(el("strong", "", `${label}：`));
      item.append(icon(iconName), body);
      return item;
    };
    for (const check of validation?.checks ?? []) {
      list.append(noteItem(
        check.result === "pass" ? "check-circle" : check.result === "warn" ? "warning-circle" : "x-circle",
        check.name,
        check.note,
      ));
    }
    for (const risk of validation?.risks ?? []) {
      list.append(noteItem("shield-warning", t("风险"), risk));
    }
    robustness.replaceChildren(el("h2", "", t("稳健性与风险结论")), list);
    robustness.hidden = list.childElementCount === 0;
  }
  const advice = notes[1];
  if (advice) {
    advice.replaceChildren(el("h2", "", t("实现思路")), richBlock(summary.approach_summary));
  }
  typesetMath(panel);
}

// ── 论文编辑页（DocumentDraft → 编辑器正文；用户草稿优先，新到正文流式呈现） ──

/** 只有用户亲手编辑过的现场才算本机草稿（task-autosave 落盘时带 user_edited
 *  标记）；旧记录或 Agent/模板快照没有该标记，不阻止真实论文正文的渲染。 */
function hasUserPaperDraft(): boolean {
  try {
    // 作用域推导与 task-autosave 的 urlScope 一致：非法 project_id 落到 demo 档
    const param = new URL(window.location.href).searchParams.get("project_id") ?? "";
    const projectId = /^proj_[0-9a-f]{32}$/.test(param) ? param : "demo";
    const raw = localStorage.getItem(PAPER_DRAFT_PREFIX + projectId);
    if (!raw) return false;
    return (JSON.parse(raw) as { user_edited?: unknown }).user_edited === true;
  } catch {
    return false;
  }
}

function reduceMotion(): boolean {
  if (document.documentElement.dataset.reduceMotion === "on") return true;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

interface DraftBlock {
  element: HTMLElement;
  /** 归属章节序号（标题/摘要/关键词为 null），流式过程中用于同步大纲状态。 */
  sectionIndex: number | null;
}

function buildDraftBlocks(draft: DocumentDraft): DraftBlock[] {
  const blocks: DraftBlock[] = [];
  blocks.push({ element: el("h1", "", draft.title), sectionIndex: null });
  if (draft.abstract) {
    blocks.push({ element: el("h2", "", t("摘要")), sectionIndex: null });
    blocks.push({ element: el("p", "", draft.abstract), sectionIndex: null });
  }
  if (draft.keywords.length) {
    const keywords = el("p");
    keywords.append(el("strong", "", `${t("关键词")}：`), document.createTextNode(draft.keywords.join("；")));
    blocks.push({ element: keywords, sectionIndex: null });
  }
  draft.sections.forEach((section, index) => {
    const heading = el("h2", "", section.heading);
    heading.id = `section-${index}`;
    blocks.push({ element: heading, sectionIndex: index });
    const body = el("div");
    body.innerHTML = renderMarkdown(section.content);
    [...body.children].forEach(child => blocks.push({ element: child as HTMLElement, sectionIndex: index }));
  });
  return blocks;
}

function rebuildPaperOutline(root: HTMLElement, draft: DocumentDraft): HTMLAnchorElement[] {
  const outline = root.querySelector<HTMLElement>(".paper-editor-workspace .outline");
  if (!outline) return [];
  const heading = outline.querySelector(".outline-heading");
  const links = draft.sections.map((section, index) => {
    const link = el("a") as HTMLAnchorElement;
    link.setAttribute("href", `#section-${index}`);
    link.append(el("span", "outline-status"), document.createTextNode(section.heading));
    return link;
  });
  outline.replaceChildren(...(heading ? [heading] : []), ...links);
  return links;
}

function markOutlineDone(link: HTMLAnchorElement | undefined): void {
  const status = link?.querySelector<HTMLElement>(".outline-status");
  if (!status || status.classList.contains("done")) return;
  status.classList.add("done");
  status.replaceChildren(icon("check"));
}

function setOutlineActive(links: HTMLAnchorElement[], active: HTMLAnchorElement | undefined): void {
  links.forEach(link => link.classList.toggle("active", link === active));
}

/** 正文重建后大纲链接是新节点（bindPaperEditor 绑的旧监听随旧节点失效），
 *  在这里补回平滑滚动 + 滚动跟随高亮，行为与演示态一致。 */
const outlineSpyBound = new WeakSet<HTMLElement>();

function bindOutlineNavigation(editor: HTMLElement, links: HTMLAnchorElement[]): void {
  links.forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    setOutlineActive(links, link);
    editor.querySelector<HTMLElement>(link.getAttribute("href") ?? "")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  if (outlineSpyBound.has(editor)) return;
  outlineSpyBound.add(editor);
  let pending = false;
  editor.addEventListener("scroll", () => {
    if (pending) return;
    pending = true;
    window.requestAnimationFrame(() => {
      pending = false;
      const outline = editor.closest(".editor-layout")?.querySelector<HTMLElement>(".outline");
      const items = outline ? [...outline.querySelectorAll<HTMLAnchorElement>("a")] : [];
      const anchors = items
        .map(link => ({ link, node: editor.querySelector<HTMLElement>(link.getAttribute("href") ?? "") }))
        .filter((item): item is { link: HTMLAnchorElement; node: HTMLElement } => Boolean(item.node));
      if (!anchors.length) return;
      const threshold = editor.getBoundingClientRect().top + 96;
      let current = anchors[0];
      anchors.forEach(item => {
        if (item.node.getBoundingClientRect().top <= threshold) current = item;
      });
      setOutlineActive(items, current.link);
    });
  }, { passive: true });
}

function refreshPaperWordCount(editor: HTMLElement): void {
  const counter = editor.closest(".paper-editor")?.querySelector<HTMLElement>("[data-editor-wordcount]");
  if (!counter) return;
  const count = (editor.innerText || "").replace(/\s+/g, "").length;
  counter.textContent = `${count.toLocaleString()} 字`;
}

interface PaperStream {
  cancel: () => void;
}

const activePaperStreams = new WeakMap<HTMLElement, PaperStream>();
/** 每个（运行, 版本）只播一次流式动画：阶段间软切换来回不重播。 */
const streamedDraftKeys = new Set<string>();
/** 只对新鲜产出（论文刚写完）做流式呈现，重开历史任务时直接完整渲染。 */
const STREAM_FRESH_WINDOW_MS = 10 * 60_000;

/**
 * 把论文正文按打字机节奏渐进呈现（后端在论文节点完成时一次性给出定稿，
 * 流式是前端的呈现节奏）：块按顺序入文、文本逐字浮现、大纲随章节完成打勾；
 * 用户一旦开始编辑立即整段放行，绝不吃掉输入。
 */
function streamDraftIntoEditor(
  editor: HTMLElement,
  blocks: DraftBlock[],
  links: HTMLAnchorElement[],
  onSettled: () => void,
): void {
  interface TypingUnit { node: Text; full: string; }
  const plan = blocks.map(block => {
    const walker = document.createTreeWalker(block.element, NodeFilter.SHOW_TEXT);
    const units: TypingUnit[] = [];
    while (walker.nextNode()) {
      const node = walker.currentNode as Text;
      units.push({ node, full: node.data });
      node.data = "";
    }
    return { block, units };
  });
  const totalChars = plan.reduce(
    (sum, item) => sum + item.units.reduce((n, unit) => n + unit.full.length, 0),
    0,
  );
  // 全文约 8 秒播完：短文逐字可辨，长文自动加速，每帧至少 3 字。
  const charsPerTick = Math.max(3, Math.ceil(totalChars / 500));
  const caret = el("span", "editor-stream-caret");
  caret.setAttribute("contenteditable", "false");
  caret.setAttribute("aria-hidden", "true");

  editor.replaceChildren();
  editor.dataset.streaming = "true";
  let blockIndex = 0;
  let unitIndex = 0;
  let timer = 0;

  const finishSection = (index: number | null): void => {
    if (index !== null) markOutlineDone(links[index]);
  };
  const detach = (): void => {
    window.clearInterval(timer);
    caret.remove();
    delete editor.dataset.streaming;
    editor.removeEventListener("beforeinput", flush);
    activePaperStreams.delete(editor);
  };
  /** 用户开始编辑时：余下内容立即整段放行，再交还编辑权。 */
  const flush = (): void => {
    for (; blockIndex < plan.length; blockIndex += 1) {
      const item = plan[blockIndex];
      for (; unitIndex < item.units.length; unitIndex += 1) {
        item.units[unitIndex].node.data = item.units[unitIndex].full;
      }
      unitIndex = 0;
      if (!item.block.element.isConnected) editor.append(item.block.element);
      finishSection(item.block.sectionIndex);
    }
    setOutlineActive(links, links[0]);
    detach();
    onSettled();
  };
  activePaperStreams.set(editor, { cancel: detach });
  editor.addEventListener("beforeinput", flush);

  timer = window.setInterval(() => {
    // 跟随写入进度吸底；用户主动上滚查看时不打扰
    const stick = editor.scrollHeight - editor.scrollTop - editor.clientHeight < 140;
    let budget = charsPerTick;
    while (budget > 0 && blockIndex < plan.length) {
      const item = plan[blockIndex];
      if (!item.block.element.isConnected) {
        item.block.element.classList.add("stream-in");
        editor.append(item.block.element);
        item.block.element.insertAdjacentElement("afterend", caret);
        if (item.block.sectionIndex !== null) setOutlineActive(links, links[item.block.sectionIndex]);
      }
      const unit = item.units[unitIndex];
      if (!unit) {
        finishSection(item.block.sectionIndex);
        blockIndex += 1;
        unitIndex = 0;
        continue;
      }
      const remaining = unit.full.length - unit.node.data.length;
      const step = Math.min(budget, remaining);
      unit.node.data = unit.full.slice(0, unit.node.data.length + step);
      budget -= step;
      if (unit.node.data.length >= unit.full.length) unitIndex += 1;
    }
    if (stick) editor.scrollTop = editor.scrollHeight;
    if (blockIndex >= plan.length) {
      setOutlineActive(links, links[0]);
      detach();
      onSettled();
    }
  }, 16);
}

function renderEditorPanel(root: HTMLElement, draft: DocumentDraft): void {
  const editor = root.querySelector<HTMLElement>('.editor-page[contenteditable="true"]');
  if (!editor) return;
  // 用户主权：用户编辑过的本机草稿绝不覆盖；同一版本正文只填充一次。
  if (editor.dataset.stageDraftVersion === String(draft.version)) return;
  if (hasUserPaperDraft()) return;
  editor.dataset.stageDraftVersion = String(draft.version);
  activePaperStreams.get(editor)?.cancel();

  const blocks = buildDraftBlocks(draft);
  const links = rebuildPaperOutline(root, draft);
  bindOutlineNavigation(editor, links);

  const settle = (): void => {
    typesetMath(editor);
    refreshPaperWordCount(editor);
  };

  const streamKey = `${draft.run_id}:${draft.version}`;
  const updatedMs = new Date(draft.updated_at).getTime();
  const fresh = Number.isFinite(updatedMs) && Date.now() - updatedMs < STREAM_FRESH_WINDOW_MS;
  const animate = fresh
    && !streamedDraftKeys.has(streamKey)
    && editor.offsetParent !== null
    && !reduceMotion();
  streamedDraftKeys.add(streamKey);

  if (animate) {
    streamDraftIntoEditor(editor, blocks, links, settle);
    return;
  }
  editor.replaceChildren(...blocks.map(block => block.element));
  links.forEach(link => markOutlineDone(link));
  setOutlineActive(links, links[0]);
  settle();
}

// ── 最终成果页（DeliveryManifest → final-summary 面板） ─────────────────────

function summaryRow(label: string, content: string | HTMLElement): HTMLElement {
  const row = el("div", "summary-row");
  row.append(el("span", "", label));
  if (typeof content === "string") row.append(el("span", "", content));
  else row.append(content);
  return row;
}

function renderCompletePanel(root: HTMLElement, manifest: DeliveryManifest): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="final-summary"]');
  if (!panel || !shouldRender(panel, "complete", manifest.updated_at)) return;

  if (manifest.problem_title) {
    const name = panel.querySelector<HTMLElement>(".complete-project-name");
    if (name) name.textContent = manifest.problem_title;
  }

  const summary = panel.querySelector<HTMLElement>(".result-summary");
  if (!summary) return;
  const rows: HTMLElement[] = [];
  const metricEntries = Object.entries(manifest.key_metrics ?? {});
  if (metricEntries.length) {
    rows.push(summaryRow(t("关键指标"), metricEntries.map(([key, value]) => `${key} = ${String(value)}`).join(" / ")));
  }
  if (manifest.validation_verdict) {
    const verdict = VERDICT_COPY[manifest.validation_verdict]?.label ?? manifest.validation_verdict;
    rows.push(summaryRow(t("检验结论"), t(verdict)));
  }
  if (manifest.paper_citation) {
    const citation = el("span");
    citation.textContent = manifest.paper_citation.title
      + (manifest.paper_citation.keywords.length ? `（${manifest.paper_citation.keywords.join("；")}）` : "");
    rows.push(summaryRow(t("论文"), citation));
  }
  rows.push(summaryRow(t("产物数量"), `${manifest.artifacts.length}`));
  if (rows.length) summary.replaceChildren(...rows);
}

// ── 入口 ─────────────────────────────────────────────────────────────────────

/** 把五类正文填进对应面板；契约为 null 的阶段保留演示内容。 */
export function renderStageContent(root: HTMLElement, outputs: StageOutputsPayload): void {
  if (outputs.dataset_profile) renderDataPanel(root, outputs.dataset_profile);
  if (outputs.plan_proposal) renderModelPanel(root, outputs.plan_proposal);
  if (outputs.experiment_summary) renderExperimentsPanel(root, outputs.experiment_summary);
  if (outputs.document_draft) renderEditorPanel(root, outputs.document_draft);
  if (outputs.delivery_manifest) renderCompletePanel(root, outputs.delivery_manifest);
}
