from __future__ import annotations

import hashlib
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import ArtifactStatus

from ..api_models import ArtifactText, AttachmentParseResult
from ..blobstore import local_content_digest
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..doc_text import extract_text
from ..errors import ApiError, NotFoundError
from ..orm import ArtifactRow, ArtifactTextRow, ProjectRow
from ..serialize import as_utc, utcnow

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

#: 没抽出正文的缓存（empty/unsupported/failed）只短期复用：解析后端修复缺陷或
#: 补装 OCR 依赖后，旧的空结果应自动重跑而不是永久挡路（2026-08 曾有版面检测
#: 缺陷期写入的 empty 缓存在修复后仍长期生效）。成功正文因内容寻址不可变，永久复用。
NEGATIVE_TEXT_CACHE_TTL = timedelta(hours=1)


def _text_cache_reusable(cached: ArtifactTextRow) -> bool:
    if cached.status in ("ready", "partial"):
        return True
    created_at = as_utc(cached.created_at)
    return created_at is not None and utcnow() - created_at < NEGATIVE_TEXT_CACHE_TTL


@router.post("/parse", response_model=AttachmentParseResult)
async def parse_attachment_adhoc(
    request: Request,
    file: UploadFile = File(...),
    _ctx: AuthContext = Depends(get_auth_context),
) -> AttachmentParseResult:
    """对话附件的即席解析（ADR-0010 批次三）：不落库、不建产物。

    对话历史保存在页面内存、服务端无状态；随消息提供的附件也保持同样姿态——
    解析一次、返回文本、什么都不留。抽取链路与产物正文完全相同（含可选 VL），
    图片和扫描件因此也能转成模型可读的 Markdown。
    """

    content = await file.read()
    name = file.filename or "attachment"
    media_type = file.content_type or "application/octet-stream"
    settings = request.app.state.settings
    if len(content) > settings.attachment_text_max_bytes:
        return AttachmentParseResult(
            name=name,
            media_type=media_type,
            status="unsupported",
            engine="none",
            characters=0,
            text="",
            detail=f"文件超过正文抽取上限 {settings.attachment_text_max_bytes} 字节",
        )
    # 抽取必须离开事件循环：VL/OCR 是同步重活（首载可达数十秒），在 async 端点里
    # 直跑会冻结整个 API（健康检查、对话、SSE 全部停摆）。
    extraction = await run_in_threadpool(
        extract_text, content, name, media_type, ocr_languages=settings.ocr_languages
    )
    return AttachmentParseResult(
        name=name,
        media_type=media_type,
        status=extraction.status,
        engine=extraction.engine,
        characters=extraction.characters,
        segments=extraction.segments,
        images=extraction.images,
        detail=extraction.detail,
        text=extraction.text,
    )


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
    要好几秒。产物内容寻址、字节不可变，因此抽一次可以一直复用；没抽出正文的
    负结果只在 TTL 内复用（见 NEGATIVE_TEXT_CACHE_TTL），``refresh=true``
    用于服务端补装了 OCR 或解析依赖之后立即重跑。
    """

    row = _get_owned_artifact(session, ctx, artifact_id)
    cached = session.get(ArtifactTextRow, artifact_id)
    if cached is not None and not refresh and _text_cache_reusable(cached):
        return ArtifactText(
            artifact_id=artifact_id,
            name=row.name,
            media_type=row.media_type or "application/octet-stream",
            status=cached.status,
            engine=cached.engine,
            characters=cached.characters,
            segments=cached.segments,
            images=cached.images,
            detail=cached.detail,
            text=cached.text,
        )

    settings = request.app.state.settings
    size = row.size_bytes or 0
    if size > settings.attachment_text_max_bytes:
        extraction_status, engine, text = "unsupported", "none", ""
        detail = f"文件超过正文抽取上限 {settings.attachment_text_max_bytes} 字节，仅保留原文件"
        segments = None
        images = None
    else:
        extraction = extract_text(
            _load_content(request, row),
            row.name,
            row.media_type or "",
            ocr_languages=settings.ocr_languages,
        )
        extraction_status, engine, text = extraction.status, extraction.engine, extraction.text
        detail, segments, images = extraction.detail, extraction.segments, extraction.images

    if cached is None:
        cached = ArtifactTextRow(artifact_id=artifact_id, created_at=utcnow())
        session.add(cached)
    cached.status = extraction_status
    cached.engine = engine
    cached.characters = len(text)
    cached.segments = segments
    cached.images = images
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
        images=images,
        detail=cached.detail,
        text=text,
    )
