"""Alembic 迁移与 ORM metadata 的一致性检查（SQLite 上执行；PostgreSQL 路径待底座就绪后补验）。"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from conftest import SERVICE_ROOT
from omm_api import models, orm  # noqa: F401  注册任务面与账户面模型
from omm_api.db import Base


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
