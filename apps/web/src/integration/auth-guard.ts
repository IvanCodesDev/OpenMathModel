/**
 * 运行态路由的登录闸门。
 *
 * `/task/running` 与五条工作台路由都要拿 run_id 找后端要快照，未登录时后端一律
 * 401，页面只能停在「状态同步需要处理 · 请先登录」这种残页上——用户看到的是一个
 * 报错的空壳，而不是一句「先登录」。与其让人先进去再撞墙，不如根本不进：未登录时
 * 直接换到 `/login?next=<原地址>`，登录成功后由 `hydrateAccountUi` 送回原地址。
 *
 * 两种情况不拦：`?demo=1`（演示态不碰任何接口）；后端不可用（`fetchMe` 抛非 401，
 * 这时把人踢去登录页只会更迷惑，留在原页面让既有错误提示说话）。
 */
import { cachedUser, fetchMe } from "../auth/api";
import type { ScreenId } from "../types/screens";
import { demoMode } from "./demo-mode";

/** 需要 run_id + 登录态才能有内容的六条路由。 */
const RUN_BOUND_SCREENS = new Set<ScreenId>([
  "running",
  "data",
  "model",
  "experiments",
  "editor",
  "complete",
]);

/** `/me` 迟迟不回时的兜底：宁可放行看到空页，也不能让正文区一直白着。 */
const GATE_TIMEOUT_MS = 2500;
let gateTimer: number | undefined;

/** 校验期间正文区不可见（CSS `body[data-auth-gate]`），避免残页闪一下。 */
function openGate(): void {
  window.clearTimeout(gateTimer);
  gateTimer = undefined;
  delete document.body.dataset.authGate;
}

function closeGate(state: "checking" | "denied"): void {
  document.body.dataset.authGate = state;
  window.clearTimeout(gateTimer);
  gateTimer = state === "checking" ? window.setTimeout(openGate, GATE_TIMEOUT_MS) : undefined;
}

/**
 * 去登录页，并记下回来的地址。
 * 也给控制器用：会话中途失效时（进页面时缓存还有效，401 才暴露）同样别把人
 * 晾在运行页上看「请先登录」，直接送走。
 */
export function redirectToLogin(): void {
  // 闸门保持关闭直到导航发生，否则跳转前会闪出一帧报错空壳
  closeGate("denied");
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.replace(`/login?next=${encodeURIComponent(next)}`);
}

export function guardRunBoundRoute(screen: ScreenId): void {
  if (!RUN_BOUND_SCREENS.has(screen) || demoMode()) {
    openGate();
    return;
  }
  const known = cachedUser();
  if (known) {
    openGate();
    return;
  }
  if (known === null) {
    redirectToLogin();
    return;
  }
  // 冷启动还没问过后端：先关闸再问，问完再决定放行还是跳登录
  closeGate("checking");
  void fetchMe()
    .then(me => {
      if (me) openGate();
      else redirectToLogin();
    })
    .catch(openGate);
}
