"""交付包导出端点：``GET /api/v1/task-runs/{run_id}/delivery-package`` → zip。

成果清单（DeliveryManifest）是读侧投影；交付包是它的写侧第一步——用户能拿走的一个文件。
只有真实论文在场（清单带 ``delivery`` 记录）才可导出；鉴权与 stage-outputs 一致（按运行归属，
非本人或不存在一律 404）。产物内容按登记哈希核验，对不上的不进包、记在包内 ``package.json``。
"""

from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import ArtifactStatus

from ..blobstore import local_content_digest
from ..db import get_session
from ..delivery_package import build_delivery_package, summarize_package
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError
from ..orm import ArtifactRow
from ..stage_outputs import build_stage_outputs
from .task_runs import get_owned_run

router = APIRouter(prefix="/v1/task-runs", tags=["delivery"])


class DeliveryNotReadyError(ApiError):
    code = "DELIVERY_NOT_READY"
    http_status = 409


def _content_reader(request: Request, rows: dict[str, ArtifactRow]):
    """按产物 id 读内容对象；读不到给 None（打包器据此记「不可读」而不是炸掉整个包）。"""

    def read(artifact_id: str) -> Optional[bytes]:
        row = rows.get(artifact_id)
        if row is None or row.status != ArtifactStatus.READY.value:
            return None
        sha256 = local_content_digest(row.uri, row.sha256)
        if sha256 is None:
            return None
        try:
            handle = request.app.state.blobs.open(sha256)
            if handle is None:
                return None
            with handle:
                content = handle.read()
        except OSError:
            return None
        return content

    return read


@router.get(
    "/{run_id}/delivery-package",
    response_class=Response,
    responses={
        200: {"content": {"application/zip": {}}, "description": "交付包 zip：manifest.json + SHA256SUMS + README.txt + package.json + files/"},
        409: {"description": "运行尚无可交付的论文成果（成果清单没有交付记录）"},
    },
)
def download_delivery_package(
    run_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Response:
    run = get_owned_run(session, ctx, run_id)
    outputs = build_stage_outputs(session, run, request.app.state.blobs)
    manifest = outputs.delivery_manifest
    if manifest is None or manifest.delivery is None:
        raise DeliveryNotReadyError(
            "运行尚无可交付的论文成果，无法导出交付包",
            details={"run_id": run_id, "has_manifest": manifest is not None},
        )
    rows = {
        row.id: row
        for row in session.execute(
            select(ArtifactRow).where(ArtifactRow.run_id == run.id)
        ).scalars()
    }
    # 包内文件名取内容 URI 尾部的真实文件名（登记 name 可能是「建模论文草稿」这样的展示名）
    file_names = {
        row.id: (str(row.uri or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or row.name)
        for row in rows.values()
    }
    package = build_delivery_package(manifest, _content_reader(request, rows), file_names=file_names)
    summary = summarize_package(package)
    filename = quote(package.filename)
    return Response(
        content=package.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Sha256": hashlib.sha256(package.content).hexdigest(),
            "X-Package-Files": str(summary["files"]),
            "X-Package-Integrity-Failures": str(summary["integrity_failures"]),
        },
    )
