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
import { describeRobustness, formatMetricValue } from "./experiment-notes";
import type { StageOutputsPayload } from "./modeling-workspace-api";
import { describePaperAudit, paperAuditStamp } from "./paper-audit";
import {
  IMPACT_LABELS,
  KIND_LABELS,
  STATUS_LABELS,
  describeAssumptions,
  describeSymbols,
} from "./plan-tables";

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

// ── 版面精简规则：模型产出长度不可控，页面槽位按「精华上屏、细节收纳」处理 ──
//
// R1 结论条只放第一句（完整内容悬停可见）；
// R2 表格里的字段清单渲染成「字段名」徽标，完整含义放悬停与字段说明页；
// R3 长句列表项与策略段落按行数截断，点击展开全文；
// R4 纯演示的子分页（无真实数据源）在真实数据到达后隐藏，不拿假数据示人。

/** R1：取第一句（遇句读符号截断，过长再按字数截）；返回 [上屏文本, 是否截过]。 */
function leadSentence(text: string, maxChars = 72): [string, boolean] {
  const compact = text.replace(/\s+/g, " ").trim();
  const match = /[。！？!?；;]/.exec(compact);
  let lead = match ? compact.slice(0, match.index + 1) : compact;
  if ([...lead].length > maxChars) lead = [...lead].slice(0, maxChars).join("") + "…";
  return [lead, lead !== compact];
}

/** R3：多行截断 + 点击展开收起。截断行数由 CSS 变量控制，展开态可再点回。 */
function clampExpandable(node: HTMLElement, lines: number): HTMLElement {
  node.classList.add("stage-clamp");
  node.style.setProperty("--stage-clamp-lines", String(lines));
  node.title = t("点击展开或收起全文");
  node.addEventListener("click", () => node.classList.toggle("is-open"));
  return node;
}

/** R3 收尾：渲染后把实际没溢出的条目摘掉截断态（不给短内容留误导的手型）。
 *  隐藏面板（未激活的分页）量不到高度，保留截断态等下次展开时自然生效。 */
function settleClamps(scope: HTMLElement): void {
  window.requestAnimationFrame(() => {
    scope.querySelectorAll<HTMLElement>(".stage-clamp:not(.is-open)").forEach(node => {
      if (node.offsetParent === null) return;
      if (node.scrollHeight <= node.clientHeight + 2) {
        node.classList.remove("stage-clamp");
        node.removeAttribute("title");
      }
    });
  });
}

