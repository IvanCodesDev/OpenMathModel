/**
 * 现有产品外壳内的认证模态层：登录、注册和双重验证共用同一入口。
 * 成功后以 /api/account/me 复核 httpOnly Cookie，再通知调用方刷新当前界面。
 */
import {
  ApiError,
  authApi,
  fetchMe,
  invalidateMe,
  type MeResponse,
} from "./api";

type AuthView = "login" | "register" | "two-factor";

interface AuthDialogOptions {
  onAuthenticated?: (me: MeResponse) => void;
}

interface AuthDialogState {
  view: AuthView;
  email: string;
  name: string;
  challengeToken: string;
  notice: string;
  error: string;
}

let activeBackdrop: HTMLElement | null = null;
let authenticatedCallbacks: Array<(me: MeResponse) => void> = [];

const escapeHtml = (value: string): string =>
  value.replace(/[&<>"']/g, character => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" } as Record<string, string>
  )[character] ?? character);

function messageFor(error: unknown): string {
  return error instanceof ApiError ? error.message : "操作失败，请稍后再试";
}

function setBusy(form: HTMLFormElement, busy: boolean, label = "处理中…"): void {
  const submit = form.querySelector<HTMLButtonElement>('[type="submit"]');
  if (!submit) return;
  if (busy) {
    submit.dataset.idleLabel = submit.textContent ?? "提交";
    submit.textContent = label;
    submit.disabled = true;
  } else {
    submit.textContent = submit.dataset.idleLabel ?? "提交";
    submit.disabled = false;
  }
}

function input(form: HTMLFormElement, name: string): string {
  return form.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? "";
}

function loginMarkup(state: AuthDialogState): string {
  return `
    <div class="auth-card">
      <div class="auth-dialog-heading"><span class="auth-dialog-icon"><i class="ph ph-user-circle" aria-hidden="true"></i></span><div><h1>登录账户</h1><p class="auth-subtitle">登录后同步项目、任务和账户设置</p></div></div>
      <form data-auth-form="login">
        <label class="auth-field"><span>邮箱</span><input name="email" type="email" autocomplete="username" required value="${escapeHtml(state.email)}" placeholder="name@example.com"></label>
        <label class="auth-field"><span>密码</span><input name="password" type="password" autocomplete="current-password" required></label>
        ${state.error ? `<div class="auth-error" role="alert">${escapeHtml(state.error)}</div>` : ""}
        <button class="auth-submit" type="submit">登录</button>
      </form>
      <p class="auth-switch">还没有账户？<button type="button" data-auth-switch="register">创建账户</button></p>
    </div>`;
}

function registerMarkup(state: AuthDialogState): string {
  return `
    <div class="auth-card">
      <div class="auth-dialog-heading"><span class="auth-dialog-icon"><i class="ph ph-user-plus" aria-hidden="true"></i></span><div><h1>创建账户</h1><p class="auth-subtitle">验证码有效期为 10 分钟</p></div></div>
      <form data-auth-form="register">
        <label class="auth-field"><span>名称</span><input name="name" autocomplete="name" maxlength="80" required value="${escapeHtml(state.name)}"></label>
        <label class="auth-field"><span>邮箱</span><input name="email" type="email" autocomplete="username" required value="${escapeHtml(state.email)}" placeholder="name@example.com"></label>
        <label class="auth-field"><span>邮箱验证码</span><span class="auth-code-row"><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required><button class="auth-send-code" type="button" data-auth-send-code>发送验证码</button></span></label>
        <label class="auth-field"><span>密码</span><input name="password" type="password" autocomplete="new-password" minlength="8" required placeholder="至少 8 个字符"></label>
        ${state.notice ? `<div class="auth-notice" role="status">${escapeHtml(state.notice)}</div>` : ""}
        ${state.error ? `<div class="auth-error" role="alert">${escapeHtml(state.error)}</div>` : ""}
        <button class="auth-submit" type="submit">注册并登录</button>
      </form>
      <p class="auth-switch">已有账户？<button type="button" data-auth-switch="login">返回登录</button></p>
    </div>`;
}

function twoFactorMarkup(state: AuthDialogState): string {
  return `
    <div class="auth-card">
      <div class="auth-dialog-heading"><span class="auth-dialog-icon"><i class="ph ph-shield-check" aria-hidden="true"></i></span><div><h1>双重验证</h1><p class="auth-subtitle">输入验证器生成的 6 位代码或恢复代码</p></div></div>
      <form data-auth-form="two-factor">
        <label class="auth-field"><span>验证码</span><input name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus></label>
        ${state.error ? `<div class="auth-error" role="alert">${escapeHtml(state.error)}</div>` : ""}
        <button class="auth-submit" type="submit">验证并登录</button>
      </form>
      <p class="auth-switch"><button type="button" data-auth-switch="login">返回密码登录</button></p>
    </div>`;
}

