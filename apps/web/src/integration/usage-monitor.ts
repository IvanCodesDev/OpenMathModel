/**
 * 设置中心「用量监控」与服务端 /api/usage 的对接。
 *
 * 数据来自服务端 llm_usage_records（对话、Agent 任务、测试连接、Auto 路由判定
 * 四类调用各记一条）；费用为按单价表的估算值，页面文案已声明。三个预算项
 * （月度预算/提醒阈值/硬限制）存服务端 users.usage_settings：暂停付费模型的
 * 闸门在服务端调用路径上，改本机缓存绕不过。
 */

import { ApiError, authApi, type UsageSettings, type UsageSummary } from "../auth/api";
import { sendDesktopNotification } from "../notifications/desktop-notifications";
import { notifyBudgetEnabled } from "../preferences/privacy-preferences";
import { t } from "../i18n/locale";

function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch] as string
  ));
}

/** Token 数的短格式：2.84M / 96k / 512。 */
export function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(Math.round(value));
}

function formatCny(value: number): string {
  return `¥ ${value.toFixed(2)}`;
}

/** "2026-08-03" → "8月3日"（柱状图 tooltip 与日期范围都用）。 */
function monthDayLabel(iso: string): string {
  const [, month, day] = iso.split("-");
  return `${Number(month)}月${Number(day)}日`;
}

function setText(backdrop: HTMLElement, selector: string, text: string): void {
  const node = backdrop.querySelector(selector);
  if (node) node.textContent = text;
}

/** 「较上月」的注脚：无上月数据时如实说明，避免编造百分比。 */
function tokensNote(summary: UsageSummary): string {
  const current = summary.totals.total_tokens;
  const previous = summary.previous.total_tokens;
  if (current === 0 && previous === 0) return "本月暂无调用";
  if (previous === 0) return "上月无用量";
  const delta = ((current - previous) / previous) * 100;
  const sign = delta >= 0 ? "+" : "";
  return `较上月 ${sign}${delta.toFixed(1)}%`;
}

function costNote(summary: UsageSummary): string {
  const budget = summary.budget;
  if (budget.monthly_budget_cny == null) return "未设置预算";
  const remaining = budget.remaining_cny ?? 0;
  const base = remaining >= 0 ? `预算剩余 ${formatCny(remaining)}` : `已超出预算 ${formatCny(-remaining)}`;
  return budget.alert ? `已达提醒阈值 · ${base}` : base;
}

function renderBudgetBar(backdrop: HTMLElement, summary: UsageSummary): void {
  const host = backdrop.querySelector<HTMLElement>("[data-usage-budget]");
  if (!host) return;
  const budget = summary.budget;
  if (budget.monthly_budget_cny == null) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  setText(backdrop, "[data-usage-budget-label]", `${formatCny(budget.used_cny)} / ${formatCny(budget.monthly_budget_cny)}`);
  setText(backdrop, "[data-usage-budget-percent]", `${budget.used_percent}%`);
  const progress = backdrop.querySelector<HTMLProgressElement>("[data-usage-budget-progress]");
  if (progress) {
    progress.max = budget.monthly_budget_cny;
    progress.value = Math.min(budget.used_cny, budget.monthly_budget_cny);
  }
}

function renderChart(backdrop: HTMLElement, summary: UsageSummary): void {
  const host = backdrop.querySelector<HTMLElement>("[data-usage-chart]");
  if (!host) return;
  const peak = Math.max(...summary.daily.map(day => day.total_tokens), 1);
  host.innerHTML = summary.daily
    .map(day => {
      const percent = day.total_tokens > 0 ? Math.max(4, Math.round((day.total_tokens / peak) * 100)) : 0;
      const title = `${monthDayLabel(day.date)} · ${formatTokens(day.total_tokens)} Token`;
      return `<span style="--usage:${percent}%" title="${esc(title)}"></span>`;
    })
    .join("");
}

function renderModelTable(backdrop: HTMLElement, summary: UsageSummary): void {
  const host = backdrop.querySelector<HTMLElement>("[data-usage-models]");
  if (!host) return;
  const head = '<div class="usage-table-head"><span>模型</span><span>请求</span><span>Token</span><span>费用</span></div>';
  if (!summary.models.length) {
    host.innerHTML = `${head}<div><strong>本月暂无调用</strong><span>–</span><span>–</span><span>–</span></div>`;
    return;
  }
  host.innerHTML = head + summary.models
    .slice(0, 8)
    .map(row => `<div><strong>${esc(row.model)}</strong><span>${row.requests}</span><span>${formatTokens(row.total_tokens)}</span><span>${formatCny(row.estimated_cost_cny)}</span></div>`)
    .join("");
}

/** 预算三项回填表单：数值框、阈值下拉（含增强后的自定义下拉）与硬限制开关。 */
function fillSettingsForm(backdrop: HTMLElement, settings: UsageSettings): void {
  const budgetInput = backdrop.querySelector<HTMLInputElement>('[name="monthlyBudget"]');
  if (budgetInput) budgetInput.value = settings.monthly_budget_cny == null ? "" : String(settings.monthly_budget_cny);

  const select = backdrop.querySelector<HTMLSelectElement>('[name="budgetThreshold"]');
  if (select) {
    const wanted = [...select.options].find(option => (option.textContent || "").includes(`${settings.budget_threshold_percent}%`));
    if (wanted) {
      select.value = wanted.value;
      const custom = select.nextElementSibling;
      if (custom instanceof HTMLElement && custom.classList.contains("settings-custom-select")) {
        const trigger = custom.querySelector("[data-custom-select-trigger] span");
        if (trigger) trigger.textContent = (wanted.textContent || "").trim();
        custom.querySelectorAll("[data-custom-select-option]").forEach(button => {
          button.setAttribute("aria-selected", String(button.getAttribute("data-custom-select-option") === wanted.value));
        });
      }
    }
  }

  const toggle = backdrop.querySelector<HTMLElement>('[name="hardBudgetLimit"]');
  if (toggle) {
    toggle.classList.toggle("active", settings.hard_limit);
    toggle.setAttribute("aria-checked", String(settings.hard_limit));
  }
}

