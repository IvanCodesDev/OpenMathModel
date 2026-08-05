from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from omm_contracts import (
    CreateTaskRunInput,
    TaskRun,
    TaskRunActionInput,
    TaskRunStatus,
)

from ..actions import execute_action
from ..api_models import ApprovalList, StepRunList, TaskRunList
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..engine_glue import create_run_events
from ..errors import NotFoundError
from ..idempotency import with_idempotency
from ..ids import new_id
from ..orm import ApprovalRequestRow, ProjectRow, StepRunRow, TaskRunRow
from ..serialize import (
    approval_to_contract,
    step_run_to_contract,
    task_run_to_contract,
    utcnow,
)
from ..workflow import NODE_CREATED

router = APIRouter(prefix="/v1/task-runs", tags=["task-runs"])


def get_owned_run(session: Session, ctx: AuthContext, run_id: str) -> TaskRunRow:
    """按归属取运行（经项目 owner 校验）；不存在或非本人一律 404。"""
    row = session.execute(
        select(TaskRunRow)
        .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
        .where(TaskRunRow.id == run_id, ProjectRow.owner == ctx.user.id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"任务运行不存在：{run_id}")
    return row


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


@router.post("", response_model=TaskRun, status_code=201)
def create_task_run(
    payload: CreateTaskRunInput,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    session_factory = request.app.state.db.session_factory

    def _create(session: Session) -> dict[str, Any]:
        project = session.get(ProjectRow, payload.project_id)
        if project is None or project.owner != ctx.user.id:
            raise NotFoundError(f"项目不存在：{payload.project_id}")
        now = utcnow()
        row = TaskRunRow(
            id=new_id("run"),
            project_id=payload.project_id,
            goal=payload.goal,
            workflow_version=payload.workflow_version,
            status=TaskRunStatus.QUEUED.value,
            current_node=NODE_CREATED,
            auto_start=payload.auto_start,
            budget=payload.budget.model_dump(exclude_none=True) if payload.budget else None,
            params=payload.params,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        # 播种领域事件日志（RUN_CREATED），v1 run.created 事件由投影产生
        create_run_events(session, row, goal=payload.goal, auto_start=payload.auto_start)
        session.flush()
        return task_run_to_contract(row).model_dump(mode="json")

    status_code, body = with_idempotency(
        session_factory,
        request.headers.get("Idempotency-Key"),
        {"op": "create_task_run", "body": payload.model_dump(mode="json")},
        lambda: (201, _run_in_tx(session_factory, _create)),
    )
    return JSONResponse(status_code=status_code, content=body)


@router.get("", response_model=TaskRunList)
def list_task_runs(
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
    project_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TaskRunList:
    conditions = [ProjectRow.owner == ctx.user.id]
    if project_id:
        conditions.append(TaskRunRow.project_id == project_id)
    if status:
        conditions.append(TaskRunRow.status == status)
    total = session.execute(
        select(func.count())
        .select_from(TaskRunRow)
        .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
        .where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(TaskRunRow)
        .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
        .where(*conditions)
        .order_by(TaskRunRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars()
    return TaskRunList(items=[task_run_to_contract(r) for r in rows], total=total)


@router.get("/{run_id}", response_model=TaskRun)
def get_task_run(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> TaskRun:
    return task_run_to_contract(get_owned_run(session, ctx, run_id))


@router.get("/{run_id}/steps", response_model=StepRunList)
def list_task_run_steps(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> StepRunList:
    get_owned_run(session, ctx, run_id)
    rows = session.execute(
        select(StepRunRow)
        .where(StepRunRow.run_id == run_id)
        .order_by(StepRunRow.created_at.asc(), StepRunRow.id.asc())
    ).scalars()
    return StepRunList(items=[step_run_to_contract(r) for r in rows])


@router.get("/{run_id}/approvals", response_model=ApprovalList)
def list_task_run_approvals(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> ApprovalList:
    get_owned_run(session, ctx, run_id)
    rows = session.execute(
        select(ApprovalRequestRow)
        .where(ApprovalRequestRow.run_id == run_id)
        .order_by(ApprovalRequestRow.requested_at.asc())
    ).scalars()
    return ApprovalList(items=[approval_to_contract(r) for r in rows])


@router.post("/{run_id}/actions", response_model=TaskRun)
def post_task_run_action(
    run_id: str,
    payload: TaskRunActionInput,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    session_factory = request.app.state.db.session_factory

    def _act(session: Session) -> dict[str, Any]:
        get_owned_run(session, ctx, run_id)
        run = execute_action(session, run_id, payload, actor=ctx.user.email)
        session.flush()
        return task_run_to_contract(run).model_dump(mode="json")

    status_code, body = with_idempotency(
        session_factory,
        request.headers.get("Idempotency-Key"),
        {
            "op": "task_run_action",
            "run_id": run_id,
            "body": payload.model_dump(mode="json"),
        },
        lambda: (200, _run_in_tx(session_factory, _act)),
    )
    return JSONResponse(status_code=status_code, content=body)
