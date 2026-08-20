from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omm_contracts import (
    AgentEventType,
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    CreateProjectInput,
    Project,
    ProjectMode,
)

from ..api_models import ArtifactList, ProjectList, ProjectUpdateInput
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError, NotFoundError
from ..events import append_event
from ..ids import new_id
from ..orm import ArtifactRow, ProjectRow, TaskRunRow
from ..privacy import purge_project
from ..serialize import artifact_to_contract, project_to_contract, utcnow

router = APIRouter(prefix="/v1/projects", tags=["projects"])


def get_owned_project(
    session: Session, ctx: AuthContext, project_id: str
) -> ProjectRow:
    """按归属取项目；不存在或非本人一律 404（不泄露资源存在性）。"""
    row = session.get(ProjectRow, project_id)
    if row is None or row.owner != ctx.user.id:
        raise NotFoundError(f"项目不存在：{project_id}")
    return row


@router.post("", response_model=Project, status_code=201)
def create_project(
    payload: CreateProjectInput,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Project:
    now = utcnow()
    row = ProjectRow(
        id=new_id("proj"),
        name=payload.name,
        owner=ctx.user.id,
        description=payload.description,
        mode=payload.mode.value if payload.mode else ProjectMode.collaboration.value,
        competition_policy=payload.competition_policy,
        workspace_uri=payload.workspace_uri,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return project_to_contract(row)


@router.get("", response_model=ProjectList)
def list_projects(
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
    archived: bool = Query(default=False, description="true 时只返回已归档项目"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ProjectList:
    # 归档状态不进 Project 载荷：默认列表 = 未归档，archived=true = 只看归档。
    conditions = [
        ProjectRow.owner == ctx.user.id,
        ProjectRow.archived_at.is_not(None) if archived else ProjectRow.archived_at.is_(None),
    ]
    total = session.execute(
        select(func.count()).select_from(ProjectRow).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(ProjectRow)
        .where(*conditions)
        .order_by(ProjectRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars()
    return ProjectList(items=[project_to_contract(r) for r in rows], total=total)


@router.get("/{project_id}", response_model=Project)
def get_project(
    project_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Project:
    return project_to_contract(get_owned_project(session, ctx, project_id))


@router.patch("/{project_id}", response_model=Project)
def update_project(
    project_id: str,
    payload: ProjectUpdateInput,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Project:
    """项目维护（侧栏「最近任务」）：重命名与归档/取消归档，可同时提交。"""
    row = get_owned_project(session, ctx, project_id)
    changed = False
    if payload.name is not None and payload.name != row.name:
        row.name = payload.name
        changed = True
    if payload.archived is not None:
        target = utcnow() if payload.archived else None
        if bool(row.archived_at) != payload.archived:
            row.archived_at = target
            changed = True
    if changed:
        row.updated_at = utcnow()
        session.flush()
    return project_to_contract(row)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> None:
    """删除项目及其全部运行、事件、审批与产物；内容对象无引用后回收。

    用量记录是审计历史，保留不动。删除不可恢复——仅隐藏请用归档。
    """
    row = get_owned_project(session, ctx, project_id)
    purge_project(session, row, request.app.state.blobs)


@router.post("/{project_id}/artifacts", response_model=Artifact, status_code=201)
async def upload_project_artifact(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form("other"),
    run_id: Optional[str] = Form(None),
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> Artifact:
    """上传产物：服务端计算 sha256 并内容寻址存储（主规划 §19 对象存储/哈希校验）。"""
    get_owned_project(session, ctx, project_id)

    allowed_kinds = {k.value for k in ArtifactKind}
    if kind not in allowed_kinds:
        raise ApiError(422, "VALIDATION_ERROR", f"kind 不合法，允许：{sorted(allowed_kinds)}")
    if run_id is not None:
        run = session.get(TaskRunRow, run_id)
        if run is None or run.project_id != project_id:
            raise NotFoundError(f"任务运行不存在：{run_id}")

    settings = request.app.state.settings
    content = await file.read()
    if len(content) == 0:
        raise ApiError(422, "VALIDATION_ERROR", "空文件不允许上传")
    if len(content) > settings.artifact_max_bytes:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"文件超过上限 {settings.artifact_max_bytes} 字节")

    # 浏览器可能携带完整本地路径；仅保留显示文件名，并与 ORM/工作台契约上限一致。
    name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if len(name) > 300:
        raise ApiError(422, "VALIDATION_ERROR", "文件名不能超过 300 个字符")
    sha256, size = request.app.state.blobs.put(content)
    name = name or f"artifact-{sha256[:8]}"
    row = ArtifactRow(
        id=new_id("art"),
        project_id=project_id,
        run_id=run_id,
        kind=kind,
        name=name,
        uri=f"local://{sha256}/{name}",
        sha256=sha256,
        size_bytes=size,
        media_type=file.content_type or "application/octet-stream",
        producer_step=None,
        status=ArtifactStatus.READY.value,
        created_at=utcnow(),
    )
    session.add(row)
    if run_id is not None:
        append_event(
            session,
            run_id,
            AgentEventType.artifact_published.value,
            {"artifact_id": row.id, "kind": kind, "name": name, "uri": row.uri},
        )
    session.flush()
    return artifact_to_contract(row)


@router.get("/{project_id}/artifacts", response_model=ArtifactList)
def list_project_artifacts(
    project_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ArtifactList:
    get_owned_project(session, ctx, project_id)
    total = session.execute(
        select(func.count())
        .select_from(ArtifactRow)
        .where(ArtifactRow.project_id == project_id)
    ).scalar_one()
    rows = session.execute(
        select(ArtifactRow)
        .where(ArtifactRow.project_id == project_id)
        .order_by(ArtifactRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars()
    return ArtifactList(items=[artifact_to_contract(r) for r in rows], total=total)
