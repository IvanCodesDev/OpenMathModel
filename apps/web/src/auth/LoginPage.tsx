import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, authApi, fetchMe, invalidateMe } from "./api";
import { DoodleGarden } from "./LoginArtwork";
import { Jelly } from "../components/Jelly";
import { loginReturnPath } from "./login-navigation";
import "./login-page.css";

type View = "login" | "register" | "two-factor";

export default function LoginPage() {
  const [view, setView] = useState<View>("login");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [greeting, setGreeting] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const submitLock = useRef(false);
  const sendLock = useRef(false);
  const version = useRef(0);
  const previousView = useRef(view);

  useEffect(() => {
    document.title = "OpenMathModel · 登录";
    let active = true;
    void fetchMe().then(me => {
      if (active && me) window.location.replace(loginReturnPath(window.location.search, window.location.origin));
    }).catch(() => {});
    return () => { active = false; version.current += 1; };
  }, []);
  useEffect(() => {
    if (view !== previousView.current) {
      (view === "two-factor" ? codeRef : emailRef).current?.focus();
      previousView.current = view;
    }
  }, [view]);
  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setTimeout(() => setCooldown(value => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  function switchView(next: View) {
    version.current += 1;
    setView(next); setError(""); setNotice(""); setCode(""); setPassword(""); setVisible(false); setChallenge("");
  }
  async function confirmSession() {
    invalidateMe();
    const me = await fetchMe(true);
    if (!me) throw new ApiError(401, "SESSION_NOT_ESTABLISHED", "会话尚未建立，请检查浏览器 Cookie 设置后重试");
    window.location.replace(loginReturnPath(window.location.search, window.location.origin));
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitLock.current) return;
    submitLock.current = true; setBusy(true); setError("");
    try {
      if (view === "login") {
        const result = await authApi.login({ email: email.trim().toLowerCase(), password });
        if (result.two_factor_required) {
          if (!result.challenge_token) throw new Error("登录验证暂时不可用，请重试");
          setChallenge(result.challenge_token); setView("two-factor"); setPassword(""); setCode("");
          return;
        }
      } else if (view === "register") {
        await authApi.register({ email: email.trim().toLowerCase(), name: name.trim(), code: code.trim(), password });
      } else {
        await authApi.loginTwoFactor({ challenge_token: challenge, code: code.trim() });
      }
      await confirmSession();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "操作失败，请稍后重试");
    } finally { submitLock.current = false; setBusy(false); }
  }
  async function sendCode() {
    if (sendLock.current || cooldown || !emailRef.current?.reportValidity()) return;
    sendLock.current = true; setSending(true); setError(""); setNotice("");
    const requestVersion = version.current;
    try {
      const result = await authApi.sendRegisterCode(email.trim().toLowerCase());
      if (requestVersion !== version.current) return;
      setNotice(result.dev_code ? `开发模式验证码：${result.dev_code}` : "验证码已发送，请检查邮箱（10 分钟内有效）");
      setCooldown(60);
    } catch (failure) {
      if (requestVersion === version.current) setError(failure instanceof Error ? failure.message : "发送失败，请重试");
    } finally { sendLock.current = false; setSending(false); }
  }

  return <main className="omm-login" data-view={view}>
    <a className="login-skip" href="#login-email">跳到登录表单</a>
    <section className="login-panel" aria-label={view === "login" ? "账户登录" : view === "register" ? "创建账户" : "双重验证"}>
      <header className="login-brand">
        <button type="button" className={`login-mascot ${greeting ? "is-greeting" : ""}`} aria-label="和小水母打招呼" onClick={() => setGreeting(value => !value)} aria-pressed={greeting}>
          <Jelly variant="plain" happy={greeting} />
          <span className="mascot-spark spark-one">✦</span><span className="mascot-spark spark-two">✦</span><span className="mascot-spark spark-three">✧</span>
        </button>
        <h1>{view === "login" ? "OpenMathModel" : view === "register" ? "创建账户" : "双重验证"}</h1>
        <p>{view === "login" ? "让数学更开放，让智能更普惠" : view === "register" ? "和小水母一起，探索数学的无限可能" : "输入验证器代码或恢复代码，安全继续"}</p>
      </header>
      <form className="login-form" onSubmit={event => void submit(event)} aria-busy={busy}>
        <fieldset disabled={busy}>
          {view === "register" && <label className="login-field"><span>名称</span><input autoComplete="name" value={name} onChange={event => setName(event.target.value)} maxLength={80} required placeholder="你的名字" /></label>}
          {view !== "two-factor" && <label className="login-field" htmlFor="login-email"><span>邮箱</span><input ref={emailRef} id="login-email" name="email" type="email" autoComplete="username" required value={email} onChange={event => setEmail(event.target.value)} placeholder="name@example.com" /></label>}
          {view !== "login" && <label className="login-field"><span>{view === "register" ? "邮箱验证码" : "验证码或恢复代码"}</span><span className="login-code"><input ref={codeRef} name="code" autoComplete="one-time-code" inputMode={view === "register" ? "numeric" : "text"} pattern={view === "register" ? "[0-9]{6}" : undefined} maxLength={view === "register" ? 6 : 64} value={code} onChange={event => setCode(event.target.value)} required placeholder={view === "register" ? "6 位验证码" : "输入验证码或恢复代码"} />{view === "register" && <button type="button" disabled={sending || cooldown > 0} onClick={() => void sendCode()}>{sending ? "发送中…" : cooldown ? `${cooldown} 秒后重发` : "发送验证码"}</button>}</span></label>}
          {view !== "two-factor" && <label className="login-field" htmlFor="login-password"><span>密码</span><span className="login-password"><input id="login-password" name="password" type={visible ? "text" : "password"} autoComplete={view === "register" ? "new-password" : "current-password"} minLength={view === "register" ? 8 : undefined} required value={password} onChange={event => setPassword(event.target.value)} placeholder={view === "register" ? "至少 8 个字符" : undefined} /><button type="button" aria-label={visible ? "隐藏密码" : "显示密码"} aria-pressed={visible} onClick={() => setVisible(value => !value)}><i className={`ph ${visible ? "ph-eye-slash" : "ph-eye"}`} aria-hidden="true" /></button></span></label>}
          {notice && <p className="login-notice" role="status">{notice}</p>}
          {error && <p className="login-error" role="alert">{error}</p>}
          <button className="login-submit" type="submit">{busy ? "正在验证…" : view === "login" ? "登录" : view === "register" ? "注册并登录" : "验证并登录"}</button>
        </fieldset>
      </form>
      <footer className="login-switch">{view === "login" ? <>还没有账号？<button type="button" disabled={busy} onClick={() => switchView("register")}>创建账号</button></> : <>{view === "register" ? "已有账号？" : "换一种方式"}<button type="button" disabled={busy} onClick={() => switchView("login")}>返回登录</button></>}</footer>
    </section>
    <DoodleGarden />
  </main>;
}
