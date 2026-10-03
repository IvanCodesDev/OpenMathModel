"""API 请求载荷模型（契约的写方向）。

与响应模型不同，请求模型 ``extra="ignore"``：容忍客户端新增字段，
服务端只取已知字段；响应方向由 schemas/v1 的 additionalProperties:false 约束。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import PaperExportFormat, ProjectMode


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


class CreatePaperExportInput(InputModel):
    """与 openapi CreatePaperExportInput 一致（ADR-0012 阶段 A；ADR-0025 增加 HTML 源）。

    源二选一：source_tex 走 Tectonic 编译（format=pdf / tex）；source_html 是自足的单文件
    HTML，由服务端无头浏览器打印成 PDF（只用于 format=pdf）。字符数上限在此约束，服务端
    另按 UTF-8 字节数复核。
    """

    project_id: str
    run_id: Optional[str] = None
    format: PaperExportFormat
    title: str = Field(min_length=1, max_length=300)
    source_tex: Optional[str] = Field(default=None, min_length=1, max_length=2 * 1024 * 1024)
    source_html: Optional[str] = Field(default=None, min_length=1, max_length=32 * 1024 * 1024)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CreatePaperExportInput":
        if (self.source_tex is None) == (self.source_html is None):
            raise ValueError("source_tex 与 source_html 必须且只能提供一个")
        if self.source_html is not None and self.format is not PaperExportFormat.pdf:
            raise ValueError("source_html 只用于 format=pdf")
        return self


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
