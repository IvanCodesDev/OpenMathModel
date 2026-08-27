# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, RootModel, conint, constr


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
