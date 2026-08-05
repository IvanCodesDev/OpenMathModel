# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class Kind(Enum):
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


class Sha256(RootModel[constr(pattern=r"^[a-f0-9]{64}$")]):
    root: constr(pattern=r"^[a-f0-9]{64}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class Artifact(BaseModel):
    """
    图表、数据、代码、日志、论文等交付物的元数据。内容本体在对象存储中内容寻址，服务端负责重新计算并核验 sha256。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^art_[0-9a-f]{32}$")
    project_id: constr(pattern=r"^proj_[0-9a-f]{32}$")
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$") | None = None
    kind: Kind
    uri: constr(min_length=1, max_length=2000)
    sha256: Sha256
    size_bytes: conint(ge=0)
    media_type: constr(min_length=1, max_length=255)
    producer_step_id: constr(pattern=r"^step_[0-9a-f]{32}$") | None = None
    inputs: list[constr(pattern=r"^art_[0-9a-f]{32}$")] = Field(
        ..., description="上游 Artifact 血缘；失效传播沿此边计算。"
    )
    status: Status
    created_at: Timestamp
