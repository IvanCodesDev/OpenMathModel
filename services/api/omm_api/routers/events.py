from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from omm_contracts import TERMINAL_TASK_RUN_STATUSES

from ..api_models import AgentEventList
from ..db import Database, get_session
from ..deps import AuthContext, get_auth_context
from ..events import list_events
from ..orm import TaskRunRow
from ..serialize import agent_event_to_contract
from .task_runs import get_owned_run

router = APIRouter(prefix="/v1/task-runs", tags=["events"])

_TERMINAL = frozenset(t.value for t in TERMINAL_TASK_RUN_STATUSES)


@router.get("/{run_id}/events/history", response_model=AgentEventList)
def list_run_events(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
) -> AgentEventList:
    get_owned_run(session, ctx, run_id)
    rows = list_events(session, run_id, after=after, limit=limit)
    return AgentEventList(items=[agent_event_to_contract(r) for r in rows])


def _sse_stream(
    db: Database,
    run_id: str,
    after: int,
    poll_seconds: float,
    heartbeat_seconds: float,
) -> Iterator[str]:
    """SSE 生成器：先补拉历史，再轮询实时事件；终态且无增量时发 stream.end 收尾。

    每轮读取后 rollback 结束读事务，确保能看到其他连接（推进线程）的新提交。
    """
    session = db.session_factory()
    last_sequence = after
    last_emit = time.monotonic()
    try:
        while True:
            events = list_events(session, run_id, after=last_sequence, limit=200)
            run = session.get(TaskRunRow, run_id)
            session.rollback()

            for row in events:
                last_sequence = row.sequence
                contract = agent_event_to_contract(row)
                yield (
                    f"id: {row.sequence}\n"
                    f"event: {row.type}\n"
                    f"data: {contract.model_dump_json()}\n\n"
                )
                last_emit = time.monotonic()

            if run is None:
                break
            if run.status in _TERMINAL and not events:
                yield "event: stream.end\ndata: {\"reason\": \"run_terminal\"}\n\n"
                break

            if time.monotonic() - last_emit >= heartbeat_seconds:
                yield ": ping\n\n"
                last_emit = time.monotonic()
            time.sleep(poll_seconds)
    finally:
        session.close()


@router.get("/{run_id}/events")
def stream_run_events(
    run_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
    after: Optional[int] = Query(default=None, ge=0),
) -> StreamingResponse:
    # EventSource 无法带自定义头，但同源 Cookie 会自动携带，鉴权语义与其他接口一致
    get_owned_run(session, ctx, run_id)

    # Last-Event-ID 头优先级低于显式 after 参数
    if after is None:
        header = request.headers.get("last-event-id")
        try:
            after = int(header) if header else 0
        except ValueError:
            after = 0

    settings = request.app.state.settings
    return StreamingResponse(
        _sse_stream(
            request.app.state.db,
            run_id,
            after,
            settings.sse_poll_seconds,
            settings.sse_heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
