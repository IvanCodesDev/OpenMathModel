"""列表响应包装模型（与 OpenAPI components 对齐）。"""

from __future__ import annotations

from pydantic import BaseModel

from omm_contracts import (
    AgentEvent,
    ApprovalRequest,
    Artifact,
    Project,
    StepRun,
    TaskRun,
)


class ProjectList(BaseModel):
    items: list[Project]
    total: int


class TaskRunList(BaseModel):
    items: list[TaskRun]
    total: int


class StepRunList(BaseModel):
    items: list[StepRun]


class ApprovalList(BaseModel):
    items: list[ApprovalRequest]


class AgentEventList(BaseModel):
    items: list[AgentEvent]


class ArtifactList(BaseModel):
    items: list[Artifact]
    total: int
