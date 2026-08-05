/**
 * 设置中心「账户与安全」面板：真实接口驱动的渲染与操作。
 * 由 legacy 设置中心在打开时调用 initSecurityPane(backdrop)；
 * hydrateAccountUi() 负责把侧栏用户信息同步为登录态。
 */
import {
  ApiError,
  authApi,
  fetchMe,
  invalidateMe,
  type DeviceSession,
  type MeResponse,
} from "./api";

const escapeHtml = (value: string): string =>
  value.replace(/[&<>"']/g, character => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" } as Record<string, string>
  )[character] ?? character);

const icon = (name: string): string => `<i class="ph ph-${name}" aria-hidden="true"></i>`;

function showToast(message: string, duration = 2200): void {
  document.querySelector(".toast")?.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  document.body.appendChild(node);
  window.setTimeout(() => node.remove(), duration);
}

function relativeTime(iso: string): string {
  const time = new Date(iso).getTime();
  if (!Number.isFinite(time)) return "";
  const diffMs = Date.now() - time;
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 60) return `${days} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

// ── 对话框基元（复用原型 .modal 视觉） ──────────────────────────

function openDialog(title: string, bodyHtml: string): HTMLElement {
  document.querySelector(".security-dialog")?.remove();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop security-dialog";
  backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h2>${escapeHtml(title)}</h2>${bodyHtml}</div>`;
  document.body.appendChild(backdrop);
  backdrop.addEventListener("click", event => {
    if (event.target === backdrop || (event.target as Element).closest("[data-dialog-cancel]")) {
      backdrop.remove();
    }
  });
  backdrop.querySelector<HTMLInputElement>("input")?.focus();
  return backdrop;
}

function dialogError(backdrop: HTMLElement, message: string): void {
  const box = backdrop.querySelector<HTMLElement>("[data-dialog-error]");
  if (!box) return;
  box.textContent = message;
  box.style.display = "block";
}

function inputValue(backdrop: HTMLElement, name: string): string {
  return backdrop.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? "";
}

async function runAction(backdrop: HTMLElement, button: HTMLButtonElement, task: () => Promise<void>): Promise<void> {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = "处理中…";
  try {
    await task();
  } catch (error) {
    button.disabled = false;
    button.innerHTML = original;
    dialogError(backdrop, error instanceof ApiError ? error.message : "操作失败，请稍后再试");
    return;
  }
  button.disabled = false;
  button.innerHTML = original;
}

function recoveryCodesHtml(codes: string[]): string {
  return `
    <p class="dialog-note">恢复代码仅显示这一次，请立即保存到安全的位置。每个代码只能使用一次。</p>
    <div class="recovery-codes-grid">${codes.map(code => `<code>${escapeHtml(code)}</code>`).join("")}</div>
    <div class="modal-actions">
      <button type="button" data-copy-codes>${icon("copy")} 复制全部</button>
      <button type="button" class="primary" data-dialog-cancel>我已保存</button>
    </div>`;
}

function bindCopyCodes(backdrop: HTMLElement, codes: string[]): void {
  backdrop.querySelector("[data-copy-codes]")?.addEventListener("click", () => {
    void navigator.clipboard?.writeText(codes.join("\n")).then(() => showToast("恢复代码已复制"));
  });
}

// ── 面板渲染 ─────────────────────────────────────────────────────

interface PaneState {
  me: MeResponse;
  sessions: DeviceSession[];
}

function deviceIcon(session: DeviceSession): string {
  if (session.kind === "mobile") return icon("device-mobile");
  return session.current ? icon("desktop") : icon("browser");
}

