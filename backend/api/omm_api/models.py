from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """统一存储无时区的 UTC 时间，序列化时补 Z 后缀。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(128))
    plan: Mapped[str] = mapped_column(String(32), default="个人专业版")
    # 头像二进制存内容寻址存储，库里只保留引用；media_type 由服务端按文件魔数识别后回写。
    avatar_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    avatar_media_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 高级设置「最大并发任务」：None = 沿用部署默认值（config.DEFAULT_MAX_CONCURRENT_RUNS）。
    # 可空是刻意的——SQLite 开发库靠启动补列机制加新列，只有可空列能补。
    max_concurrent_runs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 设置中心「自定义 API」：已保存接口列表 + 主接口 + 三个行为开关（llm.parse_llm_config 解析）。
    # 密钥只落在本机后端数据库，不回传云端；None = 从未配置过。
    llm_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 设置中心「用量监控」：月度预算 / 提醒阈值 / 硬限制（usage.usage_settings_of 解析）。
    # 硬限制的闸门在服务端聊天与任务执行路径上，改浏览器缓存绕不过。
    usage_settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recovery_codes: Mapped[list["RecoveryCode"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base):
    """一次登录 = 一条会话记录，直接支撑“登录设备”列表与撤销。"""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    browser: Mapped[str] = mapped_column(String(40), default="未知浏览器")
    os_name: Mapped[str] = mapped_column(String(40), default="未知系统")
    device_label: Mapped[str] = mapped_column(String(120), default="未知设备")
    kind: Mapped[str] = mapped_column(String(16), default="desktop")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    def is_active(self, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        return self.revoked_at is None and self.expires_at > now


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="recovery_codes")
