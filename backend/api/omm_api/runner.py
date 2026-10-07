"""推进器：agents/core 引擎驱动（B2 换脑后）。

- ``WorkflowAdvancer`` 保持旧接口（``advance(run_id)`` / ``advanceable_run_ids``），
  内部委托 engine_glue：领域事件落 ``run_domain_events``（执行事实来源），v1 行为投影。
- sim 节点、投影映射与动作语义见 engine_glue.py；本模块只负责会话事务与后台节拍。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, PendingRollbackError

from omm_contracts import TaskRunStatus

from .config import Settings
from .db import Database
from .engine_glue import advance_run
from .events import lock_run
from .orm import TaskRunRow

logger = logging.getLogger("omm.runner")

# 推进互斥的 advisory lock key（'omm' + runner=01）。所有共库进程用同一个值。
RUNNER_TICK_LOCK_KEY = 0x6F6D6D01


class RunnerLock:
    """跨进程推进互斥：同一时刻只允许一个进程推进任务。

    两个 API 进程共用同一库时（如 ``npm run dev`` 之外又手起一个 uvicorn），
    ``advance_run`` 开头的 ``heal_interrupted`` 会把对方在途的 RUNNING 步骤判成
    executor lost 并整段重跑——双倍扣费。进程内互斥由 RunnerThread 的在途表保证，
    跨进程这层用 PostgreSQL 的会话级 advisory lock 收口：拿不到锁的进程只服务 HTTP、
    不推进，锁随连接断开自动释放，进程被杀也不残留。

    有步骤在途期间锁一直拿着（可能几个小时），所以单独占一条 AUTOCOMMIT 连接，
    不顺带留一个长事务挡住 VACUUM。SQLite 只出现在测试夹具（单进程），无 advisory
    lock，直接放行。
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._connection: Connection | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def try_acquire(self) -> bool:
        if self._held:
            return True
        if self._db.engine.dialect.name != "postgresql":
            self._held = True
            return True
        connection = self._db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = bool(
                connection.execute(select(func.pg_try_advisory_lock(RUNNER_TICK_LOCK_KEY))).scalar()
            )
        except Exception:
            connection.close()
            raise
        if not acquired:
            connection.close()
            return False
        self._connection = connection
        self._held = True
        return True

    def release(self) -> None:
        connection, self._connection = self._connection, None
        self._held = False
        if connection is None:
            return
        try:
            connection.execute(select(func.pg_advisory_unlock(RUNNER_TICK_LOCK_KEY)))
        finally:
            connection.close()


def _superseded_in_flight(error: Exception) -> bool:
    """是不是「在途步骤被控制事件取代」那种 seq 冲突，而不是别的完整性错误。

    SQLite 报 ``UNIQUE constraint failed: run_domain_events.run_id, run_domain_events.seq``，
    PostgreSQL 报 ``violates unique constraint "uq_run_domain_events_run_seq"``；节点中途
    的工具事件先撞上时，后续操作抛的 PendingRollbackError 正文里也带着原始报错。
    只认这一种，其余完整性错误照常抛出，不许被这条让位契约吞掉。
    """
    text = str(error)
    return "run_domain_events" in text and ("seq" in text or "uq_run_domain_events_run_seq" in text)


class WorkflowAdvancer:
    """对单个 run 执行一次最小推进（tick）。线程与测试共用。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def advance(self, run_id: str) -> Optional[str]:
        """推进一步并返回推进后的状态；无事可做返回当前状态。

        事务边界是每条领域事件，不是整个 tick（见 engine_glue._ProjectingSink
        的 checkpoint）：节点执行期间不持有写锁，否则分钟级的 LLM 调用会把并发
        请求堵到 busy_timeout。因此 ``lock_run`` 的行锁只覆盖到第一条事件落盘，
        run 级互斥由 RunnerThread 的在途表保证（同一运行同一时刻只派一个工作线程）；
        跨进程互斥归 RunnerLock / worker 的租约。
        """
        session = self._db.session_factory()
        try:
            run = lock_run(session, run_id)
            if run is None:
                return None
            advance_run(session, run)
            session.commit()
            return run.status
        except (IntegrityError, PendingRollbackError) as error:
            session.rollback()
            if not _superseded_in_flight(error):
                raise
            # 节点执行期间控制面往同一条日志追加了事件（暂停 / 取消 / 从阶段重做，
            # ADR-0019）：这个 tick 的快照已经过期，收尾写 (run_id, seq) 撞唯一约束。
            # 这是契约内的让位而不是故障——控制事件已经把在途步骤关掉，这里丢弃
            # 迟到的结果即可；下个 tick 重放最新日志接着推进。
            logger.warning(
                "run %s: in-flight step superseded by a control action; discarding its result (%s)",
                run_id,
                type(error).__name__,
            )
            return None
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
    """后台推进（T5 演进为独立 worker）：调度线程每个节拍给每个可推进、且没有步骤在途的
    运行派一个工作线程推进一步。

    运行之间互不等待、并行数不设上限——真实节点一步就是一个阶段（实验动辄十几分钟），
    串行推进时其它运行只能干等。同一运行同一时刻只有一个工作线程（在途表），
    ``advance_run`` 开头的 ``heal_interrupted`` 正依赖这一点。
    """

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._advancer = WorkflowAdvancer(db)
        self._interval = settings.runner_tick_seconds
        self._stop = threading.Event()
        self._lock = RunnerLock(db)
        self._lock_blocked_logged = False
        self._in_flight: set[str] = set()
        self._idle = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name="omm-runner", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def in_flight(self) -> list[str]:
        with self._idle:
            return sorted(self._in_flight)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """等在途步骤全部结束；超时返回 False。"""
        with self._idle:
            return self._idle.wait_for(lambda: not self._in_flight, timeout)

    def _tick(self) -> None:
        if not self._lock.try_acquire():
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
            self._dispatch(run_id)
        self._release_if_idle()

    def _dispatch(self, run_id: str) -> None:
        with self._idle:
            if run_id in self._in_flight:
                return
            self._in_flight.add(run_id)
        threading.Thread(
            target=self._work, args=(run_id,), name=f"omm-runner-{run_id}", daemon=True
        ).start()

    def _work(self, run_id: str) -> None:
        try:
            self._advancer.advance(run_id)
        except Exception:  # 一个运行推进失败不连累别的运行，下个节拍照常再派
            logger.exception("run %s: advance failed", run_id)
        finally:
            with self._idle:
                self._in_flight.discard(run_id)
                self._idle.notify_all()

    def _release_if_idle(self) -> None:
        # 有步骤在途时放锁，另一个进程就能抢到并把这些步骤判成 executor lost 重跑
        with self._idle:
            if self._in_flight:
                return
        self._lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # 推进失败不允许杀死线程
                logger.exception("runner tick failed")
            self._stop.wait(self._interval)
        # 停机时还有步骤在途就不放锁：进程退出、连接断开时锁自然释放
        try:
            self._release_if_idle()
        except Exception:
            logger.exception("runner lock release failed")
