"""启动前探库与本地 PostgreSQL 自动拉起（omm_api.db_ready）。

背景：PG 没起时 API 只会在 create_all 抛百行 psycopg 超时 traceback，前端全部接口
ECONNREFUSED；按文档「分开启动」直接跑 uvicorn 的人拿不到 dev-local.mjs 的自动拉库。
这里锚定三件事：① 只对 tools/pg-dev.ps1 管的本地实例自动拉起；② 拉起失败 / 不适用时
抛一行可读、不泄露凭据的提示；③ create_app 注入的 Settings 才是沙盒工作区根。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omm_api import db_ready, engine_glue
from omm_api.config import Settings
from omm_api.db import Database
from omm_api.db_ready import (
    DatabaseUnavailableError,
    describe_target,
    ensure_database_ready,
    manages_local_pg,
)
from omm_api.main import create_app

LOCAL_URL = "postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel"
# 5439 上没有任何服务：连接立即被拒绝，用例不依赖真实 PG，也不会误拉起本地实例
DEAD_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:5439/nothing"


class _FakeDb:
    """按脚本依次返回 ping 结果（None = 可达）。"""

    def __init__(self, *results: str | None) -> None:
        self._results = list(results)
        self.pings = 0

    def ping(self) -> str | None:
        self.pings += 1
        return self._results.pop(0)


def test_manages_local_pg_only_for_pg_dev_instance(tmp_path: Path) -> None:
    script = tmp_path / "pg-dev.ps1"
    script.write_text("# stub", encoding="utf-8")
    assert manages_local_pg(LOCAL_URL, script=script, platform="win32")
    assert manages_local_pg(
        LOCAL_URL.replace("127.0.0.1", "localhost"), script=script, platform="win32"
    )
    # Docker 底座 5432 / 远端库 / SQLite / 非 Windows / 脚本缺席 / 非法串：一律不插手
    assert not manages_local_pg(LOCAL_URL.replace("5433", "5432"), script=script, platform="win32")
    assert not manages_local_pg(
        LOCAL_URL.replace("127.0.0.1", "10.0.0.8"), script=script, platform="win32"
    )
    assert not manages_local_pg("sqlite:///dev.db", script=script, platform="win32")
    assert not manages_local_pg(LOCAL_URL, script=script, platform="linux")
    assert not manages_local_pg(LOCAL_URL, script=tmp_path / "missing.ps1", platform="win32")
    assert not manages_local_pg("not a url", script=script, platform="win32")


def test_describe_target_hides_credentials() -> None:
    assert describe_target(LOCAL_URL) == "postgresql://127.0.0.1:5433/openmathmodel"
    assert describe_target("sqlite:///dev.db") == "sqlite:dev.db"
    assert "openmathmodel:openmathmodel" not in describe_target(LOCAL_URL)


def test_unreachable_database_raises_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(db_ready, "start_local_pg", lambda *a, **k: started.append("x") or True)
    settings = Settings(database_url=DEAD_URL, local_pg_autostart=True)
    db = Database(settings.database_url)
    try:
        with pytest.raises(DatabaseUnavailableError) as excinfo:
            ensure_database_ready(db, settings)
    finally:
        db.dispose()
    message = str(excinfo.value)
    assert "数据库连不上：postgresql://127.0.0.1:5439/nothing" in message
    assert "pg-dev.ps1 start" in message and "npm run dev" in message, "提示要能照着做"
    assert "nobody:nothing" not in message, "不泄露凭据"
    assert "Traceback" not in message
    assert started == [], "5439 不是 pg-dev 实例，不得自动拉起"


def test_autostart_runs_pg_dev_once_then_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上 + 目标是本地 pg-dev 实例 → 拉起一次并复探；复探成功则正常放行。"""
    settings = Settings(database_url=LOCAL_URL, local_pg_autostart=True)
    started: list[str] = []
    monkeypatch.setattr(db_ready, "manages_local_pg", lambda url, **kwargs: True)
    monkeypatch.setattr(db_ready, "start_local_pg", lambda *a, **k: started.append("x") or True)
    db = _FakeDb("connection refused", None)

    ensure_database_ready(db, settings)  # type: ignore[arg-type]

    assert started == ["x"]
    assert db.pings == 2


def test_autostart_disabled_or_failed_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_ready, "manages_local_pg", lambda url, **kwargs: True)

    # 开关关掉：一次都不拉起
    started: list[str] = []
    monkeypatch.setattr(db_ready, "start_local_pg", lambda *a, **k: started.append("x") or True)
    with pytest.raises(DatabaseUnavailableError):
        ensure_database_ready(
            _FakeDb("connection refused"),  # type: ignore[arg-type]
            Settings(database_url=LOCAL_URL, local_pg_autostart=False),
        )
    assert started == []

    # 拉起脚本失败：不复探、照样报错
    monkeypatch.setattr(db_ready, "start_local_pg", lambda *a, **k: False)
    db = _FakeDb("connection refused")
    with pytest.raises(DatabaseUnavailableError):
        ensure_database_ready(db, Settings(database_url=LOCAL_URL, local_pg_autostart=True))  # type: ignore[arg-type]
    assert db.pings == 1

    # 拉起成功但复探仍失败：报错文案带复探结果
    monkeypatch.setattr(db_ready, "start_local_pg", lambda *a, **k: True)
    with pytest.raises(DatabaseUnavailableError, match="still down"):
        ensure_database_ready(
            _FakeDb("connection refused", "still down"),  # type: ignore[arg-type]
            Settings(database_url=LOCAL_URL, local_pg_autostart=True),
        )


def test_create_app_binds_runtime_settings_for_sandbox_workspace(tmp_path: Path) -> None:
    """create_app 注入的 Settings 必须成为沙盒工作区根，测试夹具的 tmp_path 才真隔离。"""
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'ready.db').as_posix()}",
        runner_enabled=False,
        retention_sweep_enabled=False,
        paper_export_worker_enabled=False,
        artifacts_dir=tmp_path / "artifacts",
        avatars_dir=tmp_path / "avatars",
        workspaces_dir=tmp_path / "workspaces",
    )
    create_app(settings)
    assert engine_glue.runtime_settings().workspaces_dir == tmp_path / "workspaces"
    assert engine_glue.runtime_settings().artifacts_dir == tmp_path / "artifacts"