function securityPaneHtml(state: PaneState): string {
  const { user, security } = state.me;
  const twoFactorStatus = security.two_factor_enabled
    ? '<span class="green">已通过验证器应用启用</span>'
    : "<span>未启用，建议开启以保护账户</span>";
  const recoveryStatus = security.two_factor_enabled
    ? `<span>还剩 ${security.recovery_codes_remaining} 个可用恢复代码</span>`
    : "<span>启用双重验证后可用</span>";

  const deviceRows = state.sessions
    .map(session => {
      const meta = `${session.ip ? escapeHtml(session.ip) : "本机"} · ${
        session.current ? "当前设备" : relativeTime(session.last_seen_at)
      }`;
      const tail = session.current
        ? '<span class="device-status">当前</span>'
        : `<button type="button" data-sec-action="revoke-device" data-session-id="${session.id}" data-device-label="${escapeHtml(session.device_label)}">退出</button>`;
      return `<div class="device-item"><span class="security-icon">${deviceIcon(session)}</span><div><strong>${escapeHtml(session.device_label)}</strong><span>${meta}</span></div>${tail}</div>`;
    })
    .join("");

  const hasOthers = state.sessions.some(session => !session.current);

  return `
    <div class="settings-section account-identity">
      <span class="account-avatar-large">${escapeHtml(user.avatar_letter)}</span>
      <div><h3>${escapeHtml(user.name)}</h3><p>${escapeHtml(user.email)} · ${escapeHtml(user.plan)}</p></div>
      <button type="button" class="secondary-small" data-sec-action="edit-profile">编辑资料</button>
    </div>
    <div class="settings-section">
      <div class="settings-section-heading"><div><h3>登录安全</h3><p>保护你的账户和 API 凭据。更改即时生效。</p></div></div>
      <div class="security-item"><span class="security-icon">${icon("password")}</span><div><strong>账户密码</strong><span>上次修改于 ${relativeTime(security.password_changed_at)}</span></div><button type="button" data-sec-action="change-password">修改密码</button></div>
      <div class="security-item"><span class="security-icon">${icon("device-mobile")}</span><div><strong>双重验证</strong>${twoFactorStatus}</div><button type="button" data-sec-action="manage-2fa">${security.two_factor_enabled ? "管理" : "启用"}</button></div>
      <div class="security-item"><span class="security-icon">${icon("key")}</span><div><strong>恢复代码</strong>${recoveryStatus}</div><button type="button" data-sec-action="recovery-codes" ${security.two_factor_enabled ? "" : "disabled"}>查看</button></div>
    </div>
    <div class="settings-section">
      <div class="settings-section-heading"><div><h3>登录设备</h3><p>当前处于登录状态的设备会话。</p></div>${
        hasOthers ? '<button type="button" class="danger-text" data-sec-action="revoke-others">退出其他设备</button>' : ""
      }</div>
      <div class="device-list">${deviceRows}</div>
    </div>
    <div class="settings-section">
      <div class="settings-section-heading"><div><h3>当前登录</h3><p>退出后需要重新输入邮箱和密码。</p></div><button type="button" class="danger-text" data-sec-action="logout">退出登录</button></div>
    </div>`;
}

function loginPromptHtml(): string {
  return `
    <div class="settings-section security-login-prompt">
      <span class="security-prompt-icon">${icon("user-circle")}</span>
      <h3>尚未登录</h3>
      <p>登录后可管理个人资料、密码、双重验证和登录设备。</p>
      <button type="button" class="primary-small" data-sec-action="go-login">${icon("sign-in")} 前往登录</button>
    </div>`;
}

function errorHtml(message: string): string {
  return `
    <div class="settings-section security-login-prompt">
      <span class="security-prompt-icon">${icon("warning-circle")}</span>
      <h3>账户信息加载失败</h3>
      <p>${escapeHtml(message)}</p>
      <button type="button" class="secondary-small" data-sec-action="reload">重试</button>
    </div>`;
}

async function loadPane(root: HTMLElement, backdrop: HTMLElement): Promise<void> {
  root.innerHTML = '<div class="settings-section security-loading">正在加载账户信息…</div>';
  let me: MeResponse | null;
  try {
    me = await fetchMe(true);
  } catch (error) {
    root.innerHTML = errorHtml(error instanceof ApiError ? error.message : "网络异常");
    return;
  }
  latestMe = me;
  if (me === null) {
    root.innerHTML = loginPromptHtml();
    hydrateAccountCard(backdrop, null);
    return;
  }
  let sessions: DeviceSession[] = [];
  try {
    sessions = (await authApi.listSessions()).sessions;
  } catch {
    sessions = [];
  }
  root.innerHTML = securityPaneHtml({ me, sessions });
  hydrateAccountCard(backdrop, me);
  void hydrateAccountUi();
}

