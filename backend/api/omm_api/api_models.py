"""列表响应包装模型（与 OpenAPI components 对齐）。"""

from __future__ import annotations

from typing import Literal, Optional

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


class ArtifactText(BaseModel):
    """附件正文抽取结果。

    ``status`` 分五档：ready 完整抽出、partial 触顶截断、empty 文件正常但没有
    文字、unsupported 缺少可选依赖或格式不支持、failed 文件损坏或抽取出错。
    后三档也是正常响应（200）——调用方需要的是原因，而不是一个错误码。
    """

    artifact_id: str
    name: str
    media_type: str
    status: Literal["ready", "partial", "empty", "unsupported", "failed"]
    engine: str
    characters: int
    segments: Optional[int] = None
    detail: Optional[str] = None
    text: str
