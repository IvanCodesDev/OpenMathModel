# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, RootModel, constr


class RunId(RootModel[constr(pattern=r"^run_[0-9a-f]{32}$")]):
    root: constr(pattern=r"^run_[0-9a-f]{32}$")


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class Subquestion(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: str = Field(..., description="子问题标识（如 q1）。")
    text: str = Field(..., description="子问题的一句话描述。")
    depends_on: list[str] = Field(
        ..., description="依赖的子问题 id 列表；无依赖为空列表。"
    )


class ProblemFrame(BaseModel):
    """
    读题结果正文投影：PROBLEM_ANALYSIS 阶段真实 LLM 节点的最新成功输出（run_domain_events 的 STEP_SUCCEEDED）。subquestions 是后续子问题并行（map lane）的展开依据。模拟链或阶段未完成时该投影整体为 null，由 stage-outputs 端点表达。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    title: str = Field(
        ..., description="不超过 20 字的任务标题，概括实际要解决的核心问题。"
    )
    problem_type: str = Field(
        ..., description="问题类型（如 优化 / 预测 / 评价 / 机理建模 / 混合）。"
    )
    objectives: list[str] = Field(
        ..., description="需要回答的目标问题列表，逐条对应题目小问。"
    )
    constraints: list[str] = Field(
        ..., description="题目明确给出的约束与边界条件列表；无则为空列表。"
    )
    data_requirements: list[str] = Field(
        ...,
        description="完成建模需要的数据清单（含题目附带与需自行收集）；无则为空列表。",
    )
    key_assumptions: list[str] = Field(
        ..., description="为使问题可解而显式声明的关键假设列表；无则为空列表。"
    )
    subquestions: list[Subquestion] = Field(
        ...,
        description="子问题分解（子问题并行 lane 的展开依据）；题目不可分解时为覆盖全题的单条；旧运行未产出时为空列表。",
    )
    updated_at: Timestamp
