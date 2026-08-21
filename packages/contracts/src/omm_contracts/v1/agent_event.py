# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class Type(Enum):
    run_created = "run.created"
    run_status_changed = "run.status_changed"
    run_node_changed = "run.node_changed"
    run_log = "run.log"
    step_started = "step.started"
    step_succeeded = "step.succeeded"
    step_failed = "step.failed"
    approval_requested = "approval.requested"
    approval_resolved = "approval.resolved"
    artifact_published = "artifact.published"
    paper_export_finished = "paper.export.finished"


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class AgentEvent(BaseModel):
    """
    供 Web/Desktop 实时呈现的统一事件信封。数据库事件表是时间线事实来源（当前默认 SQLite，目标部署 PostgreSQL）；sequence 在 run 内单调递增且唯一，SSE 的事件 id 即 sequence。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^evt_[0-9a-f]{32}$")
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$")
    sequence: conint(ge=1)
    step_id: constr(pattern=r"^step_[0-9a-f]{32}$") | None = None
    type: Type
    payload: dict[str, Any] = Field(
        ..., description="按 type 解释的载荷。消费方必须容忍未知字段。"
    )
    created_at: Timestamp
