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
    sha256: constr(pattern=r"^[0-9a-f]{64}$") | None = Field(
        None,
        description="产物登记时的内容摘要（下载端点按它核验）；交付清单据此做「文件 + 哈希」。可选字段（形状与 ModelingWorkspaceView.artifacts 一致、另带哈希）：旧消费者可忽略；登记缺失时为 null。",
    )


class Status1(Enum):
    """
    交付状态（按产出当前论文那一趟的 G4 审批回放）：pending_confirmation = G4 挂起待人确认；confirmed = 用户「确认交付」；returned_for_revision = 用户「退回修改」（新一版论文到来前保持）；unattended = 论文已发布但该运行没有挂 G4（无人值守 / 评测）；not_ready = 审批过期 / 取消等未成交付的状态。
    """

    not_ready = "not_ready"
    pending_confirmation = "pending_confirmation"
    confirmed = "confirmed"
    returned_for_revision = "returned_for_revision"
    unattended = "unattended"


class DeliveryAudit(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    findings_total: conint(ge=0)
    findings_by_kind: dict[str, conint(ge=0)] = Field(
        ...,
        description="发现类型 → 条数（键取 audit_finding.kind；消费者须容忍未知取值）。",
    )
    frozen_numbers_total: conint(ge=0)
    figures_total: conint(ge=0)
    figures_inserted: conint(ge=0)
    references_total: conint(ge=0)
    references_cited: conint(ge=0)


class Id(Enum):
    """
    检查项：论文草稿产物可读且哈希对得上 / 终稿审计 0 发现 / 已插入图件都有可下载产物 / 实验指标（冻结清单 EXPERIMENTING 项）逐个出现在论文里 / 检验结论在场。消费者须容忍未知取值。
    """

    paper_artifact_ready = "paper_artifact_ready"
    audit_clean = "audit_clean"
    figures_delivered = "figures_delivered"
    metrics_in_paper = "metrics_in_paper"
    validation_reported = "validation_reported"


class DeliveryCheck(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: Id = Field(
        ...,
        description="检查项：论文草稿产物可读且哈希对得上 / 终稿审计 0 发现 / 已插入图件都有可下载产物 / 实验指标（冻结清单 EXPERIMENTING 项）逐个出现在论文里 / 检验结论在场。消费者须容忍未知取值。",
    )
    label: str = Field(..., description="人可读的检查名。")
    passed: bool
    detail: str = Field(..., description="判定依据（计数、缺失项点名）。")


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


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    status: Status1 = Field(
        ...,
        description="交付状态（按产出当前论文那一趟的 G4 审批回放）：pending_confirmation = G4 挂起待人确认；confirmed = 用户「确认交付」；returned_for_revision = 用户「退回修改」（新一版论文到来前保持）；unattended = 论文已发布但该运行没有挂 G4（无人值守 / 评测）；not_ready = 审批过期 / 取消等未成交付的状态。",
    )
    paper_version: conint(ge=1) | None = Field(
        ..., description="对应的 DocumentDraft.version。"
    )
    approval_id: str | None = Field(
        ..., description="G4 审批 id；没挂过 G4 时为 null。"
    )
    confirmed_at: Timestamp | None = Field(
        ..., description="用户确认交付的时间；未确认为 null。"
    )
    comment: str | None = Field(
        ..., description="用户在 G4 拍板时留下的备注（确认或退回）；没有为 null。"
    )
    audit: DeliveryAudit | None = Field(
        ...,
        description="终稿审计与图件 / 文献计数（来自 DocumentDraft 的确定性字段）；论文未审计（旧运行）为 null。",
    )
    checks: list[DeliveryCheck] = Field(
        ...,
        description="一致性检查，固定顺序：paper_artifact_ready → audit_clean → figures_delivered → metrics_in_paper → validation_reported。全是代码判定，不经模型。",
    )
    files_total: conint(ge=0) = Field(
        ..., description="交付清单里的产物数（与 artifacts 同口径）。"
    )
    files_ready: conint(ge=0) = Field(
        ..., description="其中可下载（READY 且内容对象可读、哈希对得上）的产物数。"
    )
    files_hashed: conint(ge=0) = Field(..., description="其中带登记哈希的产物数。")


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
    delivery: DeliveryRecord | None = Field(
        None,
        description="交付记录（H5 DeliveryManifest 真实化）：G4 定稿闸门的交付状态 + 终稿审计计数 + 确定性一致性检查（论文产物哈希可读、审计 0 发现、已插入图件有产物、实验指标出现在论文里、检验结论在场）+ 文件计数；只有真实论文草稿在场时才有，否则（模拟链、未到论文阶段）为 null。可选字段：旧消费者可忽略。",
    )
    updated_at: Timestamp
