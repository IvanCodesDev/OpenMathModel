# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, constr


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


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
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
