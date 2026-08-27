"""Alembic 迁移与 ORM metadata 的一致性检查（SQLite 上执行；PostgreSQL 路径由 CI 的 api-postgres job 对真库实跑）。"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from conftest import SERVICE_ROOT
from omm_api import models, orm  # noqa: F401  注册任务面与账户面模型
from omm_api.db import Base, Database


def test_alembic_upgrade_head_matches_metadata(tmp_path, monkeypatch):
    db_url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    monkeypatch.setenv("OMM_DATABASE_URL", db_url)

    config = Config(str(SERVICE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVICE_ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected = set(Base.metadata.tables.keys())
    missing = expected - tables
    assert not missing, f"迁移缺表: {missing}"
    assert "alembic_version" in tables

    for table in expected:
        migrated_columns = {c["name"] for c in inspector.get_columns(table)}
        model_columns = {c.name for c in Base.metadata.tables[table].columns}
        assert migrated_columns == model_columns, f"{table} 列不一致: {migrated_columns ^ model_columns}"

    unique_constraints = inspector.get_unique_constraints("agent_events")
    assert any(
        set(uc["column_names"]) == {"run_id", "sequence"} for uc in unique_constraints
    ), "缺少 (run_id, sequence) 唯一约束"


def test_create_all_backfills_new_nullable_columns_on_sqlite(tmp_path):
    """本地 dev.db 从不跑迁移：模型新增可空列后，启动必须补列且保留已有账户。"""
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE users ("
        "id TEXT PRIMARY KEY, email TEXT, name TEXT, password_hash TEXT, plan TEXT,"
        " totp_secret TEXT, totp_enabled BOOLEAN, password_changed_at DATETIME, created_at DATETIME)"
    )
    connection.execute(
        "INSERT INTO users VALUES"
        " ('u1', 'legacy@test.dev', '旧用户', 'hash', '个人专业版', NULL, 0, '2026-01-01', '2026-01-01')"
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{db_path.as_posix()}")
    database.create_all()
    database.dispose()

    connection = sqlite3.connect(db_path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    assert {"avatar_sha256", "avatar_media_type"} <= columns
    assert connection.execute("SELECT name FROM users WHERE id = 'u1'").fetchone()[0] == "旧用户"
    connection.close()
