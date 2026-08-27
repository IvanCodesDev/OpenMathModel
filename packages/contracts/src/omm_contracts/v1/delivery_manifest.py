# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class Verdict(Enum):
    """
    检验总体结论：pass 可信 / concerns 可用但有保留 / fail 不可信需重做。
    """

    pass_ = "pass"
    concerns = "concerns"
    fail = "fail"


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


class ArtifactProjection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^art_[0-9a-f]{32}$")
    kind: Kind
    name: constr(min_length=1, max_length=300)
    media_type: constr(min_length=1, max_length=255)
    size_bytes: conint(ge=0) | None
    status: Status
    producer_node: constr(max_length=100) | None
    download_url: (
        constr(pattern=r"^/api/v1/artifacts/art_[0-9a-f]{32}/download$") | None
    )


class PaperCitation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    title: str = Field(..., description="论文标题。")
    abstract: str | None = Field(..., description="论文摘要。")
    keywords: list[str] = Field(..., description="关键词；未给出时为空列表。")
    artifact_id: constr(pattern=r"^art_[0-9a-f]{32}$") | None = Field(
        ...,
        description="论文草稿产物（kind=paper）；沿 /v1/artifacts/{id}/download 获取本体。",
    )


class DeliveryManifest(BaseModel):
    """
    最终成果页正文投影：本次运行的成果交付清单。数据源是 artifacts 表（run 产出的产物列表）与各阶段最新成功输出（题目标题、实验关键指标、检验结论、论文引用）。运行尚无任何可交付内容时整体为 null，由 stage-outputs 端点表达。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    problem_title: str | None = Field(
        ...,
        description="PROBLEM_ANALYSIS 提取的任务标题；模拟链或阶段未完成时为 null。",
    )
    artifacts: list[ArtifactProjection] = Field(
        ...,
        description="本次运行产出的交付物（按创建顺序）；投影形状与 ModelingWorkspaceView.artifacts 一致。",
    )
    key_metrics: dict[str, Any] | None = Field(
        ...,
        description="EXPERIMENTING 阶段的核心指标（自由载荷：指标名 → 数值）；实验未完成时为 null，脚本未打印指标时为空对象。",
    )
    validation_verdict: Verdict | None = Field(
        ..., description="VALIDATING 阶段的总体结论；检验未完成时为 null。"
    )
    paper_citation: PaperCitation | None = Field(
        ...,
        description="论文引用（标题、摘要、关键词与草稿产物指引）；论文阶段未完成时为 null。",
    )
    updated_at: Timestamp
