"""ORM 行 → 契约模型（schemas/v1 形状）。

- v1 时间戳是 UTC ISO-8601 字符串（Z 结尾）；SQLite 读出 naive datetime，统一补 UTC 后格式化。
- 响应契约 additionalProperties:false：内部字段（auto_start / paused_from_status /
  step.detail / artifact.name 等）不进入契约载荷。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from omm_contracts import (
    AgentEvent,
    ApprovalRequest,
    Artifact,
    PaperExport,
    Project,
    StepRun,
    TaskRun,
)

from . import orm


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def iso_z(value: Optional[datetime]) -> Optional[str]:
    """v1 契约时间戳：UTC、微秒六位、Z 结尾。"""
    normalized = as_utc(value)
    if normalized is None:
        return None
    return normalized.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def project_to_contract(
    row: orm.ProjectRow, stats: Optional[dict[str, Any]] = None
) -> Project:
    """stats 只在列表 include=stats 时由路由聚合传入；其余端点保持 null。"""
    return Project(
        id=row.id,
        name=row.name,
        owner=row.owner,
        mode=row.mode,
        competition_policy=row.competition_policy,
        workspace_uri=row.workspace_uri,
        description=row.description,
        created_at=iso_z(row.created_at),
        updated_at=iso_z(row.updated_at),
        stats=stats,
    )


def task_run_to_contract(row: orm.TaskRunRow) -> TaskRun:
    failure: Optional[dict[str, Any]] = None
    if row.failure_class:
        failure = {
            "failure_class": row.failure_class,
            "message": row.failure_message or "（无失败详情）",
        }
    return TaskRun(
        id=row.id,
        project_id=row.project_id,
        goal=row.goal,
        workflow_version=row.workflow_version,
        status=row.status,
        current_node=row.current_node,
        budget=row.budget,
        params=row.params,
        parent_run_id=None,
        failure=failure,
        created_at=iso_z(row.created_at),
        updated_at=iso_z(row.updated_at),
        started_at=iso_z(row.started_at),
        ended_at=iso_z(row.ended_at),
    )


def step_run_to_contract(row: orm.StepRunRow) -> StepRun:
    return StepRun(
        id=row.id,
        run_id=row.run_id,
        node=row.node,
        attempt=row.attempt,
        input_hash=row.input_hash,
        status=row.status,
        failure_class=row.failure_class,
        # detail 是内部进度文案；契约只在失败时暴露失败信息
        failure_message=row.detail if row.failure_class else None,
        created_at=iso_z(row.created_at),
        started_at=iso_z(row.started_at),
        ended_at=iso_z(row.ended_at),
    )


def agent_event_to_contract(row: orm.AgentEventRow) -> AgentEvent:
    return AgentEvent(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        type=row.type,
        payload=row.payload or {},
        created_at=iso_z(row.created_at),
    )


def artifact_to_contract(row: orm.ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        project_id=row.project_id,
        run_id=row.run_id,
        kind=row.kind,
        uri=row.uri,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        producer_step_id=row.producer_step,
        inputs=row.inputs or [],  # 历史行无血缘列，按空列表处理
        status=row.status,
        created_at=iso_z(row.created_at),
    )


def paper_export_to_contract(row: orm.PaperExportRow) -> PaperExport:
    return PaperExport(
        id=row.id,
        project_id=row.project_id,
        run_id=row.run_id,
        format=row.format,
        status=row.status,
        artifact_id=row.artifact_id,
        source_artifact_id=row.source_artifact_id,
        detail=row.detail,
        created_at=iso_z(row.created_at),
        started_at=iso_z(row.started_at),
        ended_at=iso_z(row.ended_at),
    )


def approval_to_contract(row: orm.ApprovalRequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        run_id=row.run_id,
        decision_type=row.decision_type,
        title=row.title,
        description=None,
        options=row.options,
        evidence_snapshot_id=None,  # Evidence 体系落地前允许为空（v1 契约注释）
        status=row.status,
        resolution=row.resolution,
        expires_at=iso_z(row.expires_at),
        created_at=iso_z(row.requested_at),
    )