/** R2：字段字符串「名称：说明（单位）」→ 名称与说明两段（多种分隔符兼容）。 */
function splitField(field: string): { name: string; note: string } {
  const compact = field.replace(/\s+/g, " ").trim();
  const separator = /[:：]/.exec(compact);
  if (separator && separator.index > 0 && separator.index <= 32) {
    return {
      name: compact.slice(0, separator.index).trim(),
      note: compact.slice(separator.index + 1).trim(),
    };
  }
  const paren = /[（(]/.exec(compact);
  if (paren && paren.index > 0 && paren.index <= 32) {
    return {
      name: compact.slice(0, paren.index).trim(),
      note: compact.slice(paren.index).replace(/^[（(]|[）)]$/g, "").trim(),
    };
  }
  return { name: compact, note: "" };
}

/** R2：字段名徽标墙（超出上限折叠为 +N，悬停看完整含义）。 */
function fieldChips(fields: string[], maxVisible = 10): HTMLElement {
  const host = el("span", "stage-field-chips");
  fields.slice(0, maxVisible).forEach(field => {
    const { name, note } = splitField(field);
    const chip = el("span", "stage-field-chip", name);
    chip.title = note ? `${name}：${note}` : name;
    host.append(chip);
  });
  if (fields.length > maxVisible) {
    const more = el("span", "stage-field-chip is-more", `+${fields.length - maxVisible}`);
    more.title = t("完整字段清单见「字段说明」页");
    host.append(more);
  }
  return host;
}

/** R4：隐藏子分页入口（撤走演示页，或可填充页在真实内容就绪前先藏起）。 */
function hideWorkspaceTab(workspace: HTMLElement, key: string, fallbackKey: string): void {
  const tab = workspace.querySelector<HTMLElement>(`[data-workspace-tab="${key}"]`);
  const panel = workspace.querySelector<HTMLElement>(`[data-workspace-panel="${key}"]`);
  if (tab && !tab.hidden) {
    // 用户恰好停在该分页时先切回主页面，再撤走入口
    if (tab.classList.contains("active")) {
      workspace.querySelector<HTMLElement>(`[data-workspace-tab="${fallbackKey}"]`)?.click();
    }
    tab.hidden = true;
  }
  if (panel && tab?.hidden) panel.classList.remove("active");
}

/** 可填充分页的真实内容已就绪：放出被藏起的入口。 */
function revealWorkspaceTab(root: HTMLElement, key: string): void {
  const tab = root.querySelector<HTMLElement>(`[data-workspace-tab="${key}"]`);
  if (tab) tab.hidden = false;
}

//: 各工作区的分页处置表：demo = 永远没有真实数据源的纯演示页（真实运行直接
//: 撤走）；fillable = 有真实数据源、内容就绪后由渲染器放出的页。
const STAGE_TAB_PLAN: Array<{
  workspace: string;
  primary: string;
  demo: string[];
  fillable: string[];
}> = [
  { workspace: ".data-report-workspace", primary: "data-report", demo: ["raw-data", "clean-data"], fillable: ["field-guide"] },
  // 模型假设 / 符号表自 H3 切片 2 起有真实数据源（plan-proposal.assumptions / symbols）
  { workspace: ".model-plan-workspace", primary: "model-plan", demo: [], fillable: ["assumptions", "symbols", "implementation"] },
];

/**
 * 真实运行的子分页纪律（R4）：进入真实任务即撤走纯演示分页（原始数据、
 * 清洗数据——当前契约下没有它们的真实数据源），可填充分页（字段说明、
 * 模型假设、符号表、实现计划）先藏起，等对应阶段的真实内容渲染后再放出。
 * 幂等，控制器每次快照刷新都可安全调用；演示页（无运行身份）不受影响。
 */
export function prepareStageTabs(root: HTMLElement): void {
  for (const plan of STAGE_TAB_PLAN) {
    const workspace = root.querySelector<HTMLElement>(plan.workspace);
    if (!workspace) continue;
    for (const key of plan.demo) hideWorkspaceTab(workspace, key, plan.primary);
    for (const key of plan.fillable) {
      const filled = workspace.querySelector<HTMLElement>(`[data-workspace-panel="${key}"]`)
        ?.dataset.stageContentSource === "api";
      if (!filled) hideWorkspaceTab(workspace, key, plan.primary);
    }
  }
}

// ── 数据准备页（DatasetProfile → data-report 面板） ─────────────────────────

function renderDataPanel(root: HTMLElement, profile: DatasetProfile): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="data-report"]');
  if (!panel || !shouldRender(panel, "data", profile.updated_at)) return;

  const conclusion = panel.querySelector<HTMLElement>(".focused-conclusion-strip");
  if (conclusion) {
    // R1：结论条只放画像的第一句，完整画像悬停可见（长段落会把横条顶成文字墙）
    const [lead, trimmed] = leadSentence(profile.profile_summary);
    conclusion.replaceChildren(icon("check-circle"), el("strong", "", `${t("数据画像")}：`), richInline(lead));
    conclusion.title = trimmed ? profile.profile_summary : "";
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

  // 「数据问题与处理建议」表 → 真实数据清单（名称/来源/字段/质量风险）。
  // R2/R3：字段列渲染成字段名徽标（含义悬停 + 字段说明页），风险列逐条截断可展开，
  // 长文本不再把表格行撑爆。
  const issueSection = panel.querySelector<HTMLElement>(".focused-section.compact:not(.raw-preview-section)");
  const issueTable = issueSection?.querySelector<HTMLElement>("table");
  if (issueSection && issueTable) {
    issueTable.classList.remove("issue-table");
    issueTable.classList.add("stage-dataset-table");
    issueSection.querySelector("h2")?.replaceChildren(t("数据清单与质量风险"));
    const thead = el("thead");
    const headRow = el("tr");
    [t("数据集"), t("来源"), t("字段"), t("质量风险")].forEach(label => headRow.append(el("th", "", label)));
    thead.append(headRow);
    const tbody = el("tbody");
    for (const dataset of profile.datasets) {
      const row = el("tr");
      const risks = el("td", "stage-risk-cell");
      if (dataset.quality_risks.length === 0) risks.textContent = "—";
      dataset.quality_risks.forEach(risk => {
        risks.append(clampExpandable(el("div", "stage-risk-item", risk), 2));
      });
      const fieldsCell = el("td", "stage-fields-cell");
      fieldsCell.append(fieldChips(dataset.fields));
      row.append(
        el("td", "", dataset.name),
        el("td", "", dataset.source),
        fieldsCell,
        risks,
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
      const body = el("span", "stage-strategy-body");
      body.append(richInline(value));
      // R3：策略是最容易长成一面墙的槽位，默认三行截断、点击展开
      clampExpandable(body, 3);
      item.append(el("strong", "", `${label}：`), body);
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

  // 字段说明页用真实字段填充（纯演示分页由 prepareStageTabs 统一撤走）
  renderFieldGuidePanel(root, profile);
  settleClamps(panel);
  typesetMath(panel);
}

/** 「字段说明」分页：数据清单里的真实字段逐条列成表（名称/说明/所属数据集）。 */
function renderFieldGuidePanel(root: HTMLElement, profile: DatasetProfile): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="field-guide"]');
  if (!panel) return;
  const fieldsTotal = profile.datasets.reduce((total, item) => total + item.fields.length, 0);
  if (fieldsTotal === 0) return;
  panel.dataset.stageContentSource = "api";

  const header = el("header", "focused-template-heading");
  const heading = el("div");
  heading.append(el("h1", "", t("字段说明")), el("p", "", t("来自数据准备阶段的真实字段清单")));
  header.append(heading, el("span", "focused-template-status neutral", `${fieldsTotal} ${t("个字段")}`));

  const section = el("section", "focused-template-section");
  const wrap = el("div", "focused-table-wrap");
  const table = el("table", "focused-table focused-template-table stage-field-table");
  const thead = el("thead");
  const headRow = el("tr");
  [t("字段"), t("说明"), t("所属数据集"), t("来源")].forEach(label => headRow.append(el("th", "", label)));
  thead.append(headRow);
  const tbody = el("tbody");
  for (const dataset of profile.datasets) {
    for (const field of dataset.fields) {
      const { name, note } = splitField(field);
      const row = el("tr");
      const nameCell = el("td");
      nameCell.append(el("strong", "", name));
      row.append(
        nameCell,
        el("td", "", note || "—"),
        el("td", "", dataset.name),
        el("td", "", dataset.source),
      );
      tbody.append(row);
    }
  }
  table.append(thead, tbody);
  wrap.append(table);
  section.append(wrap);

  const template = el("section", "focused-template");
  template.append(header, section);
  panel.replaceChildren(template);
  revealWorkspaceTab(root, "field-guide");
}

// ── 建模方案页（PlanProposal → model-plan 面板） ────────────────────────────

function planDetail(plan: PlanProposal["plans"][number], rationale: string | null, recommended: boolean): HTMLElement {
  const detail = el("section", "focused-plan-detail selected-plan-overview");
  const listBlock = (label: string, items: string[], clampLines?: number): HTMLElement => {
    const block = el("div");
    const list = el("ul");
    items.forEach(text => {
      const item = el("li");
      // R3：真实产出的步骤/风险可能是成段长文，默认截断、点击展开。
      // 截断体包在内层（li 直接上 -webkit-box 会丢列表圆点）。
      const body = el("span", "stage-clamp-body");
      body.append(richInline(text));
      if (clampLines) clampExpandable(body, clampLines);
      item.append(body);
      list.append(item);
    });
    block.append(el("h2", "", label), list);
    return block;
  };

  const approach = el("div");
  approach.append(el("h2", "", t("建模思路")), richBlock(plan.approach));
  detail.append(approach);

  if (plan.steps.length) detail.append(listBlock(t("实验步骤"), plan.steps, 3));
  if (plan.risks.length) detail.append(listBlock(t("主要风险"), plan.risks, 3));
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
    settleClamps(detailHost);
    typesetMath(detailHost);
  };

  // 行结构与演示行同构（radio + 标题 + 三个摘要 + 展开箭头）：
  // 网格模板是 6 列，少一个子项会让「主要风险」掉进最窄的列、行尾留空。
  list.replaceChildren(
    ...proposal.plans.map(plan => {
      const row = el("button", "focused-plan-row");
      row.type = "button";
      row.dataset.stagePlanId = plan.id;
      const selected = plan.id === recommendedPlan.id;
      if (selected) row.classList.add("selected");
      const title = el("strong", "", `${t("方案")} ${plan.id} `);
      const small = el("small", "", plan.id === proposal.recommended_plan_id ? `（${t("推荐主方案")}）` : `（${t("备选方案")}）`);
      title.append(small);
      // R1：行内摘要只放第一句，全文在下方方案详情（三行截断兜底仍在 CSS）
      const [riskLead] = leadSentence(plan.risks[0] ?? "—", 48);
      const riskCell = el("span", "", `${t("主要风险")}：${riskLead}`);
      if (plan.risks[0]) riskCell.title = plan.risks.join("\n");
      row.append(
        el("span", "plan-radio"),
        title,
        el("span", "", `${t("核心方法")}：${plan.name}`),
        el("span", "", `${t("实验步骤")}：${plan.steps.length}`),
        riskCell,
        icon(selected ? "caret-up" : "caret-down"),
      );
      row.addEventListener("click", () => {
        list.querySelectorAll(".focused-plan-row").forEach(item => {
          const on = item === row;
          item.classList.toggle("selected", on);
          const chevron = item.querySelector<HTMLElement>(":scope > i");
          if (chevron) chevron.className = `ph ph-caret-${on ? "up" : "down"}`;
        });
        renderDetail(plan);
      });
      return row;
    }),
  );
  renderDetail(recommendedPlan);

  // 三个子分页都由方案契约填充：假设表 / 符号表来自归约后的规范化（缺席时
  // 分页保持藏起），实现计划用推荐方案的真实步骤
  renderAssumptionsPanel(root, proposal);
  renderSymbolsPanel(root, proposal);
  renderImplementationPanel(root, proposal, recommendedPlan);
}

