from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, or_, select
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
from ..serialize import artifact_to_contract, iso_z, project_to_contract, utcnow

router = APIRouter(prefix="/v1/projects", tags=["projects"])

# 与契约 task-run.status 的终态子集一致：active 桶 = 存在运行且不在此集合。
TERMINAL_RUN_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")


def latest_run_subquery():
    """「项目 → 最新一次运行」轻量投影（按创建时间取最近，同刻按 id 收敛）。

    行号窗口函数一次算出每个项目的最新运行，列表聚合与 q/state 过滤共用，
    避免按项目逐个查询（切片②的服务端聚合承诺）。
    """
    rank = (
        func.row_number()
        .over(
            partition_by=TaskRunRow.project_id,
            order_by=(TaskRunRow.created_at.desc(), TaskRunRow.id.desc()),
        )
        .label("rank")
    )
    ranked = select(
        TaskRunRow.project_id.label("project_id"),
        TaskRunRow.id.label("run_id"),
        TaskRunRow.status.label("run_status"),
        TaskRunRow.current_node.label("run_node"),
        TaskRunRow.goal.label("run_goal"),
        TaskRunRow.updated_at.label("run_updated_at"),
        rank,
    ).subquery()
    return select(ranked).where(ranked.c.rank == 1).subquery()


def contains_pattern(term: str) -> str:
    """LIKE 模糊包含模式；转义用户输入里的通配符。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
    include: Optional[Literal["stats"]] = Query(
        default=None,
        description="stats = 每项附带最新运行投影与产物计数（服务端一次聚合，客户端不再 N+1）",
    ),
    q: Optional[str] = Query(
        default=None,
        max_length=200,
        description="按项目名或最新运行目标模糊搜索（大小写不敏感）",
    ),
    state: Optional[Literal["active", "done"]] = Query(
        default=None,
        description="按最新运行归桶：active = 有运行且未到终态；done = 最新运行已完成",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ProjectList:
    # 归档状态不进 Project 载荷：默认列表 = 未归档，archived=true = 只看归档。
    conditions = [
        ProjectRow.owner == ctx.user.id,
        ProjectRow.archived_at.is_not(None) if archived else ProjectRow.archived_at.is_(None),
    ]
    with_stats = include == "stats"
    search = (q or "").strip()
    # stats、搜索与状态桶都依赖「项目 → 最新运行」联结；纯列表保持原有单表查询。
    latest_run = (
        latest_run_subquery() if with_stats or search or state is not None else None
    )

    query = select(ProjectRow)
    if latest_run is not None:
        query = query.outerjoin(latest_run, latest_run.c.project_id == ProjectRow.id)
    if with_stats:
        artifact_counts = (
            select(ArtifactRow.project_id.label("project_id"), func.count().label("n"))
            .group_by(ArtifactRow.project_id)
            .subquery()
        )
        query = query.outerjoin(
            artifact_counts, artifact_counts.c.project_id == ProjectRow.id
        ).add_columns(
            latest_run.c.run_id,
            latest_run.c.run_status,
            latest_run.c.run_node,
            latest_run.c.run_goal,
            latest_run.c.run_updated_at,
            func.coalesce(artifact_counts.c.n, 0).label("artifact_count"),
        )
    query = query.where(*conditions)
    if search:
        pattern = contains_pattern(search)
        query = query.where(
            or_(
                ProjectRow.name.ilike(pattern, escape="\\"),
                latest_run.c.run_goal.ilike(pattern, escape="\\"),
            )
        )
    if state == "active":
        query = query.where(
            latest_run.c.run_id.is_not(None),
            latest_run.c.run_status.not_in(TERMINAL_RUN_STATUSES),
        )
    elif state == "done":
        query = query.where(latest_run.c.run_status == "COMPLETED")

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()
    rows = session.execute(
        query.order_by(ProjectRow.created_at.desc(), ProjectRow.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    items: list[Project] = []
    for row in rows:
        stats: Optional[dict[str, Any]] = None
        if with_stats:
            latest: Optional[dict[str, Any]] = None
            if row.run_id is not None:
                latest = {
                    "id": row.run_id,
                    "status": row.run_status,
                    # 契约要求非空节点名；历史行可能为空，按初始节点兜底。
                    "current_node": row.run_node or "CREATED",
                    "goal": row.run_goal,
                    "updated_at": iso_z(row.run_updated_at),
                }
            stats = {"latest_run": latest, "artifact_count": row.artifact_count}
        items.append(project_to_contract(row[0], stats=stats))
    return ProjectList(items=items, total=total)


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
