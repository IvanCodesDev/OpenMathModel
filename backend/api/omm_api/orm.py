"""ORM 模型。列结构与 packages/contracts/schemas 对齐；写法保持 PostgreSQL 兼容。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # v1 契约必填；MVP 单用户阶段固定 local-dev，接入认证后写用户 ID
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="local-dev")
    description: Mapped[Optional[str]] = mapped_column(String(2000))
    mode: Mapped[Optional[str]] = mapped_column(String(50))
    competition_policy: Mapped[Optional[str]] = mapped_column(String(100))
    workspace_uri: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskRunRow(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    auto_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_node: Mapped[Optional[str]] = mapped_column(String(100))
    paused_from_status: Mapped[Optional[str]] = mapped_column(String(50))
    failure_class: Mapped[Optional[str]] = mapped_column(String(50))
    failure_message: Mapped[Optional[str]] = mapped_column(String(4000))
    budget: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    params: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class StepRunRow(Base):
    __tablename__ = "step_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "node", "attempt", name="uq_step_runs_run_node_attempt"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_runs.id"), nullable=False, index=True
    )
    node: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    input_hash: Mapped[Optional[str]] = mapped_column(String(64))
    failure_class: Mapped[Optional[str]] = mapped_column(String(50))
    detail: Mapped[Optional[str]] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AgentEventRow(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        Index("ix_agent_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_runs.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False, index=True
    )
    run_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("task_runs.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    uri: Mapped[Optional[str]] = mapped_column(String(1000))
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    media_type: Mapped[Optional[str]] = mapped_column(String(200))
    producer_step: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactTextRow(Base):
    """附件正文抽取结果缓存。

    抽取按需触发、结果长期有效：产物是内容寻址的，同一个 artifact 的字节永远
    不会变，因此一次抽取可以一直复用。失败与不支持也要落库，否则每次 Agent
    读取都会重跑一遍注定失败的解析。
    """

    __tablename__ = "artifact_texts"

    artifact_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("artifacts.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    segments: Mapped[Optional[int]] = mapped_column(Integer)
    detail: Mapped[Optional[str]] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmailVerificationCodeRow(Base):
    """注册邮箱验证码：一次性、限时（哈希存储，不落明文）。

    时间列与 users/auth_sessions 一致使用 naive-UTC（models.utcnow），
    避免与认证流的时间比较出现 tz-aware/naive 混用。
    """

    __tablename__ = "email_verification_codes"
    __table_args__ = (Index("ix_email_codes_email_time", "email", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LoginAttemptRow(Base):
    """登录失败记录：数据库限速器的窗口计数依据（多实例一致）。"""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_key_time", "key", "attempted_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DomainEventRow(Base):
    """领域事件日志（执行事实来源）：agents/core 引擎的 append-only 历史。

    v1 行（task_runs/step_runs/agent_events…）是它的投影；重放本表即可重建快照。
    created_at 保存引擎时钟的 ISO 字符串原文，保证重放逐字节一致。
    """

    __tablename__ = "run_domain_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_domain_events_run_seq"),
        Index("ix_run_domain_events_run_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_runs.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class IdempotencyRecord(Base):
    """写操作幂等记录：同 key 同签名重放首次响应，同 key 不同签名 409。"""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRequestRow(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_runs.id"), nullable=False, index=True
    )
    decision_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # 幂等：同 client_token 重复 approve 返回同一结果
    client_token: Mapped[Optional[str]] = mapped_column(String(64))


class LlmUsageRow(Base):
    """一次成功的模型调用 = 一行（设置中心「用量监控」的数据源）。

    user_id 不设外键：用量是历史事实，须在用户删除后仍可审计；
    Agent 任务经项目归属找不到用户时按项目 owner 原值记录（如 local-dev）。
    费用不落库：按 usage.PRICING 在读取时估算，调价无需回填数据。
    """

    __tablename__ = "llm_usage_records"
    __table_args__ = (Index("ix_llm_usage_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: chat = 对话页；agent = 任务引擎节点；test = 测试连接；route = Auto 难度判定。
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    endpoint_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    third_party: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
