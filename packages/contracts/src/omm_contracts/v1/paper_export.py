# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, RootModel, constr


class Format(Enum):
    """
    pdf = 排队编译；tex = 只落源产物并立即 READY。
    """

    pdf = "pdf"
    tex = "tex"


class Status(Enum):
    """
    UNSUPPORTED = 服务端未安装编译器，诚实降级不伪装成功。
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class Timestamp(
    RootModel[constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
):
    root: constr(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$") = Field(
        ..., description="UTC ISO-8601，统一以 Z 结尾。"
    )


class PaperExport(BaseModel):
    """
    论文导出任务（ADR-0012 阶段 A）：客户端提交完整 .tex 源，服务端排队编译 PDF。tex 源与 PDF 都是 kind=paper 的 Artifact，PDF 的 inputs 指向 tex 源；下载沿用 /v1/artifacts/{id}/download。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: constr(pattern=r"^pex_[0-9a-f]{32}$")
    project_id: constr(pattern=r"^proj_[0-9a-f]{32}$")
    run_id: constr(pattern=r"^run_[0-9a-f]{32}$") | None = Field(
        None,
        description="关联的工作台运行；带 run_id 的导出完成时沿 run 事件流追加 paper.export.finished。",
    )
    format: Format = Field(
        ..., description="pdf = 排队编译；tex = 只落源产物并立即 READY。"
    )
    status: Status = Field(
        ..., description="UNSUPPORTED = 服务端未安装编译器，诚实降级不伪装成功。"
    )
    artifact_id: constr(pattern=r"^art_[0-9a-f]{32}$") | None = Field(
        None,
        description="交付产物：format=pdf 时为编译出的 PDF，format=tex 时为 tex 源产物。",
    )
    source_artifact_id: constr(pattern=r"^art_[0-9a-f]{32}$") | None = Field(
        None, description="受理时落库的 .tex 源产物；编译失败仍可下载排查。"
    )
    detail: constr(max_length=500) | None = Field(
        None, description="FAILED 时为编译日志尾部；UNSUPPORTED 时为启用途径说明。"
    )
    created_at: Timestamp
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