/** 可填充分页的真实内容这次没有到（规范化失败 / 旧运行）：清空并收回入口。 */
function withdrawWorkspaceTab(root: HTMLElement, workspaceSelector: string, key: string, primary: string): void {
  const workspace = root.querySelector<HTMLElement>(workspaceSelector);
  const panel = workspace?.querySelector<HTMLElement>(`[data-workspace-panel="${key}"]`);
  if (!workspace || !panel) return;
  delete panel.dataset.stageContentSource;
  panel.replaceChildren();
  hideWorkspaceTab(workspace, key, primary);
}

/** 「方案 A」/「全局」/「共享」这类归属列的文案（sharedLabel 已翻译）。 */
function scopeLabel(planId: string | null, sharedLabel: string): string {
  return planId === null ? sharedLabel : `${t("方案")} ${planId}`;
}

/** 表头 + 表体的骨架（沿用演示表格的类名，样式无需新增）。 */
function focusedTable(className: string, headers: string[]): { table: HTMLTableElement; tbody: HTMLTableSectionElement } {
  const table = el("table", `focused-table focused-template-table ${className}`);
  const thead = el("thead");
  const headRow = el("tr");
  headers.forEach(label => headRow.append(el("th", "", label)));
  thead.append(headRow);
  const tbody = el("tbody");
  table.append(thead, tbody);
  return { table, tbody };
}

/**
 * 「模型假设」分页：全局假设 + 方案特定假设六列表（编号 / 假设 / 适用范围 /
 * 依据 / 影响 / 状态），底部「验证重点」点名待检验与重点验证的条目——这是
 * 用户在 G1 决策卡前核对方案前提的地方，也是论文「模型假设」一节的底稿。
 */
