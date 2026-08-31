from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from omm_contracts import (
    AgentEventType,
    CreateTaskRunInput,
    StepRunStatus,
    TaskRun,
    TaskRunActionInput,
    TaskRunStatus,
    TERMINAL_TASK_RUN_STATUSES,
)

from ..actions import execute_action
from ..api_models import (
    ApprovalList,
    RunNote,
    RunNoteInput,
    RunRevision,
    RunRevisionInput,
    StepRunList,
    TaskRunList,
)
from ..config import DEFAULT_MAX_CONCURRENT_RUNS
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..engine_glue import (
    MAX_REVISION_ROUNDS,
    create_run_events,
    request_revision,
    revision_rounds,
    suggest_revision_stage,
)
from ..errors import ApiError, NotFoundError
from ..events import append_event, lock_run
from ..idempotency import with_idempotency
from ..ids import new_id
from ..orm import ApprovalRequestRow, ProjectRow, RunNoteRow, StepRunRow, TaskRunRow
from ..serialize import (
    approval_to_contract,
    iso_z,
    step_run_to_contract,
    task_run_to_contract,
    utcnow,
)
from ..workflow import NODE_CREATED, STAGE_LABELS

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

        # 高级设置「最大并发任务」：只数排队中和执行中的——等待审批与已暂停的
        # 任务停在人身上、不占执行资源，不该挡住用户开新任务。
        limit = ctx.user.max_concurrent_runs or DEFAULT_MAX_CONCURRENT_RUNS
        active = session.execute(
            select(func.count())
            .select_from(TaskRunRow)
            .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
            .where(
                ProjectRow.owner == ctx.user.id,
                TaskRunRow.status.in_(
                    [TaskRunStatus.QUEUED.value, TaskRunStatus.RUNNING.value]
                ),
            )
        ).scalar_one()
        if active >= limit:
            raise ApiError(
                409,
                "CONCURRENCY_LIMIT",
                f"已有 {active} 个任务在排队或执行中，达到并发上限（{limit} 个）；"
                "请等待现有任务完成，或在设置中心「高级设置」调高最大并发任务。",
            )

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


_TERMINAL_STATUSES = frozenset(status.value for status in TERMINAL_TASK_RUN_STATUSES)


@router.post("/{run_id}/notes", response_model=RunNote, status_code=201)
def post_run_note(
    run_id: str,
    payload: RunNoteInput,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> RunNote:
    """运行中追加补充要求（§11.3 方案 A）：落行 + run.log 回执，不打断当前执行。

    备注在下一次节点执行时注入提示词（EngineLlmPort 构建时读取本表）；scope
    指向已完成阶段时回执附带回退引导——重做由人显式操作，绝不由备注文本触发。
    """
    run = get_owned_run(session, ctx, run_id)
    if run.status in _TERMINAL_STATUSES:
        raise ApiError(409, "RUN_FINISHED", "运行已结束，无法追加补充要求；请在新任务中提出")
    text = payload.text.strip()
    if not text:
        raise ApiError(422, "EMPTY_TEXT", "补充要求不能为空")

    note = RunNoteRow(
        id=new_id("note"),
        run_id=run.id,
        text=text,
        scope=payload.scope,
        created_at=utcnow(),
    )
    session.add(note)

    if payload.scope == "global":
        message = "已记录补充要求，将在后续每次节点执行时提供给智能体"
    else:
        label = STAGE_LABELS.get(payload.scope, payload.scope)
        message = f"已记录补充要求，将在「{label}」阶段的节点执行时提供给智能体"
        stage_done = session.execute(
            select(StepRunRow).where(
                StepRunRow.run_id == run.id,
                StepRunRow.node == payload.scope,
                StepRunRow.status == StepRunStatus.SUCCEEDED.value,
            )
        ).scalars().first()
        if stage_done is not None:
            message += (
                "；该阶段已完成，备注不会自动触发重做——"
                "如需按新要求重做，请在时间线对相应阶段发起重试或回退"
            )
    append_event(
        session,
        run.id,
        AgentEventType.run_log.value,
        {
            "kind": "user_note",
            "note_id": note.id,
            "scope": payload.scope,
            "text": text[:500],
            "message": message,
        },
    )
    return RunNote(
        id=note.id,
        run_id=run.id,
        text=text,
        scope=payload.scope,
        created_at=iso_z(note.created_at),
    )


@router.post("/{run_id}/revisions", response_model=RunRevision, status_code=201)
def post_run_revision(
    run_id: str,
    payload: RunRevisionInput,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> RunRevision:
    """对已完成的运行提出修改要求（ADR-0013）：重开运行并挂审批门等确认起点。

    要求正文同时落成一条 global 备注——重做阶段的节点靠它读到「要改什么」，
    不落的话重跑一遍只会原样再产出一次。真正的重做要等用户在审批门里选定起点
    （本接口只受理，不推进）。
    """
    run = get_owned_run(session, ctx, run_id)
    locked = lock_run(session, run.id)
    run = locked if locked is not None else run
    if run.status != TaskRunStatus.COMPLETED.value:
        raise ApiError(
            409,
            "RUN_NOT_COMPLETED",
            f"状态 {run.status} 不支持提出修改要求；仅已完成的运行可以发起修订",
        )
    rounds = revision_rounds(session, run.id)
    if rounds >= MAX_REVISION_ROUNDS:
        raise ApiError(
            409,
            "REVISION_LIMIT_REACHED",
            f"本次运行的修改轮数已达上限（{MAX_REVISION_ROUNDS} 轮）；"
            "如仍需调整，请基于当前结果新建任务",
        )
    text = payload.text.strip()
    if not text:
        raise ApiError(422, "EMPTY_TEXT", "修改要求不能为空")

    note = RunNoteRow(
        id=new_id("note"),
        run_id=run.id,
        text=text,
        scope="global",
        created_at=utcnow(),
    )
    session.add(note)
    session.flush()  # 备注行先于领域事件落库：重做的节点按 run_id 读它

    round_no, approval_id = request_revision(session, run, text, note.id)
    suggested = suggest_revision_stage(text)
    append_event(
        session,
        run.id,
        AgentEventType.run_log.value,
        {
            "kind": "revision_requested",
            "note_id": note.id,
            "round": round_no,
            "suggested_stage": suggested,
            "text": text[:500],
            "message": (
                f"已受理第 {round_no} 轮修改要求，建议从「"
                f"{STAGE_LABELS.get(suggested, suggested)}」重做；"
                "请在待确认事项中选定重做起点后生效"
            ),
        },
    )
    return RunRevision(
        run_id=run.id,
        round=round_no,
        approval_id=approval_id,
        suggested_stage=suggested,
        note_id=note.id,
    )


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
