/**
 * 服务端模型目录的页面侧缓存（ADR-0017）。
 *
 * 目录由后端定时从公共目录同步（``GET /api/llm/catalog`` 永不阻塞在出网上），
 * 页面一次会话内只拉一次；「立即同步」走 ``POST /api/llm/catalog/refresh`` 并
 * 用返回的新视图替换缓存。未登录 / 后端不可用时解析为 null，调用方按内置文案
 * 降级，下一次调用会重试。
 */

import { authApi, type ModelCatalogView } from "../auth/api";

let catalogPromise: Promise<ModelCatalogView | null> | undefined;
let latest: ModelCatalogView | null = null;

export function loadModelCatalog(): Promise<ModelCatalogView | null> {
  catalogPromise ??= authApi.getModelCatalog().then(
    view => {
      latest = view;
      return view;
    },
    () => {
      catalogPromise = undefined;
      return null;
    },
  );
  return catalogPromise;
}

/** 最近一次成功拿到的目录（同步读取）；还没拉过时为 null。 */
export function currentModelCatalog(): ModelCatalogView | null {
  return latest;
}

/** 「立即同步」：失败抛 ApiError（服务端保留旧数据，页面照旧显示上一份）。 */
export async function refreshModelCatalog(): Promise<ModelCatalogView> {
  const view = await authApi.refreshModelCatalog();
  latest = view;
  catalogPromise = Promise.resolve(view);
  return view;
}

export function resetModelCatalog(): void {
  catalogPromise = undefined;
  latest = null;
}