function renderAssumptionsPanel(root: HTMLElement, proposal: PlanProposal): void {
  const section = describeAssumptions(proposal);
  if (section.kind === "absent") {
    withdrawWorkspaceTab(root, ".model-plan-workspace", "assumptions", "model-plan");
    return;
  }
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="assumptions"]');
  if (!panel) return;
  panel.dataset.stageContentSource = "api";

  const header = el("header", "focused-template-heading");
  const heading = el("div");
  heading.append(
    el("h1", "", t("模型假设")),
    el("p", "", `${section.globalCount} ${t("项全局假设")} · ${section.planCount} ${t("项方案特定假设")}`),
  );
  header.append(heading, el("span", "focused-template-status neutral", `${section.rows.length} ${t("项")}`));

  const notice = el("div", "focused-conclusion-strip focused-template-notice");
  if (section.focus.length === 0) {
    notice.append(icon("check-circle"), el("span", "", t("全部假设均由题面或数据直接支持。")));
  } else {
    notice.append(
      icon("warning-circle"),
      el("span", "", `${section.focus.length} ${t("项假设待检验或需重点验证，实验阶段将据此安排敏感性与稳健性检验。")}`),
    );
  }

  const { table, tbody } = focusedTable("assumption-table", [
    "#", t("假设"), t("适用范围"), t("依据"), t("影响"), t("状态"),
  ]);
  section.rows.forEach(row => {
    const tr = el("tr");
    const text = el("td");
    text.append(richInline(row.text));
    const status = el("td", "", t(STATUS_LABELS[row.status]));
    status.dataset.stageAssumptionStatus = row.status;
    tr.append(
      el("td", "", row.id),
      text,
      el("td", "", scopeLabel(row.planId, t("全局"))),
      el("td", "", row.basis || "—"),
      el("td", "", t(IMPACT_LABELS[row.impact])),
      status,
    );
    tbody.append(tr);
  });
  const wrap = el("div", "focused-table-wrap");
  wrap.append(table);
  const tableSection = el("section", "focused-template-section");
  tableSection.append(wrap);

  const template = el("section", "focused-template");
  template.append(header, notice, tableSection);
  if (section.focus.length) {
    const callout = el("footer", "focused-template-callout");
    callout.append(
      el("strong", "", t("验证重点")),
      el("span", "", `${t("建议围绕以下假设做敏感性与稳健性检验：")}${section.focus.join("、")}`),
    );
    template.append(callout);
  }
  panel.replaceChildren(template);
  revealWorkspaceTab(root, "assumptions");
  typesetMath(panel);
}

/**
 * 「符号表」分页：共享符号 + 方案专有符号六列表（符号 / 类型 / 定义 / 单位 /
 * 范围 / 适用方案）。符号是不带 $ 的 LaTeX，这里统一包成行内公式交给排版器。
 */
function renderSymbolsPanel(root: HTMLElement, proposal: PlanProposal): void {
  const section = describeSymbols(proposal);
  if (section.kind === "absent") {
    withdrawWorkspaceTab(root, ".model-plan-workspace", "symbols", "model-plan");
    return;
  }
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="symbols"]');
  if (!panel) return;
  panel.dataset.stageContentSource = "api";

  const header = el("header", "focused-template-heading");
  const heading = el("div");
  heading.append(
    el("h1", "", t("符号表")),
    el("p", "", `${section.sharedCount} ${t("个共享符号")} · ${section.planCount} ${t("个方案专有符号")}`),
  );
  header.append(heading, el("span", "focused-template-status neutral", `${section.rows.length} ${t("个符号")}`));

  const { table, tbody } = focusedTable("symbol-table", [
    t("符号"), t("类型"), t("定义"), t("单位"), t("范围"), t("适用方案"),
  ]);
  section.rows.forEach(row => {
    const tr = el("tr");
    const symbol = el("td");
    const strong = el("strong");
    strong.append(richInline(`$${row.symbol}$`));
    symbol.append(strong);
    const definition = el("td");
    definition.append(richInline(row.definition));
    tr.append(
      symbol,
      el("td", "", t(KIND_LABELS[row.kind])),
      definition,
      el("td", "", row.unit ?? "—"),
      el("td", "", row.range ?? "—"),
      el("td", "", scopeLabel(row.planId, t("共享"))),
    );
    tbody.append(tr);
  });
  const wrap = el("div", "focused-table-wrap");
  wrap.append(table);
  const footer = el("footer", "focused-template-footer");
  footer.append(
    el("span", "", `${section.rows.length} ${t("个符号")}`),
    el("span", "", t("同一含义在各方案中使用同一符号")),
  );
  const tableSection = el("section", "focused-template-section");
  tableSection.append(wrap, footer);

  const template = el("section", "focused-template");
  template.append(header, tableSection);
  panel.replaceChildren(template);
  revealWorkspaceTab(root, "symbols");
  typesetMath(panel);
}

