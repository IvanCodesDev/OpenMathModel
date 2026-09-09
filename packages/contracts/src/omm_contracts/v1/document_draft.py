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


class SourceStage1(Enum):
    """
    产出该图件的阶段：数据准备清洗沙盒的探索性图（DATA_PREPARATION，最先编号）、实验 / 检验沙盒顺手画的图，或论文阶段按总编规划、只用本次运行真实数据补画的图（PAPER_WRITING）。消费者须容忍新增取值。
    """

    EXPERIMENTING = "EXPERIMENTING"
    VALIDATING = "VALIDATING"
    PAPER_WRITING = "PAPER_WRITING"
    DATA_PREPARATION = "DATA_PREPARATION"


class PaperFigure(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    number: conint(ge=1) = Field(
        ...,
        description="全文固定编号 N（「图 N」），按 实验 → 检验 顺序赋予，不因未插入而重编。",
    )
    name: constr(min_length=1) = Field(
        ..., description="图件文件名（产物 name，仅 basename）；正文插图的 url 就是它。"
    )
    artifact_id: constr(min_length=1) | None = Field(
        ...,
        description="对应 Artifact 的 id（沿 /artifacts/{id}/download 取内容）；节点没拿到产物 id 时为 null，编辑页不解析成图。",
    )
    caption: str = Field(
        ...,
        description="画图工程师在终答里给的一句话说明（只挂到真实文件上）；没有说明为空串。",
    )
    source_stage: SourceStage1 = Field(
        ...,
        description="产出该图件的阶段：数据准备清洗沙盒的探索性图（DATA_PREPARATION，最先编号）、实验 / 检验沙盒顺手画的图，或论文阶段按总编规划、只用本次运行真实数据补画的图（PAPER_WRITING）。消费者须容忍新增取值。",
    )
    inserted: bool = Field(
        ...,
        description="正文是否已插入该图（任一 `![…](url)` / `<img src>` 的 url 或其文件名命中 name），由节点确定性判定。",
    )


class Source(Enum):
    """
    条目来源：plan_citation = 选中方案在方案文本里标出处引用的知识库卡片；user_reference = 用户在首页「添加上下文」提供、按标题匹配到知识库的资料。
    """

    plan_citation = "plan_citation"
    user_reference = "user_reference"


class Verification(Enum):
    """
    记录级验证状态：source_verified = 知识库卡片自带来源 URL；title_matched = 用户资料按标题匹配到知识库；unverified = 两者皆非；老运行没有为 null。
    """

    source_verified = "source_verified"
    title_matched = "title_matched"
    unverified = "unverified"


class PaperReference(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    number: conint(ge=1) = Field(
        ...,
        description="全文固定编号 n（正文引用标记「[n]」与参考文献条目编号），按 方案引用 → 用户提供 顺序赋予，不因未引用而重编。",
    )
    title: constr(min_length=1) = Field(
        ..., description="知识库卡片的标题原文；引用审计据此核参考文献章的条目正文。"
    )
    text: str = Field(
        ...,
        description="参考文献条目正文（不含编号；由卡片元数据确定性生成，全文 / 来源链接为 Markdown 链接），写手须逐字照抄。",
    )
    url: constr(pattern=r"^https?://\S+$") | None = Field(
        ...,
        description="出处链接（论文全文或来源页，仅 http(s)）；卡片没有可用链接时为 null。",
    )
    source: Source = Field(
        ...,
        description="条目来源：plan_citation = 选中方案在方案文本里标出处引用的知识库卡片；user_reference = 用户在首页「添加上下文」提供、按标题匹配到知识库的资料。",
    )
    card_id: constr(min_length=1) | None = Field(
        ..., description="知识库卡片 id（problem:… / paper:…）；没有对应卡片时为 null。"
    )
    cited: bool = Field(
        ...,
        description="正文（含摘要）是否引用了该条目（任一 [n] / \\cite{key} 标记展开后命中编号或 key），由节点确定性判定。",
    )
    key: constr(pattern=r"^[a-z_][a-z0-9_]*$") | None = Field(
        None,
        description="稳定的引用 key（`\\cite{key}` 与 refs/references.bib 用），由卡片 id 确定性生成；老运行没有为 null。",
    )
    verification: Verification | None = Field(
        None,
        description="记录级验证状态：source_verified = 知识库卡片自带来源 URL；title_matched = 用户资料按标题匹配到知识库；unverified = 两者皆非；老运行没有为 null。",
    )


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
    figures: list[PaperFigure] | None = Field(
        None,
        description="真实图件清单（H5 figure_render 第一步）：本次运行实验 / 检验沙盒真正落盘并采集为 figure 产物的图，按 实验 → 检验 顺序编号（图 N），是正文插图 `![图 N 标题](文件名)` 的唯一合法来源；图表审计据此核对。编辑页用 name → artifact_id 把插图解析成产物下载链接。空数组 = 本次运行没有产出图件；论文节点未产出该字段（2026-09-07 之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。",
    )
    references: list[PaperReference] | None = Field(
        None,
        description="本次运行的已验证引用库（H5 refs/ 第一步）：方案阶段从知识库解析到的条目——选中方案引用的先例 + 用户提供的资料，均带记录级出处 URL——按固定顺序编号 [n]，是正文引用标记与「参考文献」章条目的唯一合法来源；引用审计据此核编号与条目正文。空数组 = 本次运行没有可核实的引用条目；论文节点未产出该字段（旧运行、模拟节点）时为 null。可选字段：旧消费者可忽略。",
    )
