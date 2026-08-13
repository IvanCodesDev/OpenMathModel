from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..avatar import AVATAR_MEDIA_TYPES, sniff_avatar_media_type
from ..config import RECOVERY_CODE_COUNT
from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError
from ..models import AuthSession, RecoveryCode, User, new_id, utcnow
from ..schemas import (
    CodeRequest,
    LlmConfigUpdateRequest,
    PasswordChangeRequest,
    PasswordRequest,
    PreferencesUpdateRequest,
    ProfileUpdateRequest,
    llm_config_payload,
    preferences_payload,
    security_payload,
    session_payload,
    user_payload,
)
from ..security import (
    build_otpauth_uri,
    generate_recovery_codes,
    generate_totp_secret,
    hash_password,
    hash_recovery_code,
    verify_password,
    verify_totp,
)

router = APIRouter(tags=["account"])


def _recovery_remaining(db: Session, user_id: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(RecoveryCode).where(
                RecoveryCode.user_id == user_id,
                RecoveryCode.used_at.is_(None),
            )
        )
        or 0
    )


def _replace_recovery_codes(db: Session, user: User) -> list[str]:
    for record in db.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)):
        db.delete(record)
    codes = generate_recovery_codes(RECOVERY_CODE_COUNT)
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)))
    return codes


