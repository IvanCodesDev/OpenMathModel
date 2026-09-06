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


class Result(Enum):
    """
    该项检查结论。
    """

    pass_ = "pass"
    warn = "warn"
    fail = "fail"


class ValidationCheck(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    name: str = Field(..., description="检查名（结果合理性、稳健性等）。")
    result: Result = Field(..., description="该项检查结论。")
    note: str = Field(..., description="依据，一句话。")


class RobustnessCheck(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: str = Field(..., description="检查项 id（脚本自定，稳定可引用）。")
    name: str = Field(
        ..., description="检查名（扰动敏感性、重采样稳定性、对基线优势幅度等）。"
    )
    passed: bool = Field(..., description="是否达标：脚本按写死在代码里的阈值判定。")
    value: float | None = Field(
        ..., description="实测值（来自标记行）；脚本未给数值时为 null。"
    )
    threshold: float | str | None = Field(
        ...,
        description="判定阈值：数值，或脚本给出的文字口径（如「≤ 0.05」）；未给时为 null。",
    )
    detail: str = Field(..., description="依据，一句话；无则空串。")
    assumption_id: str | None = Field(
        None,
        description="该检查针对的模型假设编号（plan-proposal.assumptions[].id，如 A1 / G2）；通用检查或方案阶段未生成假设表时为 null。可选字段：旧消费者可忽略。",
    )


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


class RobustnessReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    executed: bool = Field(
        ...,
        description="沙盒复跑是否真的执行过。false 时 checks 为空列表、reason 说明原因（工具 / 监督者 / 会话出口 / 实验脚本 / 预算任一缺席，或子代理未完成）。",
    )
    status: str | None = Field(
        ...,
        description="沙盒会话终态（sandbox-run-report 的 status，如 passed / failed）；未执行时为 null。非 passed 时 checks 为空、G3 不触发。",
    )
    summary_text: str = Field(
        ...,
        description="供论文引用的一句话稳健性结论，数字只来自标记行（不是模型转述）；未执行时为空串。",
    )
    checks: list[RobustnessCheck] = Field(
        ..., description="逐项稳健性检查；未执行或复跑未成功时为空列表。"
    )
    checks_total: conint(ge=0) = Field(..., description="检查项总数（= checks 长度）。")
    checks_failed: conint(ge=0) = Field(
        ..., description="未通过项数；≥1 即触发 G3 结果采用闸门。"
    )
    reason: str = Field(
        ...,
        description="未执行 / 未完成的原因（executed=false 时非空）；执行成功时为空串。",
    )
    review: ReviewReport | None = Field(
        None,
        description="检验脚本的独立审稿结论（同实验代码的生成者-评审者环；僵持时 G3 多一个「重做检验」选项）。复跑未执行、或验证节点未产出该字段（审稿环之前的运行）时为 null。可选字段：旧消费者可忽略。",
    )


class ValidationReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    verdict: Verdict
    checks: list[ValidationCheck] = Field(
        ..., description="逐项检查列表（评审判读，模型给出）。"
    )
    risks: list[str] = Field(
        ..., description="主要风险与失效条件；节点未给出时为空列表。"
    )
    validation_summary: str = Field(
        ..., description="一段话的检验结论（如实包含保留意见），供论文阶段引用。"
    )
    robustness: RobustnessReport | None = Field(
        None,
        description="沙盒复跑的稳健性检查（G3 结果采用闸门的判定依据），数字来自检验脚本的标记行而非模型转述；验证节点未产出该字段（沙盒化之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。",
    )


class ExperimentSummary(BaseModel):
    """
    实验与验证页正文投影：EXPERIMENTING 阶段真实节点（LLM 生成代码 + python 沙箱执行）的最新成功输出；validation 为同页 VALIDATING 阶段的检验报告，检验未完成时为 null。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    approach_summary: str = Field(
        ..., description="实现思路摘要（算法、数据构造方式、评估口径）。"
    )
    metrics: dict[str, Any] = Field(
        ...,
        description="实验脚本按 OMM_METRICS_JSON 标记行打印的核心指标（自由载荷：指标名 → 数值），消费方容忍未知指标名；脚本未打印时为空对象。",
    )
    stdout_tail: str = Field(..., description="实验脚本标准输出尾部（节点侧截断）。")
    experiment_summary: str = Field(
        ...,
        description="实验过程摘要（思路 + 核心指标 + 产物文件），供检验与论文阶段引用。",
    )
    validation: ValidationReport | None = Field(
        ...,
        description="VALIDATING 阶段的检验报告（评审判读 + 沙盒复跑的稳健性检查）；该阶段未成功完成时为 null。",
    )
    review: ReviewReport | None = Field(
        None,
        description="实验代码的独立审稿结论（生成者-评审者环：节点确定性复跑核对 + 只读审稿子代理；驳回退修、修不动即僵持交 G3 裁定）。实验节点未产出该字段（审稿环之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。",
    )
    updated_at: Timestamp
