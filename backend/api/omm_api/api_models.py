"""列表响应包装模型（与 OpenAPI components 对齐）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

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


class ProjectUpdateInput(BaseModel):
    """项目维护输入（侧栏「最近任务」的重命名与归档）；两项都可省略。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    #: true = 归档（从默认列表消失），false = 取消归档；None = 不改动。
    archived: Optional[bool] = None


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
    #: 文档内嵌图片数（近似值，ADR-0010）；null = 该格式不统计或计数失败。
    images: Optional[int] = None
    detail: Optional[str] = None
    text: str


class AttachmentParseResult(BaseModel):
    """对话附件的即席解析结果（ADR-0010 批次三）。

    与 ``ArtifactText`` 同一套状态语义，但不落库、不建产物：对话历史保存在页面
    内存、服务端无状态，附件解析也保持同样的隐私姿态。
    """

    name: str
    media_type: str
    status: Literal["ready", "partial", "empty", "unsupported", "failed"]
    engine: str
    characters: int
    segments: Optional[int] = None
    images: Optional[int] = None
    detail: Optional[str] = None
    text: str
