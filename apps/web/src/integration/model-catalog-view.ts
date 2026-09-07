/**
 * 模型目录视图的纯函数（ADR-0017）：从服务端同步结果里取某厂商的型号、
 * 拼新鲜度文案、查视觉能力。不发请求，便于单测；请求见 model-catalog.ts。
 */

import type { CatalogProvider, ModelCatalogView } from "../auth/api";

export function catalogProvider(view: ModelCatalogView | null, providerId: string): CatalogProvider | undefined {
  return view?.providers.find(provider => provider.id === providerId);
}

/** 卡片副标题的型号（新在前）；目录没有该厂商时为空数组。 */
export function providerHighlights(view: ModelCatalogView | null, providerId: string): string[] {
  return catalogProvider(view, providerId)?.highlights ?? [];
}

/** 该厂商全部可对话型号 ID（新在前），供「默认模型 ID」补全与一键填入。 */
export function providerModels(view: ModelCatalogView | null, providerId: string): string[] {
  return (catalogProvider(view, providerId)?.models ?? []).map(model => model.id);
}

/** 目录是否明确知道该模型收图；未收录返回 undefined（交给命名规则判断）。 */
export function catalogVision(view: ModelCatalogView | null, model: string): boolean | undefined {
  const key = model.trim().toLowerCase();
  if (!view || !key) return undefined;
  for (const provider of view.providers) {
    if (provider.source !== "catalog") continue;
    const hit = provider.models.find(item => item.id.toLowerCase() === key);
    if (hit) return hit.vision;
  }
  return undefined;
}

function relativeTime(iso: string, now: number): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const minutes = Math.max(0, Math.round((now - then) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** 「模型厂商」区块标题下的新鲜度说明。 */
export function catalogFreshnessText(view: ModelCatalogView | null, now: number = Date.now()): string {
  if (!view) return "模型目录暂不可用，型号为内置快照，可能已过期。";
  if (view.source !== "catalog" || !view.synced_at) {
    if (!view.enabled) return "服务端已关闭模型目录同步，型号为内置快照。";
    if (view.error) return `模型目录尚未同步成功（${view.error}），型号为内置快照。`;
    return "模型目录尚未同步，型号为内置快照；后台正在拉取。";
  }
  const ago = relativeTime(view.synced_at, now);
  const when = ago === "刚刚" ? "刚刚同步" : `同步于 ${ago || "未知时间"}`;
  const parts = [`型号目录${when}，来源 ${hostOf(view.catalog_url)}`];
  if (view.error) parts.push("上次刷新失败，沿用上一份结果");
  else if (view.stale) parts.push("已到刷新时间，后台正在重试");
  return `${parts.join("；")}。`;
}