function hydrateAccountCard(backdrop: HTMLElement, me: MeResponse | null): void {
  const card = backdrop.querySelector<HTMLElement>(".settings-account-card");
  if (!card) return;
  const avatar = card.querySelector<HTMLElement>(".avatar");
  const name = card.querySelector<HTMLElement>("strong");
  const plan = card.querySelector<HTMLElement>("span:not(.avatar):not(.settings-plan)");
  const badge = card.querySelector<HTMLElement>(".settings-plan");
  if (me) {
    if (avatar) avatar.textContent = me.user.avatar_letter;
    if (name) name.textContent = me.user.name;
    if (plan) plan.textContent = me.user.plan;
    if (badge) badge.style.display = "";
  } else {
    if (avatar) avatar.textContent = "?";
    if (name) name.textContent = "未登录";
    if (plan) plan.textContent = "登录后同步账户";
    if (badge) badge.style.display = "none";
  }
}

// ── 各操作对话框 ─────────────────────────────────────────────────

function openEditProfileDialog(me: MeResponse, reload: () => void): void {
  const backdrop = openDialog(
    "编辑资料",
    `
    <label>名称</label><input name="name" value="${escapeHtml(me.user.name)}" maxlength="80">
    <label>邮箱</label><input name="email" type="email" value="${escapeHtml(me.user.email)}">
    <label>当前密码<small class="dialog-inline-hint">（仅修改邮箱时需要）</small></label><input name="password" type="password" placeholder="修改邮箱时必填">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>取消</button><button type="button" class="primary" data-dialog-submit>保存更改</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      const name = inputValue(backdrop, "name").trim();
      const email = inputValue(backdrop, "email").trim().toLowerCase();
      const password = inputValue(backdrop, "password");
      if (!name) throw new ApiError(0, "VALIDATION", "名称不能为空");
      const body: { name?: string; email?: string; password?: string } = {};
      if (name !== me.user.name) body.name = name;
      if (email !== me.user.email) {
        body.email = email;
        if (!password) throw new ApiError(0, "VALIDATION", "修改邮箱需要输入当前密码");
        body.password = password;
      }
      if (Object.keys(body).length === 0) {
        backdrop.remove();
        return;
      }
      await authApi.updateProfile(body);
      invalidateMe();
      backdrop.remove();
      showToast("资料已更新");
      reload();
    });
  });
}

function openChangePasswordDialog(reload: () => void): void {
  const backdrop = openDialog(
    "修改密码",
    `
    <label>当前密码</label><input name="current" type="password" autocomplete="current-password">
    <label>新密码</label><input name="next" type="password" autocomplete="new-password" placeholder="至少 8 个字符">
    <label>确认新密码</label><input name="confirm" type="password" autocomplete="new-password">
    <p class="dialog-note">修改成功后，除当前设备外的所有登录会话都会退出。</p>
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>取消</button><button type="button" class="primary" data-dialog-submit>确认修改</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      const current = inputValue(backdrop, "current");
      const next = inputValue(backdrop, "next");
      const confirm = inputValue(backdrop, "confirm");
      if (next.length < 8) throw new ApiError(0, "VALIDATION", "新密码至少 8 个字符");
      if (next !== confirm) throw new ApiError(0, "VALIDATION", "两次输入的新密码不一致");
      await authApi.changePassword({ current_password: current, new_password: next });
      backdrop.remove();
      showToast("密码已修改，其他设备已退出登录");
      reload();
    });
  });
}

