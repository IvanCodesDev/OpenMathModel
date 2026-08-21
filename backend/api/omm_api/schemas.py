from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

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


class PrivacySettingsUpdateRequest(BaseModel):
    """设置中心「数据与隐私」九个面板项；保留与缓存策略由服务端清扫执行。"""

    save_history: bool = True
    local_first: bool = True
    model_training: bool = False
    retention: Literal["forever", "days_90", "days_30", "on_complete"] = "forever"
    file_cache: Literal["days_30", "days_7", "on_close"] = "days_30"
    notify_task_done: bool = True
    notify_budget: bool = True
    notify_security: bool = True
    email_digest: bool = False


# ── 自定义模型接口（设置中心「自定义 API」） ─────────────────────

_LLM_PROTOCOLS = ("openai", "anthropic", "gemini", "ollama", "custom")


class LlmEndpointModel(BaseModel):
    """一条已保存接口；字段与设置面板一一对应，密钥仅存本机后端。"""

    id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    protocol: str = "openai"
    base_url: str = Field(max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    organization: str = Field(default="", max_length=200)
    headers: str = Field(default="", max_length=2000)
    path_prefix: str = Field(default="", max_length=200)
    #: 模型能力权重（1-10，Auto 模式按它路由）；0 = 未设置，按模型名自动推断。
    weight: int = Field(default=0, ge=0, le=10)

    @field_validator("protocol")
    @classmethod
    def check_protocol(cls, value: str) -> str:
        if value not in _LLM_PROTOCOLS:
            raise ValueError(f"接口协议必须是 {', '.join(_LLM_PROTOCOLS)} 之一")
        return value

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        return url

    @field_validator("path_prefix")
    @classmethod
    def check_path_prefix(cls, value: str) -> str:
        prefix = value.strip()
        if prefix and not prefix.startswith("/"):
            raise ValueError("路径前缀必须以 / 开头")
        return prefix


class LlmConfigUpdateRequest(BaseModel):
    """整体替换式保存：与设置面板「保存更改」语义一致。"""

    endpoints: list[LlmEndpointModel] = Field(default_factory=list, max_length=20)
    active_endpoint_id: Optional[str] = None
    allow_proxy: bool = True
    stream: bool = True
    fallback: bool = True


class LlmTestRequest(LlmEndpointModel):
    """「测试连接」直接携带表单当前值，无需先保存。"""

    allow_proxy: bool = True


class UsageSettingsUpdateRequest(BaseModel):
    """设置中心「用量监控」的三个预算项；硬限制的闸门在服务端执行。"""

    #: None = 未设置预算（不提醒、硬限制不生效）。
    monthly_budget_cny: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    budget_threshold_percent: int = Field(default=80, ge=1, le=100)
    hard_limit: bool = False


class ChatMessageModel(BaseModel):
    role: str
    content: str = Field(max_length=100_000)

    @field_validator("role")
    @classmethod
    def check_role(cls, value: str) -> str:
        if value not in ("system", "user", "assistant"):
            raise ValueError("消息角色必须是 system / user / assistant")
        return value


class ChatRouteStateModel(BaseModel):
    """Auto 路由的会话内状态：前端保存上一轮 route meta 并随下一条消息回传。

    服务端保持无状态（对话历史与路由状态都由前端携带），据此实现「短追问
    继承难度」与「接口粘性」两个省 token 策略；缺省等价于会话首轮。
    """

    difficulty: Optional[int] = Field(default=None, ge=1, le=5)
    endpoint_id: Optional[str] = Field(default=None, max_length=64)
    #: 距上次真实判定过去的轮数：meta.judged=true 时前端清零，否则自增。
    turns: int = Field(default=0, ge=0, le=1000)


class ChatRequest(BaseModel):
    messages: list[ChatMessageModel] = Field(min_length=1, max_length=100)
    # None = 跟随设置中心「流式输出」开关
    stream: Optional[bool] = None
    model: Optional[str] = Field(default=None, max_length=200)
    # "auto" = 先判定问题难度，再按接口权重路由到强弱合适的模型
    route: Optional[str] = Field(default=None, max_length=20)
    # 指定某条已保存接口作为本次主接口（模型选择器手动选中时携带）
    endpoint_id: Optional[str] = Field(default=None, max_length=64)
    # Auto 路由的判定输入：用户原始问题，不含前端注入的任务/附件/模式指令块
    # （那些块既偏置难度又浪费判定 token）。缺省回落最后一条 user 消息全文。
    route_question: Optional[str] = Field(default=None, max_length=8_000)
    # Auto 路由的判定微上下文（如上一轮回复首行）：只在真实重判时并入提示词。
    route_context: Optional[str] = Field(default=None, max_length=500)
    route_state: Optional[ChatRouteStateModel] = None


# ── 响应体构造 ───────────────────────────────────────────────────


def preferences_payload(user: User) -> dict:
    return {
        "max_concurrent_runs": user.max_concurrent_runs or DEFAULT_MAX_CONCURRENT_RUNS,
    }


def llm_config_payload(user: User) -> dict:
    """返回完整配置（含密钥）：仅本人可读，本机部署下等价于“保存在本机”。"""
    raw = user.llm_config if isinstance(user.llm_config, dict) else {}
    endpoints = [e for e in (raw.get("endpoints") or []) if isinstance(e, dict)]
    return {
        "endpoints": endpoints,
        "active_endpoint_id": raw.get("active_endpoint_id") or (endpoints[0].get("id") if endpoints else None),
        "allow_proxy": bool(raw.get("allow_proxy", True)),
        "stream": bool(raw.get("stream", True)),
        "fallback": bool(raw.get("fallback", True)),
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
