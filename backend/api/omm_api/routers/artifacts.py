from __future__ import annotations

import hashlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import ArtifactStatus

from ..api_models import ArtifactText
from ..blobstore import local_content_digest
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..doc_text import extract_text
from ..errors import ApiError, NotFoundError
from ..orm import ArtifactRow, ArtifactTextRow, ProjectRow
from ..serialize import utcnow

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])


def _get_owned_artifact(session: Session, ctx: AuthContext, artifact_id: str) -> ArtifactRow:
    row = session.execute(
        select(ArtifactRow)
        .join(ProjectRow, ProjectRow.id == ArtifactRow.project_id)
        .where(ArtifactRow.id == artifact_id, ProjectRow.owner == ctx.user.id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"产物不存在：{artifact_id}")
    return row


def _load_content(request: Request, row: ArtifactRow) -> bytes:
    """按登记哈希取回内容并核验；与下载走同一条校验路径。"""

    if row.status != ArtifactStatus.READY.value:
        raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "该产物没有可读的内容对象")
    sha256 = local_content_digest(row.uri, row.sha256)
    if sha256 is None:
        raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "该产物没有可读的内容对象")
    try:
        handle = request.app.state.blobs.open(sha256)
        if handle is None:
            raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "产物内容对象缺失")
        with handle:
            content = handle.read()
    except OSError as exc:
        raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "产物内容对象不可读") from exc
    if hashlib.sha256(content).hexdigest() != row.sha256:
        raise ApiError(500, "ARTIFACT_CORRUPTED", "产物内容与登记哈希不一致，已拒绝返回")
    return content


@router.get("/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Response:
    row = _get_owned_artifact(session, ctx, artifact_id)
    # 下载即核验：内容哈希必须与登记值一致（主规划 §19 哈希校验）
    content = _load_content(request, row)

    filename = quote(row.name or artifact_id)
    return Response(
        content=content,
        media_type=row.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Sha256": row.sha256,
        },
    )


@router.get("/{artifact_id}/text", response_model=ArtifactText)
def read_artifact_text(
    artifact_id: str,
    request: Request,
    refresh: bool = False,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> ArtifactText:
    """读取附件正文；首次访问时现场抽取并缓存。

    抽取放在读取时而不是上传时：上传路径要对用户即时响应，而几十兆的 PDF 抽一遍
    要好几秒。产物内容寻址、字节不可变，因此抽一次可以一直复用；``refresh=true``
    用于服务端补装了 OCR 或解析依赖之后重跑。
    """

    row = _get_owned_artifact(session, ctx, artifact_id)
    cached = session.get(ArtifactTextRow, artifact_id)
    if cached is not None and not refresh:
        return ArtifactText(
            artifact_id=artifact_id,
            name=row.name,
            media_type=row.media_type or "application/octet-stream",
            status=cached.status,
            engine=cached.engine,
            characters=cached.characters,
            segments=cached.segments,
            detail=cached.detail,
            text=cached.text,
        )

    settings = request.app.state.settings
    size = row.size_bytes or 0
    if size > settings.attachment_text_max_bytes:
        extraction_status, engine, text = "unsupported", "none", ""
        detail = f"文件超过正文抽取上限 {settings.attachment_text_max_bytes} 字节，仅保留原文件"
        segments = None
    else:
        extraction = extract_text(
            _load_content(request, row),
            row.name,
            row.media_type or "",
            ocr_languages=settings.ocr_languages,
        )
        extraction_status, engine, text = extraction.status, extraction.engine, extraction.text
        detail, segments = extraction.detail, extraction.segments

    if cached is None:
        cached = ArtifactTextRow(artifact_id=artifact_id, created_at=utcnow())
        session.add(cached)
    cached.status = extraction_status
    cached.engine = engine
    cached.characters = len(text)
    cached.segments = segments
    cached.detail = detail[:500] if detail else None
    cached.text = text
    cached.created_at = utcnow()
    session.flush()

    return ArtifactText(
        artifact_id=artifact_id,
        name=row.name,
        media_type=row.media_type or "application/octet-stream",
        status=extraction_status,
        engine=engine,
        characters=len(text),
        segments=segments,
        detail=cached.detail,
        text=text,
    )