/** 「实现计划」分页：推荐方案的实验步骤 + 主要风险 + 推荐理由（真实产出）。 */
function renderImplementationPanel(
  root: HTMLElement,
  proposal: PlanProposal,
  plan: PlanProposal["plans"][number],
): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="implementation"]');
  if (!panel || plan.steps.length === 0) return;
  panel.dataset.stageContentSource = "api";

  const header = el("header", "focused-template-heading");
  const heading = el("div");
  heading.append(
    el("h1", "", t("实现计划")),
    el("p", "", `${t("来自方案")} ${plan.id}（${plan.name}）`),
  );
  header.append(heading, el("span", "focused-template-status neutral", `${plan.steps.length} ${t("步")}`));

  const stepsSection = el("section", "focused-template-section");
  const stepsTitle = el("div", "focused-template-section-title");
  stepsTitle.append(el("h2", "", t("实验步骤")), el("span", "", t("按执行顺序")));
  const steps = el("ol", "stage-step-list");
  plan.steps.forEach(step => {
    const item = el("li");
    item.append(richInline(step));
    steps.append(item);
  });
  stepsSection.append(stepsTitle, steps);

  const template = el("section", "focused-template");
  template.append(header, stepsSection);

  if (plan.risks.length) {
    const risksSection = el("section", "focused-template-section");
    const risksTitle = el("div", "focused-template-section-title");
    risksTitle.append(el("h2", "", t("主要风险")));
    const risks = el("ul", "stage-strategy-list");
    plan.risks.forEach(risk => {
      const item = el("li");
      item.append(richInline(risk));
      risks.append(item);
    });
    risksSection.append(risksTitle, risks);
    template.append(risksSection);
  }

  if (proposal.rationale && plan.id === proposal.recommended_plan_id) {
    const callout = el("footer", "focused-template-callout");
    callout.append(el("strong", "", t("推荐理由")), el("span", "", proposal.rationale));
    template.append(callout);
  }
  panel.replaceChildren(template);
  revealWorkspaceTab(root, "implementation");
  typesetMath(panel);
}

// ── 实验与验证页（ExperimentSummary → experiment-report 面板） ──────────────

const VERDICT_COPY: Record<string, { icon: string; label: string; tone: string }> = {
  pass: { icon: "check-circle", label: "结果通过", tone: "pass" },
  concerns: { icon: "warning-circle", label: "结果可用（有保留意见）", tone: "warn" },
  fail: { icon: "x-circle", label: "结果未通过", tone: "fail" },
};

