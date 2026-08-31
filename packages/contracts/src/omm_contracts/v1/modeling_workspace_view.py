# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class RunStatus(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Pages(BaseModel):
    pass


class PageKey(Enum):
    running = "running"
    data = "data"
    model = "model"
    experiments = "experiments"
    editor = "editor"
    complete = "complete"


class Route(Enum):
    field_task_running = "/task/running"
    field_workspace_data = "/workspace/data"
    field_workspace_model_plan = "/workspace/model-plan"
    field_workspace_experiments = "/workspace/experiments"
    field_workspace_paper_editor = "/workspace/paper-editor"
    field_task_complete = "/task/complete"


class PageStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentState(Enum):
    QUEUED = "QUEUED"
    WORKING = "WORKING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentActionKind(Enum):
    approve = "approve"
    navigate = "navigate"
    pause = "pause"
    resume = "resume"
    retry = "retry"
    none = "none"


class ApproveAgentAction(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    kind: Literal["approve"]
    label: constr(min_length=1, max_length=100)
    target_route: Route
    approval_id: constr(pattern=r"^appr_[0-9a-f]{32}$")
    option_id: constr(max_length=100) | None


class NavigateAgentAction(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    kind: Literal["navigate"]
    label: constr(min_length=1, max_length=100)
    target_route: Route
    approval_id: None
    option_id: None


class Kind(Enum):
    pause = "pause"
    resume = "resume"
    retry = "retry"


class TaskAgentAction(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    kind: Kind
    label: constr(min_length=1, max_length=100)
    target_route: Route
    approval_id: None
    option_id: None


class NoneAgentAction(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    kind: Literal["none"]
    label: constr(min_length=1, max_length=100)
    target_route: None
    approval_id: None
    option_id: None


class AgentAction(
    RootModel[
        ApproveAgentAction | NavigateAgentAction | TaskAgentAction | NoneAgentAction
    ]
):
    root: ApproveAgentAction | NavigateAgentAction | TaskAgentAction | NoneAgentAction


class AgentProjection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    state: AgentState
    title: constr(min_length=1, max_length=200)
    summary: constr(min_length=1, max_length=4000)
    current_step: constr(min_length=1, max_length=300)
    action: AgentAction


class Node(RootModel[constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)]):
    root: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)


class PageProjection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    key: PageKey
    label: constr(min_length=1, max_length=100)
    route: Route
    nodes: list[Node] = Field(..., min_length=1)
    status: PageStatus
    artifact_ids: list[constr(pattern=r"^art_[0-9a-f]{32}$")]
    plan_text: constr(min_length=1, max_length=300) | None = Field(
        None,
        description="本任务专属的计划短句（问题分析的 plan_outline 派生，方案确认后实验条目细化为选中方案）；未产出时为 null，展示层回退 label。",
    )


class Kind1(Enum):
    dataset = "dataset"
    code = "code"
    figure = "figure"
    table = "table"
    log = "log"
    report = "report"
    paper = "paper"
    model = "model"
    other = "other"


class Status(Enum):
    PENDING = "PENDING"
    READY = "READY"
    STALE = "STALE"
    DELETED = "DELETED"


class ArtifactProjection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^art_[0-9a-f]{32}$")
    kind: Kind1
    name: constr(min_length=1, max_length=300)
    media_type: constr(min_length=1, max_length=255)
    size_bytes: conint(ge=0) | None
    status: Status
    producer_node: constr(max_length=100) | None
    download_url: (
        constr(pattern=r"^/api/v1/artifacts/art_[0-9a-f]{32}/download$") | None
    )


class ApprovalOption(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(min_length=1, max_length=100)
    label: constr(min_length=1, max_length=200)
    description: constr(max_length=1000) | None = None
    recommended: bool | None = Field(
        None,
        description="AI 推荐项标记：多正向选项的闸门（如 G2 数据闸门）用它声明默认选择；至多一个选项为 true，null/缺省等价于 false。",
    )


class ApprovalProjection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^appr_[0-9a-f]{32}$")
    title: constr(min_length=1, max_length=300)
    description: constr(max_length=2000) | None
    options: list[ApprovalOption] = Field(..., min_length=1)


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class ModelingWorkspaceView(BaseModel):
    """
    建模运行面向 Web 的语义投影。后端只输出阶段、Agent 文案、动作和产物语义，前端负责映射到既有流程页面与 DOM 样式。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$")
    project_id: constr(pattern=r"^proj_[0-9a-f]{32}$")
    project_name: constr(min_length=1, max_length=200)
    goal: constr(min_length=1, max_length=4000)
    workflow_version: constr(min_length=1, max_length=100)
    run_status: RunStatus
    active_node: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    active_page: PageKey
    suggested_route: Route
    agent: AgentProjection
    pages: list[PageProjection] | Pages = Field(..., max_length=6, min_length=6)
    artifacts: list[ArtifactProjection]
    pending_approval: ApprovalProjection | None
    latest_event_sequence: conint(ge=1) | None
    updated_at: Timestamp
