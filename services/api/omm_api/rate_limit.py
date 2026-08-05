"""登录限速：数据库实现（多实例一致）。

进程内内存版（security.RateLimiter）只在单副本内有效；本实现把失败记录
落到 login_attempts 表，多副本部署时窗口计数一致。后续接入 Redis 时以相同
接口（allow / record_failure / reset）替换实现即可。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from .ids import new_id
from .orm import LoginAttemptRow
from .serialize import utcnow


class DbLoginRateLimiter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        max_attempts: int,
        window_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def _cutoff(self):
        return utcnow() - timedelta(seconds=self.window_seconds)

    def allow(self, keys: Iterable[str]) -> bool:
        keys = list(keys)
        session = self._session_factory()
        try:
            # 顺带清理过期记录，避免表无界增长
            session.execute(
                delete(LoginAttemptRow).where(LoginAttemptRow.attempted_at < self._cutoff())
            )
            for key in keys:
                count = session.execute(
                    select(func.count())
                    .select_from(LoginAttemptRow)
                    .where(
                        LoginAttemptRow.key == key,
                        LoginAttemptRow.attempted_at >= self._cutoff(),
                    )
                ).scalar_one()
                if count >= self.max_attempts:
                    session.commit()
                    return False
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_failure(self, keys: Iterable[str]) -> None:
        session = self._session_factory()
        try:
            now = utcnow()
            for key in keys:
                session.add(
                    LoginAttemptRow(id=new_id("la"), key=key, attempted_at=now)
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def reset(self, keys: Iterable[str]) -> None:
        session = self._session_factory()
        try:
            session.execute(
                delete(LoginAttemptRow).where(LoginAttemptRow.key.in_(list(keys)))
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
