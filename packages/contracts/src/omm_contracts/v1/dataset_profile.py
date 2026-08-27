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
    updated_at: Timestamp
