"""推进器：agents/core 引擎驱动（B2 换脑后）。

- ``WorkflowAdvancer`` 保持旧接口（``advance(run_id)`` / ``advanceable_run_ids``），
  内部委托 engine_glue：领域事件落 ``run_domain_events``（执行事实来源），v1 行为投影。
- sim 节点、投影映射与动作语义见 engine_glue.py；本模块只负责会话事务与后台节拍。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy import and_, or_, select

from omm_contracts import TaskRunStatus

from .config import Settings
from .db import Database
from .engine_glue import advance_run
from .events import lock_run
from .orm import TaskRunRow

logger = logging.getLogger("omm.runner")


class WorkflowAdvancer:
    """对单个 run 执行一次最小推进（tick）。线程与测试共用。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def advance(self, run_id: str) -> Optional[str]:
        """推进一步并返回推进后的状态；无事可做返回当前状态。"""
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
        self._advancer = WorkflowAdvancer(db)
        self._interval = settings.runner_tick_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="omm-mock-runner", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for run_id in self._advancer.advanceable_run_ids():
                    if self._stop.is_set():
                        break
                    self._advancer.advance(run_id)
            except Exception:  # 推进失败不允许杀死线程
                logger.exception("runner tick failed")
            self._stop.wait(self._interval)
