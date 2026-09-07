# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


class SourceStage(Enum):
    """
    产出该数值的阶段。
    """

    DATA_PREPARATION = "DATA_PREPARATION"
    MODEL_PLANNING = "MODEL_PLANNING"
    EXPERIMENTING = "EXPERIMENTING"
    VALIDATING = "VALIDATING"


class FrozenNumber(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(min_length=1) = Field(
        ...,
        description="清单内稳定编号（如 metrics.rmse、robustness.bootstrap.value），论文与卡片按它引用。",
    )
    label: str = Field(
        ...,
        description="人可读含义（如「实验指标 rmse」「稳健性检查「bootstrap 稳定性」阈值」）。",
    )
    value: float = Field(
        ...,
        description="冻结的数值，来自上游阶段的结构化产出（沙盒标记行 / 清洗统计 / 方案文本），原样不改写。",
    )
    source_stage: SourceStage = Field(..., description="产出该数值的阶段。")
    source_path: str = Field(
        ...,
        description="阶段产出内的路径（如 metrics.rmse、robustness.checks[0].threshold、cleaning.rows_before、plans[A].steps[2]）。",
    )


class Kind(Enum):
    """
    发现类型：unsourced_number 无出处数值（不在冻结清单与材料中）；phantom_figure 图引用 / 插图没有对应的真实图件；phantom_table 表引用在全文找不到带该编号表题的表格；unverified_citation 引用标记或参考文献条目不在已验证的引用库中。消费者须容忍未知取值。
    """

    unsourced_number = "unsourced_number"
    phantom_figure = "phantom_figure"
    phantom_table = "phantom_table"
    unverified_citation = "unverified_citation"


class AuditFinding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    scope: str = Field(..., description="发现所在位置：「第 N 章《…》」或「摘要」。")
    kind: Kind = Field(
        ...,
        description="发现类型：unsourced_number 无出处数值（不在冻结清单与材料中）；phantom_figure 图引用 / 插图没有对应的真实图件；phantom_table 表引用在全文找不到带该编号表题的表格；unverified_citation 引用标记或参考文献条目不在已验证的引用库中。消费者须容忍未知取值。",
    )
    numbers: list[str] = Field(
        ...,
        description="违规 token 原样取样（最多 8 个）：数值 / 「图 N」「表 N」与插图文件名 / 引用标记（[3]、\\cite{key}）。字段名沿用首版（改名即破坏性变更）。",
    )
    detail: str = Field(..., description="人可读说明。")


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class PaperSection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    heading: str = Field(..., description="章节标题。")
    content: str = Field(..., description="正文 Markdown（可含列表与表格）。")


class DocumentDraft(BaseModel):
    """
    论文编辑页正文投影：PAPER_WRITING 阶段真实 LLM 节点的最新成功输出（结构化论文草稿）。version/updated_at 支撑后续论文编辑的版本演进；markdown 产物本体沿 Artifact 下载链路获取。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    title: str = Field(..., description="论文标题。")
    abstract: str = Field(..., description="摘要（问题、方法、核心结果、结论）。")
    keywords: list[str] = Field(..., description="关键词；节点未给出时为空列表。")
    sections: list[PaperSection] = Field(..., description="章节列表，按论文顺序排列。")
    version: conint(ge=1) = Field(
        ...,
        description="草稿版本号：PAPER_WRITING 阶段每次成功产出递增（重试/重跑产生新版本）。",
    )
    updated_at: Timestamp
    frozen_numbers: list[FrozenNumber] | None = Field(
        None,
        description="数字冻结清单（H5）：正文数值的合法来源——上游各阶段结构化产出里确定性抽取的「值 + 出处」，不经模型转述。论文节点未产出该字段（2026-09-03 之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。",
    )
    audit_findings: list[AuditFinding] | None = Field(
        None,
        description="终稿审计链的发现（G4 定稿闸门的证据）：数值审计（正文数值 ∈ 冻结清单 ∪ 材料）、图表审计（引用的图须是真实图件、引用的表须有带编号表题的表格）、引用审计（引用标记与参考文献条目须来自已验证的引用库）三条确定性审计顺序过终稿；空数组 = 审计过且 0 违规；未审计（旧运行、模拟节点）为 null。可选字段：旧消费者可忽略。",
    )
