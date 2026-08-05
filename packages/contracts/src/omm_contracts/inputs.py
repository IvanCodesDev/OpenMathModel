"""API 请求载荷模型（契约的写方向）。

与响应模型不同，请求模型 ``extra="ignore"``：容忍客户端新增字段，
服务端只取已知字段；响应方向由 schemas/v1 的 additionalProperties:false 约束。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import ProjectMode


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BudgetInput(InputModel):
    """与 schemas/v1/task-run.schema.json 的 budget 对象一致。"""

    max_wall_time_s: Optional[int] = Field(default=None, ge=1)
    max_model_calls: Optional[int] = Field(default=None, ge=0)
    cost_limit_usd: Optional[float] = Field(default=None, ge=0)


class CreateProjectInput(InputModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    mode: Optional[ProjectMode] = None
    competition_policy: Optional[str] = Field(default=None, max_length=200)
    workspace_uri: Optional[str] = Field(default=None, max_length=1000)


class CreateTaskRunInput(InputModel):
    project_id: str
    goal: str = Field(min_length=1, max_length=4000)
    workflow_version: str = Field(default="sim-0.1", min_length=1, max_length=100)
    budget: Optional[BudgetInput] = None
    params: Optional[dict[str, Any]] = None
    auto_start: bool = True


class TaskRunAction(str, Enum):
    APPROVE = "approve"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"


class TaskRunActionInput(InputModel):
    action: TaskRunAction
    approval_id: Optional[str] = None
    option_id: Optional[str] = Field(default=None, max_length=100)
    comment: Optional[str] = Field(default=None, max_length=2000)
    client_token: Optional[str] = Field(default=None, max_length=64)
