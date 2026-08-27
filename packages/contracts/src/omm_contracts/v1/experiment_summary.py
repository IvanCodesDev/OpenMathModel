# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, constr


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


class ValidationReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    verdict: Verdict
    checks: list[ValidationCheck] = Field(..., description="逐项检查列表。")
    risks: list[str] = Field(
        ..., description="主要风险与失效条件；节点未给出时为空列表。"
    )
    validation_summary: str = Field(
        ..., description="一段话的检验结论（如实包含保留意见），供论文阶段引用。"
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
        ..., description="VALIDATING 阶段的检验报告；该阶段未成功完成时为 null。"
    )
    updated_at: Timestamp
