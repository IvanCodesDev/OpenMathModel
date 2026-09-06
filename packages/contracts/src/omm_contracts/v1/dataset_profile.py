# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, confloat, conint, constr


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class DatasetEntry(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    name: str = Field(..., description="数据集名。")
    source: str = Field(..., description="来源：题目附件 / 需收集 / 需构造。")
    fields: list[str] = Field(..., description="字段清单（含字段含义与单位）。")
    quality_risks: list[str] = Field(
        ..., description="该数据集的质量风险（缺失、异常、口径不一等）；无则为空列表。"
    )


class CleaningStatus(Enum):
    """
    清洗沙盒最终采用波的验收结论：passed 断言全过（cleaned/ 有产物且影响面标记行合格）/ failed 到预算仍未过。
    """

    passed = "passed"
    failed = "failed"


class ReviewVerdict(Enum):
    """
    审稿结论：accept 通过 / reject 驳回（驳回成立须至少一条 blocker，否则节点按 accept 记）。
    """

    accept = "accept"
    reject = "reject"


class Severity(Enum):
    """
    严重度：blocker 会让结果不可信（一票驳回的依据）/ major 应修 / minor 建议。
    """

    blocker = "blocker"
    major = "major"
    minor = "minor"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: str = Field(..., description="意见编号（审稿人给出，缺省按序补 R1、R2…）。")
    severity: Severity = Field(
        ...,
        description="严重度：blocker 会让结果不可信（一票驳回的依据）/ major 应修 / minor 建议。",
    )
    location: str = Field(..., description="问题位置（文件 / 函数 / 行）；无则空串。")
    issue: str = Field(..., description="问题描述，一句话。")
    fix_hint: str = Field(..., description="修法建议；无则空串。")


class ReviewReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    executed: bool = Field(
        ...,
        description="独立审稿是否真的进行过（至少一轮审稿子代理给出了有效终答）。false 时 verdict 为 null、findings 为空、reason 说明原因（未配置监督者 / 预算不足 / 子代理未完成）。",
    )
    verdict: ReviewVerdict | None = Field(
        ..., description="最后一轮审稿人的结论；未执行时为 null。"
    )
    rounds: conint(ge=0) = Field(
        ..., description="审稿轮数（含驳回修复后的复审）；未派出过为 0。"
    )
    findings: list[ReviewFinding] = Field(
        ...,
        description="最后一轮审稿意见（blocker 在前、同级保序）；未执行时为空列表。",
    )
    blockers: conint(ge=0) = Field(
        ..., description="阻断性意见条数（= findings 中 severity=blocker 的条数）。"
    )
    summary: str = Field(..., description="审稿人的一句话总结；未执行时为空串。")
    stalemate: bool = Field(
        ...,
        description="僵持：驳回后修复预算已尽 / 修复波未过验收 / 修复后复审未完成 / 达到轮次上限仍有 blocker。true 时保留最后一波通过验收的结果，交由该阶段的人工闸门（结果采用 / 数据确认）裁定。",
    )
    rerun_consistent: bool | None = Field(
        ...,
        description="节点用同一份最终脚本确定性复跑一次并逐键比对该阶段的关键数字（实验 / 检验：核心指标；清洗：影响面标记行）：true 一致 / false 不一致（可复现性存疑）/ null 未复跑（预算不足或脚本正文缺失）。",
    )
    reason: str = Field(..., description="未执行或僵持的原因；正常通过时为空串。")


class CleaningReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    executed: bool = Field(
        ...,
        description="清洗脚本是否真的执行过。false 时 status 为 null、数字为 0、列表为空、reason 说明原因（未配置工具 / 监督者 / 会话端口、无数据文件、预算不足、子代理未完成）。",
    )
    status: CleaningStatus | None = Field(
        ..., description="最终采用波的验收结论；未执行时为 null。"
    )
    reason: str = Field(..., description="未执行的原因；执行过为空串。")
    attempts: conint(ge=0) = Field(
        ..., description="沙盒波次（首波 + 按审稿意见的修复波）；未执行为 0。"
    )
    rows_before: conint(ge=0) = Field(
        ..., description="清洗前数据行数（脚本标记行 OMM_METRICS_JSON.rows_before）。"
    )
    rows_after: conint(ge=0) = Field(
        ..., description="清洗后数据行数（脚本标记行 rows_after）。"
    )
    rows_deleted_ratio: confloat(ge=0.0, le=1.0) = Field(
        ...,
        description="删行比例 = 1 − rows_after / rows_before（节点按标记行计算，不信脚本自述）；G2 阈值 0.05。",
    )
    imputed_columns: list[str] = Field(
        ..., description="被插补的列（脚本标记行 imputed_columns）；无则空列表。"
    )
    imputed_target_columns: list[str] = Field(
        ...,
        description="被插补的列中属于目标列的部分（大小写不敏感求交）；非空即触发 G2。",
    )
    summary: str = Field(
        ..., description="清洗工程师（沙盒子代理）终答里的一句话自述；未执行为空串。"
    )
    review: ReviewReport | None = Field(
        ...,
        description="清洗脚本的独立审稿结论（生成者-评审者环：节点确定性复跑核对 + 只读审稿子代理；驳回退修、修不动即僵持交 G2 裁定，僵持时推荐改用原始数据）。首波未过验收、未执行、或审稿环之前的运行时为 null。",
    )


class DatasetProfile(BaseModel):
    """
    数据准备页正文投影：DATA_PREPARATION 阶段真实 LLM 节点的最新成功输出（run_domain_events 的 STEP_SUCCEEDED）。模拟链或阶段未完成时该投影整体为 null，由 stage-outputs 端点表达。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    profile_summary: str = Field(
        ...,
        description="一段话的数据画像摘要（数据构成、规模量级、质量状况与可用性结论）。",
    )
    datasets: list[DatasetEntry] = Field(
        ..., description="数据清单；题目未附数据时 source 注明「需收集」或「需构造」。"
    )
    preparation_steps: list[str] = Field(
        ..., description="可执行的数据准备步骤，按执行顺序排列。"
    )
    missing_value_strategy: str | None = Field(
        ..., description="缺失值处理策略与理由；节点未给出时为 null。"
    )
    outlier_strategy: str | None = Field(
        ..., description="异常值识别与处理策略；节点未给出时为 null。"
    )
    derived_features: list[str] = Field(
        ..., description="建议构造的衍生变量（含构造方式）；节点未给出时为空列表。"
    )
    cleaning: CleaningReport | None = Field(
        None,
        description="清洗脚本的执行结论（沙盒子代理按准备方案清洗 data/ → cleaned/：影响面统计由脚本标记行给出、节点只做除法与求交）与独立审稿结论（生成者-评审者环）。数据节点未产出该字段（该字段出现之前的运行、模拟节点）时为 null；执行被跳过时 executed=false 并给原因。可选字段：旧消费者可忽略。",
    )
    updated_at: Timestamp
