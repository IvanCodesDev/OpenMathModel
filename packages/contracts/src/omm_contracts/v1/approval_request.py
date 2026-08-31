# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, constr


class DecisionType(Enum):
    confirm_plan = "confirm_plan"
    confirm_method = "confirm_method"
    confirm_results = "confirm_results"
    generic = "generic"


class Option(BaseModel):
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


class Status(Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class Resolution(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    option_id: constr(min_length=1, max_length=100)
    actor: constr(min_length=1, max_length=200)
    comment: constr(max_length=2000) | None = None
    resolved_at: Timestamp


class ApprovalRequest(BaseModel):
    """
    人工确认（HIL）请求。人工确认是正式状态转换：审批解决后运行才能离开 WAITING_APPROVAL。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^appr_[0-9a-f]{32}$")
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$")
    step_id: constr(pattern=r"^step_[0-9a-f]{32}$") | None = None
    decision_type: DecisionType
    title: constr(min_length=1, max_length=500)
    description: constr(max_length=4000) | None = None
    options: list[Option] = Field(..., min_length=1)
    evidence_snapshot_id: constr(max_length=200) | None = Field(
        None, description="审批所依据的证据快照引用；Evidence 体系落地前允许为空。"
    )
    status: Status
    resolution: Resolution | None = None
    expires_at: Timestamp | None = None
    created_at: Timestamp
