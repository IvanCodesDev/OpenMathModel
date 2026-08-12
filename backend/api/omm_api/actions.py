"""任务动作命令：approve / pause / resume / cancel / retry（引擎驱动版）。

规则：
- 动作按 v1 生命周期状态机校验，非法动作返回 409 INVALID_ACTION。
- 等价重复动作幂等返回当前状态（如对已暂停任务再次 pause）。
- approve 支持 client_token 幂等：同令牌重复提交返回同一结果。
- approve 选 reject 选项：退回重做 MODEL_PLANNING（attempt+1）并再次请求确认。
- 审批行的解决侧（option/comment/token 与 v1 approval.resolved 事件）在本层完成；
  引擎侧转移与其余投影见 engine_glue.py。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import (
    TERMINAL_TASK_RUN_STATUSES,
    AgentEventType,
    ApprovalStatus,
    TaskRunAction,
    TaskRunActionInput,
    TaskRunStatus,
)

from .engine_glue import REJECT_OPTION_ID, cancel_run, pause_run, resolve_approval, resume_run, retry_run
from .errors import ConflictError, InvalidActionError, NotFoundError
from .events import append_event, lock_run
from .orm import ApprovalRequestRow, TaskRunRow
from .serialize import iso_z, utcnow

_TERMINAL = frozenset(t.value for t in TERMINAL_TASK_RUN_STATUSES)


def execute_action(
    session: Session, run_id: str, payload: TaskRunActionInput, actor: str = "user"
) -> TaskRunRow:
    run = lock_run(session, run_id)
    if run is None:
        raise NotFoundError(f"任务运行不存在：{run_id}")

    action = payload.action
    if action == TaskRunAction.APPROVE:
        return _approve(session, run, payload, actor)
    if action == TaskRunAction.PAUSE:
        return _pause(session, run)
    if action == TaskRunAction.RESUME:
        return _resume(session, run)
    if action == TaskRunAction.CANCEL:
        return _cancel(session, run)
    if action == TaskRunAction.RETRY:
        return _retry(session, run)
    raise InvalidActionError(f"未知动作：{action}")


def _pending_approval(session: Session, run_id: str) -> ApprovalRequestRow | None:
    return session.execute(
        select(ApprovalRequestRow)
        .where(
            ApprovalRequestRow.run_id == run_id,
            ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
        )
        .order_by(ApprovalRequestRow.requested_at.desc())
    ).scalars().first()


def _approve(
    session: Session, run: TaskRunRow, payload: TaskRunActionInput, actor: str = "user"
) -> TaskRunRow:
    pending = _pending_approval(session, run.id)
    if pending is None:
        # client_token 幂等：该令牌已完成过审批则返回当前状态
        if payload.client_token:
            match = session.execute(
                select(ApprovalRequestRow).where(
                    ApprovalRequestRow.run_id == run.id,
                    ApprovalRequestRow.client_token == payload.client_token,
                )
            ).scalars().first()
            if match is not None:
                return run
        raise InvalidActionError("当前没有待处理的审批")

    if run.status != TaskRunStatus.WAITING_APPROVAL.value:
        raise InvalidActionError(f"状态 {run.status} 不允许 approve")
    if payload.approval_id and payload.approval_id != pending.id:
        raise ConflictError("approval_id 与待处理审批不一致", {"pending": pending.id})

    option_ids = [option["id"] for option in pending.options]
    option_id = payload.option_id
    if option_id is None:
        # 兼容只有一个正向选择的“确认/退回”审批；存在多个正向候选时，
        # 服务端不能替用户默选第一项，否则会把一次缺字段请求变成正式决策。
        non_reject_option_ids = [
            candidate for candidate in option_ids if candidate != REJECT_OPTION_ID
        ]
        if len(non_reject_option_ids) != 1:
            raise ConflictError(
                "审批包含多个候选方案，必须明确提供 option_id",
                {"required": "option_id", "options": option_ids},
            )
        option_id = non_reject_option_ids[0]
    if option_id not in option_ids:
        raise ConflictError("option_id 不在候选项中", {"options": option_ids})

    pending.status = ApprovalStatus.RESOLVED.value
    pending.client_token = payload.client_token
    pending.resolution = {
        "option_id": option_id,
        "actor": actor,
        "comment": payload.comment,
        "resolved_at": iso_z(utcnow()),
    }
    append_event(
        session,
        run.id,
        AgentEventType.approval_resolved.value,
        {
            "approval_id": pending.id,
            "option_id": option_id,
            "status": ApprovalStatus.RESOLVED.value,
        },
    )
    resolve_approval(session, run, option_id)
    return run


def _pause(session: Session, run: TaskRunRow) -> TaskRunRow:
    if run.status == TaskRunStatus.PAUSED.value:
        return run  # 幂等
    if run.status != TaskRunStatus.RUNNING.value:
        raise InvalidActionError(f"状态 {run.status} 不允许 pause")
    pause_run(session, run)
    return run


def _resume(session: Session, run: TaskRunRow) -> TaskRunRow:
    if run.status == TaskRunStatus.RUNNING.value:
        return run  # 幂等：已在运行中
    if run.status != TaskRunStatus.PAUSED.value:
        raise InvalidActionError(f"状态 {run.status} 不允许 resume")
    resume_run(session, run)
    return run


def _cancel(session: Session, run: TaskRunRow) -> TaskRunRow:
    if run.status == TaskRunStatus.CANCELLED.value:
        return run  # 幂等
    if run.status in _TERMINAL:
        raise InvalidActionError(f"状态 {run.status} 不允许 cancel")
    cancel_run(session, run)
    return run


def _retry(session: Session, run: TaskRunRow) -> TaskRunRow:
    if run.status != TaskRunStatus.FAILED.value:
        raise InvalidActionError(f"状态 {run.status} 不允许 retry")
    retry_run(session, run)
    return run


__all__ = ["execute_action", "REJECT_OPTION_ID"]
