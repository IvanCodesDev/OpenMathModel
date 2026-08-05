from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import (
    CHALLENGE_TTL_SECONDS,
    COOKIE_SECURE,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
)
from ..db import get_db
from ..emailer import send_email, smtp_configured
from ..errors import ApiError
from ..ids import new_id
from ..models import AuthSession, RecoveryCode, User, utcnow
from ..orm import EmailVerificationCodeRow
from ..schemas import (
    LoginRequest,
    RegisterRequest,
    SendEmailCodeRequest,
    TwoFaLoginRequest,
    user_payload,
)
from ..security import (
    RateLimiter,
    device_label,
    generate_email_code,
    hash_email_code,
    hash_password,
    hash_recovery_code,
    new_session_token,
    parse_user_agent,
    sha256_hex,
    sign_challenge,
    verify_challenge,
    verify_password,
    verify_totp,
)

logger = logging.getLogger("omm.auth")

router = APIRouter(tags=["auth"])

# 兜底限速器：正常路径使用 app.state.login_limiter（数据库实现，多实例一致）；
# 仅在极早期启动阶段 state 缺失时退回进程内实现。
_fallback_limiter = RateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)


def _limiter(request: Request):
    return getattr(request.app.state, "login_limiter", None) or _fallback_limiter


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _limiter_keys(email: str, request: Request) -> list[str]:
    return [f"email:{email}", f"ip:{_client_ip(request)}"]


def create_session(db: Session, user: User, request: Request, response: Response) -> AuthSession:
    token = new_session_token()
    browser, os_name, kind = parse_user_agent(request.headers.get("user-agent", ""))
    session = AuthSession(
        user_id=user.id,
        token_hash=sha256_hex(token),
        browser=browser,
        os_name=os_name,
        kind=kind,
        device_label=device_label(browser, os_name),
        ip=_client_ip(request),
        expires_at=utcnow() + timedelta(days=SESSION_TTL_DAYS),
    )
    db.add(session)
    db.commit()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return session


@router.post("/register/send-code")
def send_register_code(
    body: SendEmailCodeRequest, request: Request, db: Session = Depends(get_db)
):
    settings = request.app.state.settings
    exists = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if exists is not None:
        raise ApiError(409, "EMAIL_TAKEN", "该邮箱已注册，请直接登录")

    limiter = getattr(request.app.state, "email_code_limiter", None) or _fallback_limiter
    keys = [f"mailcode:{body.email}", f"mailip:{_client_ip(request)}"]
    if not limiter.allow(keys):
        raise ApiError(429, "TOO_MANY_REQUESTS", "验证码发送太频繁，请稍后再试")
    limiter.record_failure(keys)  # 发送即占一次窗口额度

    now = utcnow()
    for stale in db.scalars(
        select(EmailVerificationCodeRow).where(
            EmailVerificationCodeRow.email == body.email,
            EmailVerificationCodeRow.used_at.is_(None),
        )
    ):
        stale.used_at = now  # 旧验证码作废，同一时刻只有最新一条有效

    code = generate_email_code()
    db.add(
        EmailVerificationCodeRow(
            id=new_id("evc"),
            email=body.email,
            code_hash=hash_email_code(body.email, code),
            expires_at=now + timedelta(seconds=settings.email_code_ttl_seconds),
            created_at=now,
        )
    )
    db.commit()

    ttl_minutes = settings.email_code_ttl_seconds // 60
    if smtp_configured(settings):
        try:
            send_email(
                settings,
                body.email,
                "OpenMathModel 注册验证码",
                f"您的注册验证码是 {code}，{ttl_minutes} 分钟内有效。如非本人操作请忽略。",
            )
        except Exception:
            logger.exception("send verification email failed: %s", body.email)
            raise ApiError(502, "EMAIL_SEND_FAILED", "验证码邮件发送失败，请稍后再试")
        return {"ok": True, "expires_in": settings.email_code_ttl_seconds}

    # 开发模式：无 SMTP，验证码写日志并随响应返回，便于本地联调
    logger.info("[DEV] 注册验证码 %s -> %s", body.email, code)
    payload: dict = {"ok": True, "expires_in": settings.email_code_ttl_seconds}
    if settings.email_dev_mode:
        payload["dev_code"] = code
    return payload


@router.post("/register", status_code=201)
def register(body: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    exists = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if exists is not None:
        raise ApiError(409, "EMAIL_TAKEN", "该邮箱已注册，请直接登录")

    now = utcnow()
    code_row = db.scalar(
        select(EmailVerificationCodeRow).where(
            EmailVerificationCodeRow.email == body.email,
            EmailVerificationCodeRow.used_at.is_(None),
            EmailVerificationCodeRow.expires_at > now,
            EmailVerificationCodeRow.code_hash == hash_email_code(body.email, body.code),
        )
    )
    if code_row is None:
        raise ApiError(401, "INVALID_EMAIL_CODE", "邮箱验证码不正确或已过期")
    code_row.used_at = now

    user = User(email=body.email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()

    create_session(db, user, request, response)
    return {"user": user_payload(user)}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter = _limiter(request)
    keys = _limiter_keys(body.email, request)
    if not limiter.allow(keys):
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "尝试次数过多，请 5 分钟后再试")

    user = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if user is None or not verify_password(body.password, user.password_hash):
        limiter.record_failure(keys)
        raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码不正确")

    if user.totp_enabled:
        return {
            "two_factor_required": True,
            "challenge_token": sign_challenge(user.id, CHALLENGE_TTL_SECONDS),
        }

    limiter.reset(keys)
    create_session(db, user, request, response)
    return {"two_factor_required": False, "user": user_payload(user)}


@router.post("/login/2fa")
def login_two_factor(body: TwoFaLoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user_id = verify_challenge(body.challenge_token)
    if user_id is None:
        raise ApiError(401, "CHALLENGE_EXPIRED", "验证已过期，请重新登录")

    user = db.get(User, user_id)
    if user is None or not user.totp_enabled or not user.totp_secret:
        raise ApiError(401, "CHALLENGE_EXPIRED", "验证已过期，请重新登录")

    limiter = _limiter(request)
    keys = _limiter_keys(user.email, request)
    if not limiter.allow(keys):
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "尝试次数过多，请 5 分钟后再试")

    if verify_totp(user.totp_secret, body.code):
        pass
    else:
        recovery = db.scalar(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == hash_recovery_code(body.code),
                RecoveryCode.used_at.is_(None),
            )
        )
        if recovery is None:
            limiter.record_failure(keys)
            raise ApiError(401, "INVALID_CODE", "验证码不正确")
        recovery.used_at = utcnow()
        db.commit()

    limiter.reset(keys)
    create_session(db, user, request, response)
    return {"two_factor_required": False, "user": user_payload(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == sha256_hex(token)))
        if session is not None and session.revoked_at is None:
            session.revoked_at = utcnow()
            db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}