function render(backdrop: HTMLElement, state: AuthDialogState): void {
  const body = backdrop.querySelector<HTMLElement>("[data-auth-dialog-body]");
  if (!body) return;
  body.innerHTML = state.view === "register"
    ? registerMarkup(state)
    : state.view === "two-factor"
      ? twoFactorMarkup(state)
      : loginMarkup(state);
  body.querySelector<HTMLInputElement>("input")?.focus();
}

async function confirmSession(backdrop: HTMLElement): Promise<void> {
  invalidateMe();
  const me = await fetchMe(true);
  if (!me) throw new ApiError(401, "SESSION_NOT_ESTABLISHED", "登录成功，但会话 Cookie 未生效，请刷新后重试");
  backdrop.remove();
  activeBackdrop = null;
  const callbacks = authenticatedCallbacks;
  authenticatedCallbacks = [];
  callbacks.forEach(callback => callback(me));
}

function close(backdrop: HTMLElement): void {
  backdrop.remove();
  activeBackdrop = null;
  authenticatedCallbacks = [];
  if (window.location.pathname.replace(/\/$/, "") === "/login") {
    window.history.replaceState({}, "", "/");
  }
}

export function openAuthDialog(options: AuthDialogOptions = {}): void {
  if (options.onAuthenticated) authenticatedCallbacks.push(options.onAuthenticated);
  if (activeBackdrop?.isConnected) {
    activeBackdrop.querySelector<HTMLInputElement>("input")?.focus();
    return;
  }

  const state: AuthDialogState = {
    view: "login",
    email: "",
    name: "",
    challengeToken: "",
    notice: "",
    error: "",
  };
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop auth-dialog";
  backdrop.innerHTML = `
    <section class="modal" role="dialog" aria-modal="true" aria-label="账户登录">
      <button class="auth-dialog-close" type="button" data-auth-dialog-close aria-label="关闭"><i class="ph ph-x" aria-hidden="true"></i></button>
      <div data-auth-dialog-body></div>
    </section>`;
  document.body.appendChild(backdrop);
  activeBackdrop = backdrop;
  render(backdrop, state);

  backdrop.addEventListener("click", event => {
    const target = event.target as Element;
    if (event.target === backdrop || target.closest("[data-auth-dialog-close]")) {
      close(backdrop);
      return;
    }
    const switchButton = target.closest<HTMLButtonElement>("[data-auth-switch]");
    if (switchButton) {
      state.view = switchButton.dataset.authSwitch === "register" ? "register" : "login";
      state.challengeToken = "";
      state.notice = "";
      state.error = "";
      render(backdrop, state);
      return;
    }
    const sendButton = target.closest<HTMLButtonElement>("[data-auth-send-code]");
    if (!sendButton) return;
    const form = sendButton.closest<HTMLFormElement>("form");
    if (!form) return;
    const email = input(form, "email").trim().toLowerCase();
    const name = input(form, "name").trim();
    if (!email) {
      state.error = "请先输入邮箱";
      render(backdrop, state);
      return;
    }
    state.email = email;
    state.name = name;
    state.error = "";
    sendButton.disabled = true;
    sendButton.textContent = "发送中…";
    void authApi.sendRegisterCode(email).then(result => {
      state.notice = result.dev_code
        ? `开发模式验证码：${result.dev_code}`
        : "验证码已发送，请检查邮箱";
      render(backdrop, state);
    }).catch(error => {
      state.error = messageFor(error);
      render(backdrop, state);
    });
  });

  backdrop.addEventListener("submit", event => {
    event.preventDefault();
    const form = event.target as HTMLFormElement;
    const formKind = form.dataset.authForm;
    state.error = "";

    if (formKind === "login") {
      state.email = input(form, "email").trim().toLowerCase();
      setBusy(form, true, "登录中…");
      void authApi.login({ email: state.email, password: input(form, "password") }).then(result => {
        if (result.two_factor_required && result.challenge_token) {
          state.view = "two-factor";
          state.challengeToken = result.challenge_token;
          render(backdrop, state);
          return;
        }
        return confirmSession(backdrop);
      }).catch(error => {
        state.error = messageFor(error);
        render(backdrop, state);
      }).finally(() => setBusy(form, false));
      return;
    }

    if (formKind === "register") {
      state.name = input(form, "name").trim();
      state.email = input(form, "email").trim().toLowerCase();
      setBusy(form, true, "注册中…");
      void authApi.register({
        name: state.name,
        email: state.email,
        code: input(form, "code").trim(),
        password: input(form, "password"),
      }).then(() => confirmSession(backdrop)).catch(error => {
        state.error = messageFor(error);
        render(backdrop, state);
      }).finally(() => setBusy(form, false));
      return;
    }

    if (formKind === "two-factor") {
      setBusy(form, true, "验证中…");
      void authApi.loginTwoFactor({
        challenge_token: state.challengeToken,
        code: input(form, "code").trim(),
      }).then(() => confirmSession(backdrop)).catch(error => {
        state.error = messageFor(error);
        render(backdrop, state);
      }).finally(() => setBusy(form, false));
    }
  });
}
