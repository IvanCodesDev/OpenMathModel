# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, conint


class Status(Enum):
    """
    passed=全部断言通过；failed=断言未全过或运行失败（明细见 assertions 与 attempts）。
    """

    passed = "passed"
    failed = "failed"


class AssertionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: str = Field(..., description="断言标识（父节点任务卡中的编号）。")
    passed: bool
    detail: str = Field(
        ..., description="断言的判定说明；失败时必须携带可定位的差异信息。"
    )


class EnvFingerprint(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    runtime: str = Field(..., description="语言运行时（如 python）。")
    version: str = Field(..., description="运行时版本号。")
    deps_hash: str = Field(
        ...,
        description="依赖清单的内容哈希；同指纹 + 同种子 + 同数据 = 指标应一致（浮点容差内）。",
    )


class Usage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    runs: conint(ge=0) = Field(..., description="沙箱运行次数。")
    tokens: conint(ge=0) = Field(..., description="本任务消耗的 LLM tokens。")
    duration_ms: conint(ge=0)


class SandboxRunReport(BaseModel):
    """
    沙盒 Agent 的执行报告（设计 D1.4）：一次「写码→运行→读产物→修复」任务的可审计结论。验收以 assertions 为准（父节点给定的确定性校验），不接受模型自述成功；父节点从 metrics_source_artifact 读真实数字拼 StageOutput（数字冻结纪律的源头）。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    status: Status = Field(
        ...,
        description="passed=全部断言通过；failed=断言未全过或运行失败（明细见 assertions 与 attempts）。",
    )
    attempts: conint(ge=1) = Field(
        ..., description="实际执行的运行轮数（R2 修复每轮计一次）。"
    )
    final_code_artifact: str = Field(
        ..., description="最终版本代码的 artifact id（可复现入口）。"
    )
    produced_artifacts: list[str] = Field(
        ..., description="本次任务产出的全部 artifact id 列表；无产物为空列表。"
    )
    metrics_source_artifact: str | None = Field(
        ...,
        description="唯一指标来源的 artifact id（如 metrics.json）；无指标类任务（纯清洗/渲染）为 null。",
    )
    assertions: list[AssertionResult] = Field(
        ...,
        description="验收断言逐条结果（断言由父节点给定）；空列表 = 父节点未给断言（仅以运行成功为准，须在消费方显式声明）。",
    )
    seeds: dict[str, int | str] = Field(
        ...,
        description="本次运行使用的显式随机种子（名称 → 值），可复现性硬要求（§7.3）。",
    )
    env_fingerprint: EnvFingerprint
    usage: Usage
