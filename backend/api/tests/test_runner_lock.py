"""跨进程推进互斥（runner_tick_mutex，PostgreSQL advisory lock）的回归用例。

背景：两个 API 进程共用同一库时（npm run dev 之外又手起一个 uvicorn），
``advance_run`` 开头的 ``heal_interrupted`` 会把对方在途的 RUNNING 步骤判成
executor lost 并整段重跑——双倍扣费。互斥语义见 runner.py::runner_tick_mutex。

PG 专属用例按 OMM_TEST_DATABASE_URL 是否存在跳过：SQLite 没有 advisory lock，
夹具本身也只有单进程，直接放行即是正确行为（有专门用例锚定）。
"""

from __future__ import annotations

import os

import pytest
from conftest import create_project, create_run
from sqlalchemy import func, select

from omm_api.runner import RUNNER_TICK_LOCK_KEY, RunnerThread, runner_tick_mutex

requires_postgres = pytest.mark.skipif(
    not os.environ.get("OMM_TEST_DATABASE_URL"),
    reason="advisory lock 只在真实 PostgreSQL 上可测（OMM_TEST_DATABASE_URL 未设置）",
)


def test_tick_mutex_grants_when_uncontended(app) -> None:
    """无竞争时放行：SQLite 直接放行；PG 拿锁成功。两种方言都必须为真。"""
    with runner_tick_mutex(app.state.db) as acquired:
        assert acquired is True
    # 出上下文后必须已释放：紧接着再进一次仍能拿到
    with runner_tick_mutex(app.state.db) as acquired:
        assert acquired is True


@requires_postgres
def test_tick_mutex_excludes_rival_connection(app) -> None:
    """别的连接（等价于另一个进程）持锁期间拿不到；对方释放后恢复。"""
    db = app.state.db
    rival = db.engine.connect()
    try:
        held = rival.execute(
            select(func.pg_try_advisory_lock(RUNNER_TICK_LOCK_KEY))
        ).scalar()
        assert held is True
        with runner_tick_mutex(db) as acquired:
            assert acquired is False
    finally:
        rival.execute(select(func.pg_advisory_unlock(RUNNER_TICK_LOCK_KEY)))
        rival.close()
    with runner_tick_mutex(db) as acquired:
        assert acquired is True


@requires_postgres
def test_runner_tick_does_not_advance_while_rival_holds_lock(client) -> None:
    """行为面：锁被占时整个 tick 跳过（不推进、不 heal）；释放后照常推进。"""
    project = create_project(client)
    run = create_run(client, project["id"], goal="互斥用例", auto_start=True)
    thread = RunnerThread(client.app.state.db, client.app.state.settings)

    db = client.app.state.db
    rival = db.engine.connect()
    try:
        assert rival.execute(
            select(func.pg_try_advisory_lock(RUNNER_TICK_LOCK_KEY))
        ).scalar() is True
        thread._tick()
        after_blocked = client.get(f"/api/v1/task-runs/{run['id']}").json()
        assert after_blocked["status"] == "QUEUED", "锁被占时不得推进任何 run"
    finally:
        rival.execute(select(func.pg_advisory_unlock(RUNNER_TICK_LOCK_KEY)))
        rival.close()

    thread._tick()
    after_released = client.get(f"/api/v1/task-runs/{run['id']}").json()
    assert after_released["status"] != "QUEUED", "锁释放后 tick 应恢复推进"