function openEnableTwoFactorDialog(reload: () => void): void {
  const backdrop = openDialog("启用双重验证", '<p class="dialog-note">正在生成验证器密钥…</p>');
  void (async () => {
    let setup: { secret: string; otpauth_uri: string };
    try {
      setup = await authApi.twoFactorSetup();
    } catch (error) {
      const modalBody = backdrop.querySelector(".modal");
      if (modalBody) {
        modalBody.innerHTML = `<h2>启用双重验证</h2><p class="dialog-note">${escapeHtml(
          error instanceof ApiError ? error.message : "加载失败，请稍后再试",
        )}</p><div class="modal-actions"><button type="button" data-dialog-cancel>关闭</button></div>`;
      }
      return;
    }
    const modalBody = backdrop.querySelector(".modal");
    if (!modalBody) return;
    modalBody.innerHTML = `
      <h2>启用双重验证</h2>
      <p class="dialog-note">1. 在验证器应用（如 1Password、Microsoft Authenticator、Google Authenticator）中添加账户，输入下方密钥或点击链接。</p>
      <div class="secret-box"><code>${escapeHtml(setup.secret)}</code><button type="button" data-copy-secret title="复制密钥">${icon("copy")}</button></div>
      <p class="dialog-note"><a href="${escapeHtml(setup.otpauth_uri)}">otpauth 快捷链接</a>（支持的验证器可直接识别）</p>
      <label>2. 输入验证器生成的 6 位验证码</label><input name="code" inputmode="numeric" maxlength="6" placeholder="000000">
      <div class="dialog-error" data-dialog-error></div>
      <div class="modal-actions"><button type="button" data-dialog-cancel>取消</button><button type="button" class="primary" data-dialog-submit>验证并启用</button></div>`;
    modalBody.querySelector("[data-copy-secret]")?.addEventListener("click", () => {
      void navigator.clipboard?.writeText(setup.secret).then(() => showToast("密钥已复制"));
    });
    modalBody.querySelector<HTMLInputElement>("[name=code]")?.focus();
    modalBody.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
      void runAction(backdrop, this, async () => {
        const { recovery_codes } = await authApi.twoFactorEnable(inputValue(backdrop, "code"));
        invalidateMe();
        modalBody.innerHTML = `<h2>双重验证已启用</h2>${recoveryCodesHtml(recovery_codes)}`;
        bindCopyCodes(backdrop, recovery_codes);
        showToast("双重验证已启用");
        reload();
      });
    });
  })();
}

function openManageTwoFactorDialog(reload: () => void): void {
  const backdrop = openDialog(
    "双重验证管理",
    `
    <p class="dialog-note">双重验证已启用。输入当前密码后可以重新生成恢复代码，或关闭双重验证。</p>
    <label>当前密码</label><input name="password" type="password" autocomplete="current-password">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions">
      <button type="button" data-dialog-cancel>取消</button>
      <button type="button" data-dialog-regen>${icon("arrows-clockwise")} 重新生成恢复代码</button>
      <button type="button" class="danger" data-dialog-disable>关闭双重验证</button>
    </div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-regen]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      const { recovery_codes } = await authApi.regenerateRecoveryCodes(inputValue(backdrop, "password"));
      invalidateMe();
      const modalBody = backdrop.querySelector(".modal");
      if (modalBody) {
        modalBody.innerHTML = `<h2>新的恢复代码</h2>${recoveryCodesHtml(recovery_codes)}`;
        bindCopyCodes(backdrop, recovery_codes);
      }
      reload();
    });
  });
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-disable]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      await authApi.twoFactorDisable(inputValue(backdrop, "password"));
      invalidateMe();
      backdrop.remove();
      showToast("双重验证已关闭");
      reload();
    });
  });
}

function openRecoveryCodesDialog(me: MeResponse, reload: () => void): void {
  const backdrop = openDialog(
    "恢复代码",
    `
    <p class="dialog-note">还剩 <strong>${me.security.recovery_codes_remaining}</strong> 个可用恢复代码。出于安全考虑，代码只在生成时完整显示一次；如已遗失请重新生成（旧代码将全部失效）。</p>
    <label>当前密码</label><input name="password" type="password" autocomplete="current-password">
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>取消</button><button type="button" class="primary" data-dialog-submit>重新生成</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      const { recovery_codes } = await authApi.regenerateRecoveryCodes(inputValue(backdrop, "password"));
      invalidateMe();
      const modalBody = backdrop.querySelector(".modal");
      if (modalBody) {
        modalBody.innerHTML = `<h2>新的恢复代码</h2>${recoveryCodesHtml(recovery_codes)}`;
        bindCopyCodes(backdrop, recovery_codes);
      }
      reload();
    });
  });
}

