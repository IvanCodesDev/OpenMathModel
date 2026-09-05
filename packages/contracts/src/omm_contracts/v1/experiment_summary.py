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
    updated_at: Timestamp