/** 打开设置时回填「用量监控」：统计卡、预算条、柱状图、模型分布与预算表单。 */
export async function hydrateUsagePane(backdrop: HTMLElement): Promise<void> {
  let summary: UsageSummary;
  try {
    summary = await authApi.getUsageSummary();
  } catch (error) {
    const note = error instanceof ApiError && error.status === 401
      ? "登录后可查看用量统计"
      : "用量数据加载失败，稍后重试";
    setText(backdrop, '[data-usage-stat-note="tokens"]', note);
    return;
  }

  setText(backdrop, '[data-usage-stat="tokens"]', formatTokens(summary.totals.total_tokens));
  setText(backdrop, '[data-usage-stat-note="tokens"]', tokensNote(summary));
  setText(backdrop, '[data-usage-stat="runs"]', String(summary.agent_runs.total));
  setText(backdrop, '[data-usage-stat-note="runs"]', `其中 ${summary.agent_runs.llm} 个使用真实模型`);
  setText(backdrop, '[data-usage-stat="cost"]', formatCny(summary.budget.used_cny));
  setText(backdrop, '[data-usage-stat-note="cost"]', costNote(summary));
  setText(
    backdrop,
    "[data-usage-range]",
    `${summary.range.start.slice(0, 4)}年${monthDayLabel(summary.range.start)}－${monthDayLabel(summary.range.end)}`,
  );
  renderBudgetBar(backdrop, summary);
  renderChart(backdrop, summary);
  renderModelTable(backdrop, summary);
  fillSettingsForm(backdrop, {
    monthly_budget_cny: summary.budget.monthly_budget_cny,
    budget_threshold_percent: summary.budget.budget_threshold_percent,
    hard_limit: summary.budget.hard_limit,
  });
}

/** 表单值 → 预算三项（阈值从「达到 80% 时提醒」里提数字；预算空串 = 未设置）。 */
export function usageSettingsFromForm(values: Record<string, unknown>): UsageSettings {
  const rawBudget = String(values.monthlyBudget ?? "").trim();
  const budget = rawBudget === "" ? null : Number.parseFloat(rawBudget);
  const threshold = Number.parseInt(String(values.budgetThreshold ?? "").match(/\d+/)?.[0] ?? "80", 10);
  return {
    monthly_budget_cny: budget != null && Number.isFinite(budget) && budget >= 0 ? budget : null,
    budget_threshold_percent: Number.isFinite(threshold) ? Math.min(100, Math.max(1, threshold)) : 80,
    hard_limit: values.hardBudgetLimit === true,
  };
}

/** 「保存更改」时调用：预算三项落服务端。返回要提示用户的文案，null = 成功。 */
export async function persistUsageSettings(values: Record<string, unknown>): Promise<string | null> {
  try {
    await authApi.updateUsageSettings(usageSettingsFromForm(values));
    return null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return "预算设置已在本机保存，登录后才会同步到服务端生效。";
    }
    return error instanceof Error ? `预算设置同步失败：${error.message}` : "预算设置同步失败，请稍后重试。";
  }
}

const BUDGET_ALERT_GUARD = "openmathmodel.budgetAlertNotified";

/**
 * 应用启动后调用一次：本月费用达到提醒阈值时提醒（隐私开关「预算与限额提醒」）。
 *
 * 桌面通知成功时返回 null；用户正看着页面（桌面通知被抑制）时返回文案，
 * 由调用方以页内 toast 呈现。同一个月每个浏览器会话只提醒一次。
 */
export async function maybeNotifyBudgetAlert(): Promise<string | null> {
  if (!notifyBudgetEnabled()) return null;
  let summary: UsageSummary;
  try {
    summary = await authApi.getUsageSummary();
  } catch {
    return null; // 未登录或网络失败：不打扰
  }
  const budget = summary.budget;
  if (!budget.alert || budget.monthly_budget_cny == null) return null;
  try {
    if (sessionStorage.getItem(BUDGET_ALERT_GUARD) === summary.month) return null;
    sessionStorage.setItem(BUDGET_ALERT_GUARD, summary.month);
  } catch {
    // 会话存储不可用时退化为每次加载提醒一次
  }
  const message = `本月预估费用 ${formatCny(budget.used_cny)}，已达预算 ${formatCny(budget.monthly_budget_cny)} 的 ${budget.used_percent}%`;
  const delivered = sendDesktopNotification({
    title: t("预算与限额提醒"),
    body: message,
    tag: `omm-budget-${summary.month}`,
  });
  return delivered ? null : message;
}

/** 「导出明细」：下载当月调用明细 CSV。返回要提示用户的文案。 */
export async function exportUsageCsv(): Promise<string> {
  let response: Response;
  try {
    response = await fetch("/api/usage/export", { credentials: "same-origin" });
  } catch {
    return "无法连接服务，请确认后端已启动";
  }
  if (response.status === 401) return "请先登录再导出用量明细";
  if (!response.ok) return "导出失败，请稍后重试";

  const disposition = response.headers.get("content-disposition") || "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] || "openmathmodel-usage.csv";
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  return "用量明细 CSV 已导出";
}
