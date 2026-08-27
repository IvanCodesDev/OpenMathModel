"""数据库写位竞争：长节点不占锁、认证统计不致命、锁冲突有可判别错误码。

真实节点是分钟级的（LLM 调用、沙箱执行）。把整个 tick 包在一个事务里会让
SQLite 唯一的写位被占满节点执行全程，并发的 HTTP 请求等满 busy_timeout 后以
"database is locked" 失败——页面上表现为对话气泡里的「服务器内部错误」。
这里守住三条防线：写位在节点执行前就已释放、认证的活跃度统计写失败不影响
业务请求、真出现锁冲突时返回可判别且可重试的信封而不是通用 500。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

import pytest
from omm_agent_core import NodeResult, TaskState
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from omm_api import engine_glue
from omm_api.models import AuthSession, utcnow


def _locked_error() -> OperationalError:
    return OperationalError("UPDATE auth_sessions", {}, Exception("database is locked"))


class _WriteLockProbe:
    """节点：执行期间从另一条连接抢一次写位，把结果记下来。

    ``BEGIN IMMEDIATE`` 是探测写位是否可得的标准手法——它立刻申请写锁但不改
    任何数据。忙等设成 0.5 秒，写位若仍被 tick 占着，这里马上失败，不用陪着
    生产配置的 30 秒忙等。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self.ran = False
        self.lock_error: str | None = None

    def run(self, ctx, services) -> NodeResult:
        self.ran = True
        probe = sqlite3.connect(self._db_path, timeout=0.5)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.commit()
        except sqlite3.OperationalError as error:
            self.lock_error = str(error)
        finally:
            probe.close()
        return NodeResult.succeeded(outputs={"label": "题意解析"})


@pytest.mark.skipif(
    bool(os.environ.get("OMM_TEST_DATABASE_URL")),
    reason="单写位是 SQLite 特有的；PostgreSQL 走行级锁，不存在这个竞争",
)
def test_tick_does_not_hold_write_lock_while_node_runs(app, client, make_run, monkeypatch):
    """节点执行期间数据库必须可写：否则并发请求会被堵到 busy_timeout。"""
    probe = _WriteLockProbe(app.state.db.engine.url.database)
    monkeypatch.setitem(engine_glue.SIM_NODES, TaskState.PROBLEM_ANALYSIS, probe)

    run = make_run()
    app.state.advancer.advance(run["id"])

    assert probe.ran, "节点没有被执行，用例没有覆盖到目标路径"
    assert probe.lock_error is None, f"节点执行期间写位仍被 tick 占用：{probe.lock_error}"


def test_tick_persists_events_before_the_node_returns(app, client, make_run, monkeypatch):
    """逐条提交的另一面：步骤开始事件在节点还在跑时就已经可读。

    工作台的执行轨迹靠这些事件实时显示进度，攒到 tick 结束才提交等于分钟级空白。
    """
    seen: list[str] = []

    class _EventReadingNode:
        def run(self, ctx, services) -> NodeResult:
            reader = app.state.db.session_factory()
            try:
                seen.extend(
                    event["type"]
                    for event in reader.execute(
                        select(engine_glue.DomainEventRow.event_type.label("type"))
                        .where(engine_glue.DomainEventRow.run_id == ctx.run_id)
                    ).mappings()
                )
            finally:
                reader.close()
            return NodeResult.succeeded(outputs={"label": "题意解析"})

    monkeypatch.setitem(engine_glue.SIM_NODES, TaskState.PROBLEM_ANALYSIS, _EventReadingNode())

    run = make_run()
    app.state.advancer.advance(run["id"])

    assert "STEP_STARTED" in seen, f"节点运行期间读不到已发生的事件：{seen}"


def test_business_request_survives_failed_activity_write(app, client, monkeypatch):
    """认证里的活跃时间只是统计字段，写不进去不该把业务请求打成 500。"""
    writer = app.state.db.session_factory()
    try:
        auth_session = writer.scalars(select(AuthSession)).first()
        assert auth_session is not None
        auth_session.last_seen_at = utcnow() - timedelta(minutes=5)
        writer.commit()
    finally:
        writer.close()

    original_commit = Session.commit
    failed: list[bool] = []

    def commit_once_then_fail(self, *args, **kwargs):
        # 只打掉第一次提交（认证的滑动写入），其余照常，隔离出被测分支
        if not failed:
            failed.append(True)
            raise _locked_error()
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", commit_once_then_fail)

    response = client.get("/api/account/preferences")

    assert failed, "活跃时间没有触发写入，用例没有覆盖到目标分支"
    assert response.status_code == 200, response.text


def test_database_unavailable_returns_retriable_envelope(client, monkeypatch):
    """锁冲突要给出可判别、可重试的错误码，而不是笼统的「服务器内部错误」。"""

    def boom(self, *args, **kwargs):
        raise _locked_error()

    monkeypatch.setattr(Session, "scalar", boom)

    response = client.get("/api/account/preferences")

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["code"] == "DB_BUSY"
    assert payload["message"] != "服务器内部错误"
    assert response.headers["Retry-After"] == "2"
