# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class Status(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class FailureClass(Enum):
    TRANSIENT = "TRANSIENT"
    TOOL_ENV = "TOOL_ENV"
    CODE_DEFECT = "CODE_DEFECT"
    METHOD_INVALID = "METHOD_INVALID"
    DATA_DEFECT = "DATA_DEFECT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    POLICY_BLOCK = "POLICY_BLOCK"
    NON_PROGRESS = "NON_PROGRESS"


class Sha256(RootModel[constr(pattern=r"^[a-f0-9]{64}$")]):
    root: constr(pattern=r"^[a-f0-9]{64}$")


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


class StepRun(BaseModel):
    """
    状态机中单个节点的一次执行记录。同一节点重试产生新的 attempt。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^step_[0-9a-f]{32}$")
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$")
    node: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    attempt: conint(ge=1)
    input_hash: Sha256 | None = Field(
        None, description="本次执行输入的内容哈希，幂等与重放依据。"
    )
    status: Status
    failure_class: FailureClass | None = None
    failure_message: constr(max_length=4000) | None = None
    created_at: Timestamp
    started_at: NullableTimestamp | None = None
    ended_at: NullableTimestamp | None = None
