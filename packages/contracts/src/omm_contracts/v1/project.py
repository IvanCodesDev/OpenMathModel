# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class Mode(Enum):
    """
    产品模式，见规划文档 §1.2。
    """

    learning = "learning"
    collaboration = "collaboration"
    auto_experiment = "auto_experiment"
    review = "review"
    organization = "organization"


class ProjectId(RootModel[constr(pattern=r"^proj_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^proj_[0-9a-f]{32}$")


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class Status(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class LatestRun(BaseModel):
    """
    该项目最新一次运行的轻量投影（按创建时间取最近）；从未发起运行时为 null。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: RunId
    status: Status
    current_node: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100) = Field(
        ...,
        description="领域阶段节点，随 workflow_version 演进；消费方必须容忍未知节点名。",
    )
    goal: constr(min_length=1, max_length=4000)
    updated_at: Timestamp


class ProjectStats(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    latest_run: LatestRun | None = Field(
        ...,
        description="该项目最新一次运行的轻量投影（按创建时间取最近）；从未发起运行时为 null。",
    )
    artifact_count: conint(ge=0) = Field(
        ..., description="项目产物总条数（运行产出与手动上传都计入）。"
    )


class Project(BaseModel):
    """
    一个持续存在的建模项目。领域对象事实来源为 PostgreSQL，本契约描述 API 对外表示。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: ProjectId
    name: constr(min_length=1, max_length=200)
    owner: constr(min_length=1, max_length=200) = Field(
        ...,
        description="所有者标识。MVP 单用户阶段固定为 local-dev，接入认证后为用户 ID。",
    )
    mode: Mode = Field(..., description="产品模式，见规划文档 §1.2。")
    competition_policy: constr(max_length=200) | None = Field(
        None, description="CompetitionPolicyProfile 引用 ID；MVP 允许为空。"
    )
    workspace_uri: constr(max_length=1000) | None = Field(
        None, description="工作区根 URI（对象存储前缀或本地路径）。"
    )
    description: constr(max_length=2000) | None = None
    created_at: Timestamp
    updated_at: Timestamp
    stats: ProjectStats | None = Field(
        None,
        description="列表统计投影：仅 GET /v1/projects?include=stats 计算并返回对象，其余端点为 null 或缺省。服务端一次聚合，客户端不再按项目逐个拉取运行与产物。",
    )
