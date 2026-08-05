# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, confloat, conint, constr


class Status(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Budget(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    max_wall_time_s: conint(ge=1) | None = None
    max_model_calls: conint(ge=0) | None = None
    cost_limit_usd: confloat(ge=0.0) | None = None


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class NullableTimestamp(
    RootModel[
        constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") | None
    ]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") | None


class FailureClass(Enum):
    """
    失败分类，见规划文档 §8.6。
    """

    TRANSIENT = "TRANSIENT"
    TOOL_ENV = "TOOL_ENV"
    CODE_DEFECT = "CODE_DEFECT"
    METHOD_INVALID = "METHOD_INVALID"
    DATA_DEFECT = "DATA_DEFECT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    POLICY_BLOCK = "POLICY_BLOCK"
    NON_PROGRESS = "NON_PROGRESS"


class Failure(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    failure_class: FailureClass
    message: constr(max_length=4000)


class TaskRun(BaseModel):
    """
    一次可暂停、恢复、重试、分支的 Agent 运行。status 是稳定生命周期枚举；current_node 是随 workflow_version 演进的领域阶段，消费方必须容忍未知节点名。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: RunId
    project_id: constr(pattern=r"^proj_[0-9a-f]{32}$")
    goal: constr(min_length=1, max_length=4000)
    workflow_version: constr(min_length=1, max_length=100) = Field(
        ...,
        description="工作流定义版本。首个模拟实现为 sim-0.1，节点集合：CREATED, PROBLEM_ANALYSIS, DATA_PREPARATION, MODEL_PLANNING, EXPERIMENTING, VALIDATING, PAPER_WRITING, COMPLETED。",
    )
    status: Status
    current_node: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    budget: Budget | None = None
    params: dict[str, Any] | None = Field(
        None,
        description="运行输入参数（题目引用、模拟钩子等），按 workflow_version 解释。",
    )
    parent_run_id: RunId | None = Field(
        None, description="分支来源运行（规划文档 §7.1 的 parent_branch）。"
    )
    failure: Failure | None = None
    created_at: Timestamp
    updated_at: Timestamp
    started_at: NullableTimestamp | None = None
    ended_at: NullableTimestamp | None = None
