from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from omm_contracts import (
    ArtifactKind,
    ArtifactStatus,
    CreatePaperExportInput,
    PaperExport,
    PaperExportFormat,
    PaperExportStatus,
)

from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError, ConflictError, NotFoundError
from ..idempotency import with_idempotency
from ..ids import new_id
from ..orm import ArtifactRow, PaperExportRow, ProjectRow, TaskRunRow
from ..paper_export import UNSUPPORTED_HINT, find_tectonic
from ..serialize import paper_export_to_contract, utcnow

router = APIRouter(prefix="/v1/paper-exports", tags=["paper-exports"])

_ACTIVE_STATUSES = (PaperExportStatus.QUEUED.value, PaperExportStatus.RUNNING.value)


def _run_in_tx(
    session_factory: sessionmaker[Session],
    work: Callable[[Session], dict[str, Any]],
) -> dict[str, Any]:
    """幂等 produce 与请求会话解耦：独立事务执行，成功提交、异常回滚。"""
    session = session_factory()
    try:
        body = work(session)
        session.commit()
        return body
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("", response_model=PaperExport, status_code=202)
def create_paper_export(
    payload: CreatePaperExportInput,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    """提交论文导出（ADR-0012 阶段 A：客户端直传 .tex）。

    受理即把 .tex 源落为 kind=paper 的 Artifact——编译失败源文件仍可下载排查。
    format=tex 只落源产物并立即 READY；format=pdf 排队编译，未安装 Tectonic
    时直接落 UNSUPPORTED（诚实降级，不占队列）。
    """
    settings = request.app.state.settings
    blobs = request.app.state.blobs
    session_factory = request.app.state.db.session_factory

    source_bytes = payload.source_tex.encode("utf-8")
    if len(source_bytes) > settings.paper_export_max_bytes:
        raise ApiError(
            413,
            "SOURCE_TOO_LARGE",
            f"tex 源超过 {settings.paper_export_max_bytes} 字节上限，请精简正文或图片注记",
        )

    def _create(session: Session) -> dict[str, Any]:
        project = session.get(ProjectRow, payload.project_id)
        if project is None or project.owner != ctx.user.id:
            raise NotFoundError(f"项目不存在：{payload.project_id}")
        if payload.run_id:
            run = session.get(TaskRunRow, payload.run_id)
            if run is None:
                raise NotFoundError(f"任务运行不存在：{payload.run_id}")
            if run.project_id != payload.project_id:
                raise ConflictError("run_id 与 project_id 不属于同一个项目")

        wants_pdf = payload.format is PaperExportFormat.pdf
        if wants_pdf:
            # 每用户同时编译 1 个：编译是重活，先完成再排下一篇
            mine_active = session.execute(
                select(func.count())
                .select_from(PaperExportRow)
                .join(ProjectRow, ProjectRow.id == PaperExportRow.project_id)
                .where(
                    ProjectRow.owner == ctx.user.id,
                    PaperExportRow.status.in_(_ACTIVE_STATUSES),
                )
            ).scalar_one()
            if mine_active >= 1:
                raise ApiError(
                    409,
                    "CONCURRENCY_LIMIT",
                    "已有一篇论文正在排队或编译中，请等它完成后再导出",
                )
            total_active = session.execute(
                select(func.count())
                .select_from(PaperExportRow)
                .where(PaperExportRow.status.in_(_ACTIVE_STATUSES))
            ).scalar_one()
            if total_active >= settings.paper_export_queue_limit:
                raise ApiError(
                    409,
                    "QUEUE_FULL",
                    "服务端编译队列已满，请稍后重试",
                )

        now = utcnow()
        sha256, size = blobs.put(source_bytes)
        # ArtifactRow.name 上限 300：为扩展名预留位，标题过长时截断
        source_artifact = ArtifactRow(
            id=new_id("art"),
            project_id=payload.project_id,
            run_id=payload.run_id,
            kind=ArtifactKind.paper.value,
            name=f"{payload.title[:290]}.tex",
            uri=f"local://{sha256}",
            sha256=sha256,
            size_bytes=size,
            media_type="application/x-tex",
            producer_step=None,
            inputs=[],
            status=ArtifactStatus.READY.value,
            created_at=now,
        )
        session.add(source_artifact)

        row = PaperExportRow(
            id=new_id("pex"),
            project_id=payload.project_id,
            run_id=payload.run_id,
            format=payload.format.value,
            status=PaperExportStatus.QUEUED.value,
            source_artifact_id=source_artifact.id,
            source_sha256=sha256,
            created_at=now,
        )
        if not wants_pdf:
            row.status = PaperExportStatus.READY.value
            row.artifact_id = source_artifact.id
            row.ended_at = now
        elif find_tectonic(settings) is None:
            row.status = PaperExportStatus.UNSUPPORTED.value
            row.detail = UNSUPPORTED_HINT[:500]
            row.ended_at = now
        session.add(row)
        session.flush()
        return paper_export_to_contract(row).model_dump(mode="json")

    status_code, body = with_idempotency(
        session_factory,
        request.headers.get("Idempotency-Key"),
        {"op": "create_paper_export", "body": payload.model_dump(mode="json")},
        lambda: (202, _run_in_tx(session_factory, _create)),
    )
    return JSONResponse(status_code=status_code, content=body)


@router.get("/{export_id}", response_model=PaperExport)
def get_paper_export(
    export_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> PaperExport:
    row = session.execute(
        select(PaperExportRow)
        .join(ProjectRow, ProjectRow.id == PaperExportRow.project_id)
        .where(PaperExportRow.id == export_id, ProjectRow.owner == ctx.user.id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"导出任务不存在：{export_id}")
    return paper_export_to_contract(row)
