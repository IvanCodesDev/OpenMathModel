"""推进器：agents/core 引擎驱动（B2 换脑后）。

- ``WorkflowAdvancer`` 保持旧接口（``advance(run_id)`` / ``advanceable_run_ids``），
  内部委托 engine_glue：领域事件落 ``run_domain_events``（执行事实来源），v1 行为投影。
- sim 节点、投影映射与动作语义见 engine_glue.py；本模块只负责会话事务与后台节拍。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import and_, func, or_, select

from omm_contracts import TaskRunStatus

from .config import Settings
from .db import Database
from .engine_glue import advance_run
from .events import lock_run
from .orm import TaskRunRow

logger = logging.getLogger("omm.runner")

# 推进互斥的 advisory lock key（'omm' + runner=01）。所有共库进程用同一个值。
RUNNER_TICK_LOCK_KEY = 0x6F6D6D01


@contextmanager
def runner_tick_mutex(db: Database) -> Iterator[bool]:
    """跨进程推进互斥：同一时刻只允许一个进程执行 runner tick。

    两个 API 进程共用同一库时（如 ``npm run dev`` 之外又手起一个 uvicorn），
    ``advance_run`` 开头的 ``heal_interrupted`` 会把对方在途的 RUNNING 步骤判成
    executor lost 并整段重跑——双倍扣费。进程内互斥由「唯一推进线程」保证，
    跨进程这层用 PostgreSQL 的会话级 advisory lock 收口：拿不到锁的进程本 tick
    直接放弃（只服务 HTTP，不推进），锁随连接断开自动释放，进程被杀也不残留。
    SQLite 只出现在测试夹具（单进程），无 advisory lock，直接放行。
    """
    session = db.session_factory()
    try:
        if session.get_bind().dialect.name != "postgresql":
            yield True
            return
        acquired = bool(
            session.execute(select(func.pg_try_advisory_lock(RUNNER_TICK_LOCK_KEY))).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                session.execute(select(func.pg_advisory_unlock(RUNNER_TICK_LOCK_KEY)))
                # 会话级锁与事务无关，rollback 只为把连接干净地还回池子
                session.rollback()
    finally:
        session.close()


class WorkflowAdvancer:
    """对单个 run 执行一次最小推进（tick）。线程与测试共用。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def advance(self, run_id: str) -> Optional[str]:
        """推进一步并返回推进后的状态；无事可做返回当前状态。

        事务边界是每条领域事件，不是整个 tick（见 engine_glue._ProjectingSink
        的 checkpoint）：节点执行期间不持有写锁，否则分钟级的 LLM 调用会把并发
        请求堵到 busy_timeout。因此 ``lock_run`` 的行锁只覆盖到第一条事件落盘，
        run 级互斥由「进程内只有一个推进线程」保证；跨进程互斥归 worker 的租约。
        """
        session = self._db.session_factory()
        try:
            run = lock_run(session, run_id)
            if run is None:
                return None
            advance_run(session, run)
            session.commit()
            return run.status
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def advanceable_run_ids(self) -> list[str]:
        session = self._db.session_factory()
        try:
            rows = session.execute(
                select(TaskRunRow.id).where(
                    or_(
                        and_(
                            TaskRunRow.status == TaskRunStatus.QUEUED.value,
                            TaskRunRow.auto_start.is_(True),
                        ),
                        TaskRunRow.status == TaskRunStatus.RUNNING.value,
                    )
                )
            ).scalars()
            return list(rows)
        finally:
            session.close()


class RunnerThread:
    """后台推进线程：周期性对可推进的 run 执行 tick（T5 演进为独立 worker）。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._advancer = WorkflowAdvancer(db)
        self._interval = settings.runner_tick_seconds
        self._stop = threading.Event()
        self._lock_blocked_logged = False
        self._thread = threading.Thread(
            target=self._loop, name="omm-mock-runner", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _tick(self) -> None:
        with runner_tick_mutex(self._db) as acquired:
            if not acquired:
                # 状态未变化时不刷屏：从「被挡」到「重新拿到」各提示一次
                if not self._lock_blocked_logged:
                    logger.warning(
                        "另一个进程正持有推进锁（advisory %#x）：本进程只服务 HTTP、"
                        "不推进任务，避免双跑互相把在途步骤判死重跑",
                        RUNNER_TICK_LOCK_KEY,
                    )
                    self._lock_blocked_logged = True
                return
            if self._lock_blocked_logged:
                logger.info("推进锁已重新拿到，本进程恢复推进任务")
                self._lock_blocked_logged = False
            for run_id in self._advancer.advanceable_run_ids():
                if self._stop.is_set():
                    break
                self._advancer.advance(run_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # 推进失败不允许杀死线程
                logger.exception("runner tick failed")
            self._stop.wait(self._interval)
