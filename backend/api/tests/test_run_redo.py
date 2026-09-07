"""任意非完成状态从选定阶段重做（ADR-0019）：engine_glue.redo_run 与推进器让位契约。

对话层的提案 → 确认 → 执行链路在 test_run_control.py；这里钉的是控制面本身：
RUN_REDO 的投影结果、HTTP 语义的错误码、以及「在途步骤被控制事件取代」时推进器
把 seq 唯一约束冲突当作让位而不是故障。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from omm_agent_core.models import TaskState
from omm_api import engine_glue, runner as runner_module
from omm_api.engine_glue import redo_run
from omm_api.errors import ApiError
from omm_api.orm import AgentEventRow, RunNoteRow, StepRunRow, TaskRunRow
from omm_api.runner import WorkflowAdvancer, _superseded_in_flight


def _steps(client, run_id: str) -> dict[str, list[tuple[int, str]]]:
    grouped: dict[str, list[tuple[int, str]]] = {}
    for step in client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]:
        grouped.setdefault(step["node"], []).append((step["attempt"], step["status"]))
    return {node: sorted(items) for node, items in grouped.items()}


def _redo(client, run_id: str, stage: str, text: str) -> dict:
    with client.app.state.db.session_factory() as session:
        run = session.get(TaskRunRow, run_id)
        receipt = redo_run(session, run, stage, text)
        session.commit()
    return receipt


def test_redo_run_rewinds_a_failed_run_and_leaves_a_note(client, make_run, tick):
    run_id = make_run("实验容错验证 [fail:experiment]")["id"]
    tick(run_id, times=3)
    assert client.post(f"/api/v1/task-runs/{run_id}/actions", json={"action": "approve"}).status_code == 200
    assert tick(run_id) == "FAILED"

    receipt = _redo(client, run_id, "MODEL_PLANNING", "  换成随机森林重新做  ")
    assert receipt["stage"] == "MODEL_PLANNING" and receipt["from_status"] == "FAILED"
    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "RUNNING" and run["current_node"] == "MODEL_PLANNING"
    assert run["failure"] is None and run["ended_at"] is None

    with client.app.state.db.session_factory() as session:
        notes = session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all()
        assert [(n.scope, n.text) for n in notes] == [("global", "换成随机森林重新做")]
        assert notes[0].id == receipt["note_id"]
        logs = [
            row.payload
            for row in session.scalars(select(AgentEventRow).where(AgentEventRow.run_id == run_id)).all()
            if row.type == "run.log" and (row.payload or {}).get("kind") == "redo_requested"
        ]
        assert len(logs) == 1 and logs[0]["stage"] == "MODEL_PLANNING" and logs[0]["note_id"] == receipt["note_id"]

    # 从建模方案起第 2 趟，回到 G1；上游两段不重跑
    assert tick(run_id) == "WAITING_APPROVAL"
    steps = _steps(client, run_id)
    assert steps["PROBLEM_ANALYSIS"] == [(1, "SUCCEEDED")] and steps["DATA_PREPARATION"] == [(1, "SUCCEEDED")]
    assert steps["MODEL_PLANNING"] == [(1, "SUCCEEDED"), (2, "SUCCEEDED")]
    assert steps["EXPERIMENTING"] == [(1, "FAILED")]


def _complete(client, tick, run_id: str) -> None:
    tick(run_id, times=3)  # 到 G1
    assert client.post(f"/api/v1/task-runs/{run_id}/actions", json={"action": "approve"}).status_code == 200
    for _ in range(6):
        if tick(run_id) == "COMPLETED":
            return
    raise AssertionError("模拟链 6 个 tick 内应完成")


def test_redo_run_refuses_wrong_states_and_bad_input(client, make_run, tick):
    run_id = make_run("重做边界")["id"]
    with client.app.state.db.session_factory() as session:
        run = session.get(TaskRunRow, run_id)
        with pytest.raises(ApiError) as queued:
            redo_run(session, run, "PROBLEM_ANALYSIS", "重来")
        assert (queued.value.http_status, queued.value.code) == (409, "RUN_NOT_REDOABLE")

    assert tick(run_id) == "RUNNING"
    with client.app.state.db.session_factory() as session:
        run = session.get(TaskRunRow, run_id)
        with pytest.raises(ApiError) as bad_stage:
            redo_run(session, run, "NOPE", "重来")
        assert (bad_stage.value.http_status, bad_stage.value.code) == (422, "INVALID_STAGE")
        with pytest.raises(ApiError) as empty:
            redo_run(session, run, "PROBLEM_ANALYSIS", "   ")
        assert (empty.value.http_status, empty.value.code) == (422, "EMPTY_TEXT")
        session.rollback()

    done = make_run("已完成边界")["id"]
    _complete(client, tick, done)
    with client.app.state.db.session_factory() as session:
        run = session.get(TaskRunRow, done)
        with pytest.raises(ApiError) as completed:
            redo_run(session, run, "DATA_PREPARATION", "重来")
        assert completed.value.code == "RUN_NOT_REDOABLE" and "修订" in completed.value.message


def test_superseded_in_flight_only_matches_the_domain_event_seq_constraint():
    sqlite = IntegrityError(
        "INSERT INTO run_domain_events ...",
        {},
        Exception("UNIQUE constraint failed: run_domain_events.run_id, run_domain_events.seq"),
    )
    postgres = IntegrityError(
        "INSERT INTO run_domain_events ...",
        {},
        Exception('duplicate key value violates unique constraint "uq_run_domain_events_run_seq"'),
    )
    unrelated = IntegrityError("INSERT INTO step_runs ...", {}, Exception("UNIQUE constraint failed: step_runs.id"))
    assert _superseded_in_flight(sqlite) and _superseded_in_flight(postgres)
    assert not _superseded_in_flight(unrelated)
    assert not _superseded_in_flight(RuntimeError("run_domain_events is fine"))  # 没提 seq


class _RedoMidway:
    """包一层模拟节点：执行中途从另一条会话对同一运行发起 redo（等价于用户在对话里确认）。"""

    def __init__(self, inner, session_factory, run_id: str, stage: str) -> None:
        self._inner = inner
        self._session_factory = session_factory
        self._run_id = run_id
        self._stage = stage
        self.fired = 0

    def run(self, ctx, services):
        if self.fired == 0:
            self.fired += 1
            with self._session_factory() as session:
                run = session.get(TaskRunRow, self._run_id)
                redo_run(session, run, self._stage, "题意理解错了，重新解析")
                session.commit()
        return self._inner.run(ctx, services)


def test_in_flight_step_yields_to_a_redo_and_the_next_tick_restarts_from_target(client, make_run, tick, monkeypatch, caplog):
    run_id = make_run("在途让位")["id"]
    assert tick(run_id) == "RUNNING"  # 题意解析已完成

    midway = _RedoMidway(
        engine_glue.SIM_NODES[TaskState.DATA_PREPARATION],
        client.app.state.db.session_factory,
        run_id,
        "PROBLEM_ANALYSIS",
    )
    monkeypatch.setitem(engine_glue.SIM_NODES, TaskState.DATA_PREPARATION, midway)
    # test_migrations 跑过 alembic 的 fileConfig 后会把已存在的 logger 全部 disabled；
    # 这里只在本用例内恢复 omm.runner，好让 caplog 收得到让位告警。
    monkeypatch.setattr(runner_module.logger, "disabled", False)

    # 这个 tick 的数据准备步骤在执行中被 redo 取代：收尾写 seq 撞唯一约束，推进器丢弃结果
    with caplog.at_level(logging.WARNING, logger=runner_module.logger.name):
        assert tick(run_id) is None
    assert midway.fired == 1
    assert any("superseded by a control action" in record.getMessage() for record in caplog.records)

    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "RUNNING" and run["current_node"] == "PROBLEM_ANALYSIS"
    steps = _steps(client, run_id)
    assert steps["DATA_PREPARATION"] == [(1, "CANCELLED")]
    with client.app.state.db.session_factory() as session:
        cancelled = session.scalars(
            select(StepRunRow).where(StepRunRow.run_id == run_id, StepRunRow.node == "DATA_PREPARATION")
        ).one()
        assert cancelled.detail == "已被「从「题意解析」重做」取代" and cancelled.ended_at is not None

    # 下个 tick 从题意解析第 2 趟起跑；被取代的步骤不会再被 heal 判成 executor lost
    assert tick(run_id) == "RUNNING"
    steps = _steps(client, run_id)
    assert steps["PROBLEM_ANALYSIS"] == [(1, "SUCCEEDED"), (2, "SUCCEEDED")]
    assert steps["DATA_PREPARATION"] == [(1, "CANCELLED")]
    assert tick(run_id) == "RUNNING"
    assert _steps(client, run_id)["DATA_PREPARATION"] == [(1, "CANCELLED"), (2, "SUCCEEDED")]


def test_advancer_reraises_unrelated_integrity_errors(client, make_run, monkeypatch):
    run_id = make_run("完整性错误不吞")["id"]
    boom = IntegrityError("INSERT INTO step_runs ...", {}, Exception("UNIQUE constraint failed: step_runs.id"))

    def explode(session, run):
        raise boom

    monkeypatch.setattr(runner_module, "advance_run", explode)
    with pytest.raises(IntegrityError):
        WorkflowAdvancer(client.app.state.db).advance(run_id)