function openConfirmDialog(title: string, message: string, confirmLabel: string, onConfirm: () => Promise<void>): void {
  const backdrop = openDialog(
    title,
    `
    <p class="dialog-note">${escapeHtml(message)}</p>
    <div class="dialog-error" data-dialog-error></div>
    <div class="modal-actions"><button type="button" data-dialog-cancel>取消</button><button type="button" class="primary" data-dialog-submit>${escapeHtml(confirmLabel)}</button></div>`,
  );
  backdrop.querySelector<HTMLButtonElement>("[data-dialog-submit]")?.addEventListener("click", function () {
    void runAction(backdrop, this, async () => {
      await onConfirm();
      backdrop.remove();
    });
  });
}

// ── 面板初始化与事件 ─────────────────────────────────────────────

let latestMe: MeResponse | null = null;

export function initSecurityPane(backdrop: HTMLElement): void {
  const pane = backdrop.querySelector<HTMLElement>('[data-settings-pane="security"]');
  if (!pane) return;
  let root = pane.querySelector<HTMLElement>("[data-security-root]");
  if (!root) {
    root = document.createElement("div");
    root.dataset.securityRoot = "";
    pane.innerHTML = "";
    pane.appendChild(root);
  }

  const reload = () => void loadPane(root, backdrop);

  pane.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLButtonElement>("[data-sec-action]");
    if (!button || button.disabled) return;
    event.stopPropagation();
    const action = button.dataset.secAction;
    const me = latestMe;

    if (action === "go-login") {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
      return;
    }
    if (action === "reload") {
      reload();
      return;
    }
    if (!me) return;

    if (action === "edit-profile") openEditProfileDialog(me, reload);
    if (action === "change-password") openChangePasswordDialog(reload);
    if (action === "manage-2fa") {
      if (me.security.two_factor_enabled) openManageTwoFactorDialog(reload);
      else openEnableTwoFactorDialog(reload);
    }
    if (action === "recovery-codes") openRecoveryCodesDialog(me, reload);
    if (action === "revoke-device") {
      const sessionId = button.dataset.sessionId ?? "";
      const label = button.dataset.deviceLabel ?? "该设备";
      openConfirmDialog("退出设备", `确定要退出「${label}」吗？该设备需要重新登录。`, "退出设备", async () => {
        await authApi.revokeSession(sessionId);
        showToast("该设备已退出登录");
        reload();
      });
    }
    if (action === "revoke-others") {
      openConfirmDialog("退出其他设备", "除当前设备外的所有登录会话都会退出。", "全部退出", async () => {
        const result = await authApi.revokeOtherSessions();
        showToast(`已退出 ${result.revoked_sessions} 台设备`);
        reload();
      });
    }
    if (action === "logout") {
      openConfirmDialog("退出登录", "退出后需要重新输入邮箱和密码才能继续使用账户功能。", "退出登录", async () => {
        await authApi.logout();
        invalidateMe();
        window.location.href = "/login";
      });
    }
  });

  reload();
}

// ── 侧栏用户信息同步 ─────────────────────────────────────────────

export async function hydrateAccountUi(): Promise<void> {
  let me: MeResponse | null;
  try {
    me = await fetchMe();
  } catch {
    return; // 后端不可用时保持原型静态内容
  }
  latestMe = me;

  const profileRow = document.querySelector<HTMLElement>(".profile-row");
  if (!profileRow) return;
  const avatar = profileRow.querySelector<HTMLElement>(".avatar");
  const name = profileRow.querySelector<HTMLElement>("strong");
  const detail = profileRow.querySelector<HTMLElement>("small");

  if (me) {
    if (avatar) avatar.textContent = me.user.avatar_letter;
    if (name) name.textContent = me.user.name;
    if (detail) detail.textContent = me.user.email;
    profileRow.classList.remove("profile-row-guest");
    profileRow.removeAttribute("title");
  } else {
    if (avatar) avatar.textContent = "?";
    if (name) name.textContent = "未登录";
    if (detail) detail.textContent = "点击登录账户";
    profileRow.classList.add("profile-row-guest");
    profileRow.title = "前往登录";
  }
}

// 未登录时点击侧栏用户区跳转登录页（设置按钮除外）
document.addEventListener("click", event => {
  const row = (event.target as Element).closest?.(".profile-row-guest");
  if (!row) return;
  if ((event.target as Element).closest('[data-action="settings"]')) return;
  window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
});