function renderExperimentsPanel(root: HTMLElement, summary: ExperimentSummary): void {
  const panel = root.querySelector<HTMLElement>('[data-workspace-panel="experiment-report"]');
  if (!panel || !shouldRender(panel, "experiments", summary.updated_at)) return;

  const validation = summary.validation;
  const conclusion = panel.querySelector<HTMLElement>(".focused-report-conclusion");
  if (conclusion) {
    const verdict = VERDICT_COPY[validation?.verdict ?? ""]
      ?? { icon: "check-circle", label: "实验已完成", tone: "pass" };
    // 结论条按判定结果着色：未通过是红叉、保留意见是黄色警示，不能一律绿色
    conclusion.classList.remove("is-pass", "is-warn", "is-fail");
    conclusion.classList.add(`is-${verdict.tone}`);
    // R1：结论条只放第一句，完整结论悬停可见（详情在下方稳健性小节）
    const summaryText = validation?.validation_summary || summary.approach_summary;
    const [lead, trimmed] = leadSentence(summaryText);
    conclusion.replaceChildren(
      icon(verdict.icon),
      el("strong", "", `${t(verdict.label)}：`),
      richInline(lead),
    );
    conclusion.title = trimmed ? summaryText : "";
  }

  const metricsHost = panel.querySelector<HTMLElement>(".focused-metrics");
  if (metricsHost) {
    const entries = Object.entries(summary.metrics ?? {});
    metricsHost.hidden = entries.length === 0;
    metricsHost.replaceChildren(
      ...entries.slice(0, 6).map(([key, value]) => {
        const article = el("article");
        // 指标名来自实验代码的原始键：下划线换空格便于折行，数值做千分位格式化
        article.append(
          el("span", "", key.replace(/_/g, " ")),
          el("strong", "", formatMetricValue(value)),
        );
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
    const heading: HTMLElement[] = [el("h2", "", t("稳健性与风险结论"))];
    const list = el("ul");
    const noteItem = (tone: string, iconName: string, label: string, detail: string): HTMLElement => {
      const item = el("li", `is-${tone}`);
      const body = richInline(detail);
      body.prepend(el("strong", "", `${label}：`));
      // R3：检查备注可能成段，默认三行截断、点击展开
      clampExpandable(body, 3);
      item.append(icon(iconName), body);
      return item;
    };
    // 沙盒复跑的稳健性检查放最前：数字来自检验脚本的标记行（G3 结果采用闸门的
    // 判定依据），一句话结论与论文引用的是同一句；没跑成 / 没跑时把原因摆出来，
    // 不让「未执行」看起来像「全过」。评审判读（模型给出）与风险列在其后。
    const rerun = describeRobustness(validation?.robustness);
    if (rerun.kind === "executed") {
      if (rerun.summary) {
        const summary = el("p");
        summary.append(richInline(rerun.summary));
        heading.push(summary);
      }
      for (const row of rerun.rows) {
        const facts = [t(row.tone === "pass" ? "通过" : "未通过")];
        if (row.value !== null) facts.push(`${t("实测")} ${row.value}`);
        if (row.threshold !== null) facts.push(`${t("阈值")} ${row.threshold}`);
        const detail = row.detail ? `${facts.join("｜")} — ${row.detail}` : facts.join("｜");
        list.append(noteItem(
          row.tone,
          row.tone === "pass" ? "check-circle" : "x-circle",
          row.name,
          detail,
        ));
      }
    } else if (rerun.kind === "unfinished") {
      list.append(noteItem("warn", "warning-circle", t("稳健性复跑未完成"), rerun.summary));
    } else if (rerun.kind === "skipped") {
      list.append(noteItem("warn", "warning-circle", t("稳健性复跑未执行"), rerun.reason));
    }
    for (const check of validation?.checks ?? []) {
      const tone = check.result === "pass" ? "pass" : check.result === "warn" ? "warn" : "fail";
      list.append(noteItem(
        tone,
        tone === "pass" ? "check-circle" : tone === "warn" ? "warning-circle" : "x-circle",
        check.name,
        check.note,
      ));
    }
    for (const risk of validation?.risks ?? []) {
      list.append(noteItem("warn", "shield-warning", t("风险"), risk));
    }
    robustness.replaceChildren(...heading, list);
    robustness.hidden = list.childElementCount === 0;
  }
  const advice = notes[1];
  if (advice) {
    advice.replaceChildren(el("h2", "", t("实现思路")), richBlock(summary.approach_summary));
  }
  settleClamps(panel);
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
  /** 归属大纲条目序号（正文标题/关键词行为 null），流式过程中用于同步大纲状态。 */
  outlineIndex: number | null;
}

interface OutlineEntry {
  href: string;
  label: string;
}

/** 正文块 + 大纲条目（摘要与各章节；与优秀论文一致，摘要进大纲）。 */
function buildDraftBlocks(draft: DocumentDraft): { blocks: DraftBlock[]; entries: OutlineEntry[] } {
  const blocks: DraftBlock[] = [];
  const entries: OutlineEntry[] = [];
  blocks.push({ element: el("h1", "", draft.title), outlineIndex: null });
  if (draft.abstract) {
    const abstractIndex = entries.length;
    entries.push({ href: "#section-abstract", label: t("摘要") });
    const heading = el("h2", "paper-abstract-heading", t("摘要"));
    heading.id = "section-abstract";
    blocks.push({ element: heading, outlineIndex: abstractIndex });
    blocks.push({ element: el("p", "paper-abstract", draft.abstract), outlineIndex: abstractIndex });
    if (draft.keywords.length) {
      const keywords = el("p", "paper-keywords");
      keywords.append(el("strong", "", `${t("关键词")}：`), document.createTextNode(draft.keywords.join("；")));
      blocks.push({ element: keywords, outlineIndex: abstractIndex });
    }
  } else if (draft.keywords.length) {
    const keywords = el("p", "paper-keywords");
    keywords.append(el("strong", "", `${t("关键词")}：`), document.createTextNode(draft.keywords.join("；")));
    blocks.push({ element: keywords, outlineIndex: null });
  }
  draft.sections.forEach((section, index) => {
    const outlineIndex = entries.length;
    entries.push({ href: `#section-${index}`, label: section.heading });
    const heading = el("h2", "", section.heading);
    heading.id = `section-${index}`;
    blocks.push({ element: heading, outlineIndex });
    const body = el("div");
    body.innerHTML = renderMarkdown(section.content);
    [...body.children].forEach(child => blocks.push({ element: child as HTMLElement, outlineIndex }));
  });
  return { blocks, entries };
}

function rebuildPaperOutline(root: HTMLElement, entries: OutlineEntry[]): HTMLAnchorElement[] {
  const outline = root.querySelector<HTMLElement>(".paper-editor-workspace .outline");
  if (!outline) return [];
  const heading = outline.querySelector(".outline-heading");
  const links = entries.map(entry => {
    const link = el("a") as HTMLAnchorElement;
    link.setAttribute("href", entry.href);
    link.append(el("span", "outline-status"), document.createTextNode(entry.label));
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
    // 兄弟链接在点击时现查：分章直播会陆续补挂新链接，闭包快照会漏掉它们
    const siblings = [...(link.closest(".outline")?.querySelectorAll<HTMLAnchorElement>("a") ?? [])];
    setOutlineActive(siblings.length ? siblings : links, link);
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
  /** 立即整段放行余下内容并触发收尾（追加新章前对上一章调用）。 */
  flush: () => void;
}

const activePaperStreams = new WeakMap<HTMLElement, PaperStream>();
/** 每个（运行, 版本）只播一次流式动画：阶段间软切换来回不重播。 */
const streamedDraftKeys = new Set<string>();
/** 只对新鲜产出（论文刚写完）做流式呈现，重开历史任务时直接完整渲染。 */
const STREAM_FRESH_WINDOW_MS = 10 * 60_000;

interface StreamOptions {
  /** true（默认）= 清空编辑器整篇播放；false = 追加模式（分章直播）。 */
  replace?: boolean;
  /** 播完后大纲高亮落点：first = 回到首项（整篇模式）；keep = 停在当前章。 */
  activeOnSettle?: "first" | "keep";
}

/**
 * 把论文正文按打字机节奏渐进呈现：块按顺序入文、文本逐字浮现、大纲随章节
 * 完成打勾；用户一旦开始编辑立即整段放行，绝不吃掉输入。
 * 整篇模式（replace）用于定稿一次性到达；追加模式用于分章直播
 * （run.log 的 paper_section 事件逐章推送）。
 */
function streamDraftIntoEditor(
  editor: HTMLElement,
  blocks: DraftBlock[],
  links: HTMLAnchorElement[],
  onSettled: () => void,
  options: StreamOptions = {},
): void {
  const { replace = true, activeOnSettle = "first" } = options;
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

  if (replace) editor.replaceChildren();
  editor.dataset.streaming = "true";
  let blockIndex = 0;
  let unitIndex = 0;
  let timer = 0;

  const finishSection = (index: number | null): void => {
    if (index !== null) markOutlineDone(links[index]);
  };
  const activateSection = (index: number | null): void => {
    if (index !== null) setOutlineActive(links, links[index]);
  };
  const settleActive = (): void => {
    if (activeOnSettle === "first") setOutlineActive(links, links[0]);
  };
  const detach = (): void => {
    window.clearInterval(timer);
    caret.remove();
    delete editor.dataset.streaming;
    editor.removeEventListener("beforeinput", flush);
    activePaperStreams.delete(editor);
  };
  /** 用户开始编辑（或下一章到达）时：余下内容立即整段放行，再交还控制权。 */
  const flush = (): void => {
    for (; blockIndex < plan.length; blockIndex += 1) {
      const item = plan[blockIndex];
      for (; unitIndex < item.units.length; unitIndex += 1) {
        item.units[unitIndex].node.data = item.units[unitIndex].full;
      }
      unitIndex = 0;
      if (!item.block.element.isConnected) editor.append(item.block.element);
      finishSection(item.block.outlineIndex);
    }
    settleActive();
    detach();
    onSettled();
  };
  activePaperStreams.set(editor, { cancel: detach, flush });
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
        activateSection(item.block.outlineIndex);
      }
      const unit = item.units[unitIndex];
      if (!unit) {
        finishSection(item.block.outlineIndex);
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
      settleActive();
      detach();
      onSettled();
    }
  }, 16);
}

// ── 分章直播（run.log 的 paper_outline / paper_section 事件 → 编辑器实时上屏） ──

interface LivePaperState {
  total: number;
  /** 已上屏的章节序号（1 起），SSE 重连重放靠它去重。 */
  rendered: Set<number>;
}

const livePaperState = new WeakMap<HTMLElement, LivePaperState>();

function liveEditor(root: HTMLElement): HTMLElement | null {
  const editor = root.querySelector<HTMLElement>('.editor-page[contenteditable="true"]');
  if (!editor) return null;
  // 终稿已渲染（重看历史任务时事件重放）或用户已有草稿：直播一律不动编辑器
  if (editor.dataset.stageDraftVersion !== undefined) return null;
  if (hasUserPaperDraft()) return null;
  return editor;
}

/** 论文骨架已定（paper_outline 事件）：清空演示正文，大纲预挂全部章节为待写。 */
export function preparePaperOutline(
  root: HTMLElement,
  payload: { total: number; headings: string[] },
): void {
  const editor = liveEditor(root);
  if (!editor || livePaperState.has(editor)) return;
  livePaperState.set(editor, { total: payload.total, rendered: new Set() });
  const entries = payload.headings.map((label, index) => ({
    href: `#section-${index}`,
    label,
  }));
  const links = rebuildPaperOutline(root, entries);
  bindOutlineNavigation(editor, links);
  editor.replaceChildren();
  refreshPaperWordCount(editor);
}

/** 单章完成（paper_section 事件）：正文按打字机节奏追加，大纲随章打勾。 */
export function appendPaperSection(
  root: HTMLElement,
  payload: { index: number; total: number; heading: string; content: string },
  animate: boolean,
): void {
  const editor = liveEditor(root);
  if (!editor) return;
  let state = livePaperState.get(editor);
  if (!state) {
    // 没收到骨架事件（重连从中段开始等）：以本章信息补建直播状态
    state = { total: payload.total, rendered: new Set() };
    livePaperState.set(editor, state);
    editor.replaceChildren();
  }
  if (state.rendered.has(payload.index)) return;
  state.rendered.add(payload.index);

  // 大纲缺项时按需补挂（骨架事件丢失的兜底），保持 href 与终稿一致
  const outline = root.querySelector<HTMLElement>(".paper-editor-workspace .outline");
  if (outline) {
    const existing = outline.querySelectorAll("a").length;
    for (let index = existing; index < payload.index; index += 1) {
      const link = el("a") as HTMLAnchorElement;
      link.setAttribute("href", `#section-${index}`);
      link.append(el("span", "outline-status"), document.createTextNode(
        index === payload.index - 1 ? payload.heading : `第 ${index + 1} 章`,
      ));
      outline.append(link);
      bindOutlineNavigation(editor, [link]);
    }
  }
  const links = outline ? [...outline.querySelectorAll<HTMLAnchorElement>("a")] : [];

  const outlineIndex = payload.index - 1;
  const heading = el("h2", "", payload.heading);
  heading.id = `section-${outlineIndex}`;
  const blocks: DraftBlock[] = [{ element: heading, outlineIndex }];
  const body = el("div");
  body.innerHTML = renderMarkdown(payload.content);
  [...body.children].forEach(child => blocks.push({ element: child as HTMLElement, outlineIndex }));

  const settle = (): void => {
    typesetMath(editor);
    refreshPaperWordCount(editor);
  };
  // 上一章还在打字：先整段放行，直播永远按章节顺序推进
  activePaperStreams.get(editor)?.flush();
  if (animate && editor.offsetParent !== null && !reduceMotion()) {
    streamDraftIntoEditor(editor, blocks, links, settle, { replace: false, activeOnSettle: "keep" });
    return;
  }
  blocks.forEach(block => editor.append(block.element));
  markOutlineDone(links[outlineIndex]);
  setOutlineActive(links, links[outlineIndex]);
  settle();
}

function renderEditorPanel(root: HTMLElement, draft: DocumentDraft): void {
  const editor = root.querySelector<HTMLElement>('.editor-page[contenteditable="true"]');
  if (!editor) return;
  // 用户主权：用户编辑过的本机草稿绝不覆盖；同一版本正文只填充一次。
  if (editor.dataset.stageDraftVersion === String(draft.version)) return;
  if (hasUserPaperDraft()) return;
  editor.dataset.stageDraftVersion = String(draft.version);
  activePaperStreams.get(editor)?.cancel();

  const { blocks, entries } = buildDraftBlocks(draft);
  const links = rebuildPaperOutline(root, entries);
  bindOutlineNavigation(editor, links);

  const settle = (): void => {
    typesetMath(editor);
    refreshPaperWordCount(editor);
  };

  // 分章直播已经把各章打字上屏：终稿只需一次性补齐标题/摘要/关键词并对齐
  // 全文（内容与直播一致），不再重播整篇动画。
  const live = livePaperState.get(editor);
  const liveRendered = Boolean(live && live.rendered.size > 0);
  livePaperState.delete(editor);

  const streamKey = `${draft.run_id}:${draft.version}`;
  const updatedMs = new Date(draft.updated_at).getTime();
  const fresh = Number.isFinite(updatedMs) && Date.now() - updatedMs < STREAM_FRESH_WINDOW_MS;
  const animate = fresh
    && !liveRendered
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

// ── 论文页「数字审计」条（DocumentDraft.frozen_numbers / audit_findings → 编辑器上方） ──
//
// G4 定稿交付闸门的证据面：卡片 title 只点得出前两处发现，完整清单与发现在契约字段里。
// 条挂在 article.paper-editor 的工具栏与纸面之间——不进 contenteditable 纸面（不会被本机
// 草稿保存 / 导出带走），也不受「用户草稿优先」早退影响（审计说的是智能体这一版）。
// 原生 <details>：一行结论默认收起，展开看发现与清单，不引入新交互模型。

const PAPER_AUDIT_CLASS = "paper-audit";

function paperAuditChip(text: string): HTMLElement {
  return el("code", "paper-audit-chip", text);
}

function renderPaperAudit(root: HTMLElement, draft: DocumentDraft): void {
  const article = root.querySelector<HTMLElement>(".paper-editor-workspace article.paper-editor");
  if (!article) return;
  const existing = article.querySelector<HTMLElement>(`:scope > .${PAPER_AUDIT_CLASS}`);
  const section = describePaperAudit(draft);
  if (section.kind === "absent") {
    // 旧运行 / 模拟节点没有审计字段：不拿空条示人；换到这类运行时把上一份的条摘掉
    existing?.remove();
    return;
  }
  const stamp = paperAuditStamp(draft);
  if (existing?.dataset.paperAuditStamp === stamp) return;

  const tone = section.kind === "findings" ? "warn" : section.kind === "clean" ? "clean" : "muted";
  const details = el("details", `${PAPER_AUDIT_CLASS} is-${tone}`);
  details.dataset.paperAuditStamp = stamp;
  // 有发现默认展开：这正是用户在 G4 要看的东西；干净时收起，不挤纸面
  details.open = section.kind === "findings";

  const summary = el("summary", "paper-audit-summary");
  const iconName = tone === "warn" ? "warning-circle" : tone === "clean" ? "check-circle" : "list-numbers";
  const rows = section.rows;
  const verdict = section.kind === "findings"
    ? `${section.findings.length} ${t("处无出处数值")}`
    : section.kind === "clean"
      ? t("正文数值全部对账通过")
      : t("未做终稿数值审计");
  summary.append(
    icon(iconName),
    el("strong", "", `${t("数字审计")}：`),
    el("span", "paper-audit-verdict", `${rows.length} ${t("项冻结数字")}，${verdict}`),
    el("span", "paper-audit-hint", t("展开查看清单与发现")),
  );
  details.append(summary);

  const body = el("div", "paper-audit-body");
  if (section.kind === "findings") {
    const list = el("ul", "paper-audit-findings");
    for (const finding of section.findings) {
      const item = el("li");
      const text = el("div");
      text.append(el("strong", "", `${finding.scope}：`));
      if (finding.kind === "unsourced_number") {
        // 数值 token 原样成 chip，用户可直接去正文里搜
        finding.numbers.forEach(number => text.append(paperAuditChip(number)));
        text.append(el("span", "paper-audit-reason", t("不在冻结清单与材料中")));
      } else {
        // 审计链后续新增的发现类型（引用 / 图表）：先按节点给的说明原样示人
        text.append(el("span", "paper-audit-reason", finding.detail || finding.kind));
      }
      item.append(icon("warning-circle"), text);
      list.append(item);
    }
    body.append(el("h4", "", t("审计发现")), list);
  }

  body.append(el("h4", "", t("数字冻结清单")));
  if (rows.length === 0) {
    body.append(el("p", "paper-audit-empty", t("上游阶段没有可冻结的数字，正文数值只能引用材料中已有的数值")));
  } else {
    const table = el("table", "paper-audit-table");
    const thead = el("thead");
    const head = el("tr");
    for (const label of ["编号", "数值", "含义", "出处"]) head.append(el("th", "", t(label)));
    thead.append(head);
    const tbody = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      const value = el("td", "paper-audit-value");
      value.append(paperAuditChip(row.value));
      tr.append(
        el("td", "paper-audit-id", row.id),
        value,
        el("td", "", row.label),
        el("td", "paper-audit-source", `${t(row.stage)} · ${row.path}`),
      );
      tbody.append(tr);
    }
    table.append(thead, tbody);
    body.append(table);
  }
  body.append(el("p", "paper-audit-note", t("口径：正文数值须来自冻结清单或输入材料（题面常数靠材料放行）；一位数不计。")));
  details.append(body);

  const toolbar = article.querySelector<HTMLElement>(":scope > .editor-toolbar");
  if (existing) existing.replaceWith(details);
  else if (toolbar) toolbar.after(details);
  else article.prepend(details);
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
  if (outputs.document_draft) {
    renderPaperAudit(root, outputs.document_draft);
    renderEditorPanel(root, outputs.document_draft);
  }
  if (outputs.delivery_manifest) renderCompletePanel(root, outputs.delivery_manifest);
}
