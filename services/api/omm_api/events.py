"""事件服务：agent_events 是 UI 时间线的唯一事实来源。

(run_id, sequence) 唯一且单调递增。写事件与状态变更在同一事务中提交，
先锁 task_run 行（PostgreSQL 生效；SQLite 单写者天然串行）再取 MAX+1，
避免并发下的序列跳跃或冲突。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ids import new_id
from .orm import AgentEventRow, TaskRunRow
from .serialize import utcnow


def lock_run(session: Session, run_id: str) -> Optional[TaskRunRow]:
    """取运行行并加行锁（PG: SELECT ... FOR UPDATE；SQLite 忽略锁提示）。"""
    return session.execute(
        select(TaskRunRow).where(TaskRunRow.id == run_id).with_for_update()
    ).scalar_one_or_none()


def next_sequence(session: Session, run_id: str) -> int:
    # autoflush=False：先把同一事务中挂起的事件推入数据库，
    # 否则 MAX(sequence) 看不到它们，同一 tick 的多条事件会撞号。
    session.flush()
    current = session.execute(
        select(func.max(AgentEventRow.sequence)).where(AgentEventRow.run_id == run_id)
    ).scalar()
    return int(current or 0) + 1


def append_event(
    session: Session, run_id: str, event_type: str, payload: dict[str, Any]
) -> AgentEventRow:
    row = AgentEventRow(
        id=new_id("evt"),
        run_id=run_id,
        sequence=next_sequence(session, run_id),
        type=event_type,
        payload=payload,
        created_at=utcnow(),
    )
    session.add(row)
    return row


def list_events(
    session: Session, run_id: str, after: int = 0, limit: Optional[int] = None
) -> list[AgentEventRow]:
    stmt = (
        select(AgentEventRow)
        .where(AgentEventRow.run_id == run_id, AgentEventRow.sequence > after)
        .order_by(AgentEventRow.sequence.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())
