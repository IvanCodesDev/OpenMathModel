from __future__ import annotations

import hashlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError, NotFoundError
from ..orm import ArtifactRow, ProjectRow

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


@router.get("/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Response:
    row = _get_owned_artifact(session, ctx, artifact_id)

    if not (row.uri or "").startswith("local://"):
        raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "该产物没有可下载的内容对象")
    sha256 = row.uri.removeprefix("local://").split("/", 1)[0]

    handle = request.app.state.blobs.open(sha256)
    if handle is None:
        raise ApiError(404, "ARTIFACT_CONTENT_MISSING", "产物内容对象缺失")
    with handle:
        content = handle.read()

    # 下载即核验：内容哈希必须与登记值一致（主规划 §19 哈希校验）
    actual = hashlib.sha256(content).hexdigest()
    if actual != row.sha256:
        raise ApiError(500, "ARTIFACT_CORRUPTED", "产物内容与登记哈希不一致，已拒绝返回")

    filename = quote(row.name or artifact_id)
    return Response(
        content=content,
        media_type=row.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Sha256": row.sha256,
        },
    )
