from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .config import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    MAX_CONCURRENT_RUNS_CEILING,
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_LENGTH,
)
from .models import AuthSession, User

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_PATTERN.fullmatch(email) or len(email) > 255:
        raise ValueError("邮箱格式不正确")
    return email


def _validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少 {PASSWORD_MIN_LENGTH} 个字符")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError("密码过长")
    return value


def iso_utc(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


# ── 请求体 ───────────────────────────────────────────────────────


class SendEmailCodeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _validate_email(value)


class RegisterRequest(BaseModel):
    email: str
    # 邮箱验证码（先调 /register/send-code 获取）
    code: str = Field(min_length=4, max_length=8)
    password: str
    name: str = Field(min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _validate_email(value)

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        return _validate_password(value)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("名称不能为空")
        return name


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _validate_email(value)


class TwoFaLoginRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=4, max_length=16)


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    email: Optional[str] = None
    password: Optional[str] = None

    @field_validator("email")
    @classmethod
    def check_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_email(value)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("名称不能为空")
        return name


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def check_password(cls, value: str) -> str:
        return _validate_password(value)


class CodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=16)


class PasswordRequest(BaseModel):
    password: str


class PreferencesUpdateRequest(BaseModel):
    """高级设置里需要服务端生效的用户偏好；纯本机偏好仍走 localStorage。"""

    max_concurrent_runs: int = Field(ge=1, le=MAX_CONCURRENT_RUNS_CEILING)


# ── 响应体构造 ───────────────────────────────────────────────────


def preferences_payload(user: User) -> dict:
    return {
        "max_concurrent_runs": user.max_concurrent_runs or DEFAULT_MAX_CONCURRENT_RUNS,
    }


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "plan": user.plan,
        "avatar_letter": (user.name[:1] or user.email[:1]).upper(),
        # 摘要进查询串：换头像后 URL 随之变化，浏览器不会命中旧图缓存。
        "avatar_url": f"/api/account/avatar?v={user.avatar_sha256[:16]}" if user.avatar_sha256 else None,
        "created_at": iso_utc(user.created_at),
    }


def security_payload(user: User, recovery_codes_remaining: int) -> dict:
    return {
        "password_changed_at": iso_utc(user.password_changed_at),
        "two_factor_enabled": user.totp_enabled,
        "recovery_codes_remaining": recovery_codes_remaining,
    }


def session_payload(session: AuthSession, current_session_id: str) -> dict:
    return {
        "id": session.id,
        "device_label": session.device_label,
        "browser": session.browser,
        "os": session.os_name,
        "kind": session.kind,
        "ip": session.ip,
        "created_at": iso_utc(session.created_at),
        "last_seen_at": iso_utc(session.last_seen_at),
        "current": session.id == current_session_id,
    }
