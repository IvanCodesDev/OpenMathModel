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


class PlanOption(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: str = Field(..., description="方案标识（如 A / B）。")
    name: str = Field(..., description="方法名。")
    approach: str = Field(..., description="核心思路与数学工具。")
    steps: list[str] = Field(
        ..., description="可执行的实验步骤（能直接转成 Python 实验）。"
    )
    risks: list[str] = Field(..., description="该方案的主要风险与失效条件。")


class PlanProposal(BaseModel):
    """
    建模方案页正文投影：MODEL_PLANNING 阶段真实 LLM 节点的最新成功输出（审批门的确认对象）。节点侧已保证 recommended_plan_id 指向 plans 中的方案；llm_attempts 等过程杂项不进入本投影。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: RunId
    plans: list[PlanOption] = Field(
        ..., description="候选方案列表（当前提示词约定为 A/B 两套）。", min_length=1
    )
    recommended_plan_id: str = Field(
        ..., description="推荐方案的 id；审批「采用当前方案」即采纳该方案。"
    )
    rationale: str | None = Field(
        ...,
        description="推荐理由（与数据规模、约束和评审标准的匹配度）；节点未给出时为 null。",
    )
    updated_at: Timestamp
