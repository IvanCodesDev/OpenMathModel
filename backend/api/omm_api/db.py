from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)

# 非 SQLite 后端的单次建连上限（秒）。libpq 默认无限等待，本地库没起时每次探测
# 都要拖到驱动自己的超时才报错；5 秒足以覆盖本机与内网，远端库可在连接串里覆盖。
CONNECT_TIMEOUT_SECONDS = 5


def _first_line(error: BaseException) -> str:
    text_ = str(error).strip()
    return text_.splitlines()[0] if text_ else type(error).__name__


class Base(DeclarativeBase):
    pass


def _add_missing_sqlite_columns(engine: Engine) -> None:
    """SQLite 开发库补齐模型新增的可空列。

    本地默认链路用 ``create_all`` 建表、从不跑 Alembic，而 ``create_all`` 只建新表、
    不会改已存在的表。没有这一步，任何新增列都会让已有 dev.db 在查询时报
    “no such column”，开发者只能删库重来。这里只补可空列、只作用于 SQLite；
    PostgreSQL 部署仍以 Alembic 为准，两者的一致性由 test_migrations 守住。
    """

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or not column.nullable or column.primary_key:
                    continue
                definition = CreateColumn(column).compile(engine).string
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {definition}"))
                logger.info("dev sqlite schema: added %s.%s", table.name, column.name)


class Database:
    """引擎与会话工厂的持有者，由 create_app 构建并挂到 app.state。"""

    def __init__(self, database_url: str) -> None:
        if database_url.startswith("sqlite"):
            db_path = database_url.split("///", 1)[-1]
            is_file_db = bool(db_path) and db_path != ":memory:"
            if is_file_db:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            # FastAPI 线程池与后台推进线程都会使用连接。timeout 提高 SQLite 的
            # 忙等待上限：RunnerThread 高频提交事件/用量行时，默认 5 秒会让并发的
            # 请求以 "database is locked" 失败（页面表现为对话 500）。
            self.engine = create_engine(
                database_url, connect_args={"check_same_thread": False, "timeout": 30.0}
            )
            if is_file_db:
                # WAL 让写入不再阻塞读取（默认回滚日志是写全库锁）：请求处理、
                # RunnerThread 与 SSE 轮询并发访问同一个 dev.db 时必须开启。
                @event.listens_for(self.engine, "connect")
                def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()
        else:
            self.engine = create_engine(
                database_url,
                pool_pre_ping=True,
                connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            )
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def ping(self) -> str | None:
        """探一次连接：可达返回 None，否则返回一行可读的失败原因（不带 traceback）。"""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except DBAPIError as exc:
            return _first_line(exc.orig if exc.orig is not None else exc)
        return None

    def create_all(self) -> None:
        from . import models, orm  # noqa: F401  确保任务面与账户面模型都已注册

        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            _add_missing_sqlite_columns(self.engine)

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
