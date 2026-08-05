from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    """引擎与会话工厂的持有者，由 create_app 构建并挂到 app.state。"""

    def __init__(self, database_url: str) -> None:
        if database_url.startswith("sqlite"):
            db_path = database_url.split("///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # FastAPI 线程池与后台推进线程都会使用连接
            self.engine = create_engine(
                database_url, connect_args={"check_same_thread": False}
            )
        else:
            self.engine = create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def create_all(self) -> None:
        from . import models, orm  # noqa: F401  确保任务面与账户面模型都已注册

        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        """删除全部业务表（PostgreSQL 测试隔离用；生产禁用）。"""
        from . import models, orm  # noqa: F401

        Base.metadata.drop_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI 依赖：请求级会话，成功提交、异常回滚。"""
    session: Session = request.app.state.db.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# 认证模块使用的别名（同一依赖，两个惯用名）
get_db = get_session