@router.get("/me")
def me(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    return {
        "user": user_payload(ctx.user),
        "security": security_payload(ctx.user, _recovery_remaining(db, ctx.user.id)),
    }


@router.patch("/profile")
def update_profile(
    body: ProfileUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if body.name is not None:
        user.name = body.name

    if body.email is not None and body.email != user.email:
        if not body.password:
            raise ApiError(400, "PASSWORD_REQUIRED", "修改邮箱需要输入当前密码")
        if not verify_password(body.password, user.password_hash):
            raise ApiError(401, "INVALID_PASSWORD", "当前密码不正确")
        taken = db.scalar(select(User).where(func.lower(User.email) == body.email, User.id != user.id))
        if taken is not None:
            raise ApiError(409, "EMAIL_TAKEN", "该邮箱已被其他账户使用")
        user.email = body.email

    db.commit()
    return {"user": user_payload(user)}


# ── 用户偏好（高级设置里需要服务端生效的部分） ───────────────────


@router.get("/preferences")
def get_preferences(ctx: AuthContext = Depends(get_auth_context)):
    return {"preferences": preferences_payload(ctx.user)}


@router.put("/preferences")
def update_preferences(
    body: PreferencesUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """最大并发任务上限存服务端而不是浏览器：创建任务的闸门在这里校验，
    放 localStorage 改个缓存就能绕过。"""
    ctx.user.max_concurrent_runs = body.max_concurrent_runs
    db.commit()
    return {"preferences": preferences_payload(ctx.user)}


# ── 自定义模型接口配置（设置中心「自定义 API」） ──────────────────


@router.get("/llm-config")
def get_llm_config(ctx: AuthContext = Depends(get_auth_context)):
    return {"config": llm_config_payload(ctx.user)}


@router.put("/llm-config")
def update_llm_config(
    body: LlmConfigUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """整体替换保存。密钥存服务端而不是浏览器：对话回复与任务执行都在
    服务端出网调用模型，密钥留在本机后端、不随页面分发。"""
    endpoints = []
    for endpoint in body.endpoints:
        data = endpoint.model_dump()
        data["id"] = data["id"] or f"ep_{new_id()[:12]}"
        endpoints.append(data)
    known_ids = {e["id"] for e in endpoints}
    active = body.active_endpoint_id if body.active_endpoint_id in known_ids else None
    ctx.user.llm_config = {
        "endpoints": endpoints,
        "active_endpoint_id": active or (endpoints[0]["id"] if endpoints else None),
        "allow_proxy": body.allow_proxy,
        "stream": body.stream,
        "fallback": body.fallback,
    }
    db.commit()
    return {"config": llm_config_payload(ctx.user)}


# ── 头像 ─────────────────────────────────────────────────────────


@router.post("/avatar")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """上传头像：按魔数确认格式后内容寻址存储，库内只保留引用。"""
    settings = request.app.state.settings
    content = await file.read()
    if not content:
        raise ApiError(422, "VALIDATION_ERROR", "头像文件为空")
    if len(content) > settings.avatar_max_bytes:
        raise ApiError(
            413,
            "PAYLOAD_TOO_LARGE",
            f"头像不能超过 {settings.avatar_max_bytes // 1024} KB",
        )

    media_type = sniff_avatar_media_type(content)
    if media_type is None:
        raise ApiError(
            422,
            "UNSUPPORTED_IMAGE",
            f"仅支持 {'、'.join(t.removeprefix('image/').upper() for t in AVATAR_MEDIA_TYPES)} 图片",
        )

    sha256, _ = request.app.state.avatars.put(content)
    ctx.user.avatar_sha256 = sha256
    ctx.user.avatar_media_type = media_type
    db.commit()
    return {"user": user_payload(ctx.user)}


@router.delete("/avatar")
def delete_avatar(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    """移除头像（幂等）：回落到姓名首字母。内容对象由存储层统一回收。"""
    ctx.user.avatar_sha256 = None
    ctx.user.avatar_media_type = None
    db.commit()
    return {"user": user_payload(ctx.user)}


@router.get("/avatar")
def get_avatar(request: Request, ctx: AuthContext = Depends(get_auth_context)) -> Response:
    """返回当前登录用户的头像内容；仅本人可读，不按 user_id 暴露他人头像。"""
    user = ctx.user
    if not user.avatar_sha256:
        raise ApiError(404, "AVATAR_NOT_FOUND", "该账户尚未设置头像")

    try:
        handle = request.app.state.avatars.open(user.avatar_sha256)
        if handle is None:
            raise ApiError(404, "AVATAR_NOT_FOUND", "头像内容对象缺失")
        with handle:
            content = handle.read()
    except OSError as exc:
        raise ApiError(404, "AVATAR_NOT_FOUND", "头像内容对象不可读") from exc

    # 媒体类型取服务端识别值；nosniff 阻止浏览器把内容重新猜成可执行类型。
    media_type = user.avatar_media_type
    return Response(
        content=content,
        media_type=media_type if media_type in AVATAR_MEDIA_TYPES else "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/password")
def change_password(
    body: PasswordChangeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if not verify_password(body.current_password, user.password_hash):
        raise ApiError(401, "INVALID_PASSWORD", "当前密码不正确")
    if body.current_password == body.new_password:
        raise ApiError(400, "PASSWORD_UNCHANGED", "新密码不能与当前密码相同")

    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()

    # 修改密码后退出除当前设备外的所有会话
    revoked = 0
    for session in db.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
        if session.id != ctx.session.id and session.revoked_at is None:
            session.revoked_at = utcnow()
            revoked += 1
    db.commit()
    return {"ok": True, "revoked_sessions": revoked}


# ── 双重验证 ─────────────────────────────────────────────────────


@router.get("/2fa/setup")
def two_factor_setup(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    user = ctx.user
    if user.totp_enabled:
        raise ApiError(409, "ALREADY_ENABLED", "双重验证已启用")

    if not user.totp_secret:
        user.totp_secret = generate_totp_secret()
        db.commit()

    return {
        "secret": user.totp_secret,
        "otpauth_uri": build_otpauth_uri(user.totp_secret, user.email),
    }


@router.post("/2fa/enable")
def two_factor_enable(
    body: CodeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if user.totp_enabled:
        raise ApiError(409, "ALREADY_ENABLED", "双重验证已启用")
    if not user.totp_secret:
        raise ApiError(400, "SETUP_REQUIRED", "请先获取验证器密钥")
    if not verify_totp(user.totp_secret, body.code):
        raise ApiError(401, "INVALID_CODE", "验证码不正确，请确认验证器时间同步")

    user.totp_enabled = True
    codes = _replace_recovery_codes(db, user)
    db.commit()
    return {"ok": True, "recovery_codes": codes}


@router.post("/2fa/disable")
def two_factor_disable(
    body: PasswordRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if not user.totp_enabled:
        raise ApiError(409, "NOT_ENABLED", "双重验证未启用")
    if not verify_password(body.password, user.password_hash):
        raise ApiError(401, "INVALID_PASSWORD", "当前密码不正确")

    user.totp_enabled = False
    user.totp_secret = None
    for record in db.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)):
        db.delete(record)
    db.commit()
    return {"ok": True}


@router.post("/2fa/recovery-codes")
def regenerate_recovery_codes(
    body: PasswordRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if not user.totp_enabled:
        raise ApiError(409, "NOT_ENABLED", "启用双重验证后才能生成恢复代码")
    if not verify_password(body.password, user.password_hash):
        raise ApiError(401, "INVALID_PASSWORD", "当前密码不正确")

    codes = _replace_recovery_codes(db, user)
    db.commit()
    return {"ok": True, "recovery_codes": codes}


# ── 登录设备 ─────────────────────────────────────────────────────


@router.get("/sessions")
def list_sessions(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    now = utcnow()
    sessions = db.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == ctx.user.id)
        .order_by(AuthSession.last_seen_at.desc())
    ).all()
    active = [s for s in sessions if s.is_active(now)]
    return {"sessions": [session_payload(s, ctx.session.id) for s in active]}


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    if session_id == ctx.session.id:
        raise ApiError(400, "CANNOT_REVOKE_CURRENT", "不能退出当前设备，请使用退出登录")

    session = db.get(AuthSession, session_id)
    if session is None or session.user_id != ctx.user.id or not session.is_active():
        raise ApiError(404, "SESSION_NOT_FOUND", "设备会话不存在或已退出")

    session.revoked_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/sessions/revoke-others")
def revoke_other_sessions(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    revoked = 0
    for session in db.scalars(select(AuthSession).where(AuthSession.user_id == ctx.user.id)):
        if session.id != ctx.session.id and session.revoked_at is None:
            session.revoked_at = utcnow()
            revoked += 1
    db.commit()
    return {"ok": True, "revoked_sessions": revoked}
