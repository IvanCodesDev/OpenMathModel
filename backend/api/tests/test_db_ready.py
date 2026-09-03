"""启动前探库与本地 PostgreSQL 自动拉起（omm_api.db_ready）。

背景：PG 没起时 API 只会在 create_all 抛百行 psycopg 超时 traceback，前端全部接口
ECONNREFUSED；按文档「分开启动」直接跑 uvicorn 的人拿不到 dev-local.mjs 的自动拉库。
这里锚定三件事：① 只对 tools/pg-dev.ps1 管的本地实例自动拉起；② 拉起失败 / 不适用时
抛一行可读、不泄露凭据的提示；③ create_app 注入的 Settings 才是沙盒工作区根。
"""

from __future__ import annotations

import subprocess
import sys
import time
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


class _ScriptedProcess:
    """替身 Popen 返回值：按参数决定 wait() 的结局。"""

    def __init__(self, returncode: int = 0, hang: bool = False) -> None:
        self.returncode = returncode
        self.hang = hang
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("powershell", timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def test_start_local_pg_isolates_console_and_never_reads_pipes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """2026-09-03 事故的两条护栏：子进程不共享控制台（uvicorn --reload 的 CTRL_C_EVENT 会
    广播给同控制台的 postgres 触发 fast shutdown）；输出不走管道（pg_ctl 留下的 cmd.exe 持有
    管道句柄，communicate() 永远等不到 EOF，API 启动卡死）。"""
    calls: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _ScriptedProcess:
        calls["command"] = command
        calls["kwargs"] = kwargs
        kwargs["stdout"].write("PostgreSQL 已启动（port 5433，PID 1）".encode())  # type: ignore[attr-defined]
        return _ScriptedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    script = Path("tools") / "pg-dev.ps1"

    with caplog.at_level("INFO", logger="omm_api.db_ready"):
        assert db_ready.start_local_pg(script) is True

    command = calls["command"]
    assert command[-2:] == [str(script), "start"]  # type: ignore[index]
    kwargs = calls["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL  # type: ignore[index]
    assert kwargs["stderr"] is subprocess.STDOUT  # type: ignore[index]
    assert kwargs["stdout"] is not subprocess.PIPE and hasattr(kwargs["stdout"], "read")  # type: ignore[index]
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW  # type: ignore[index, attr-defined]
    assert "PostgreSQL 已启动" in caplog.text, "脚本输出要进日志"


def test_start_local_pg_gives_up_after_timeout_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hung = _ScriptedProcess(hang=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: hung)
    with caplog.at_level("WARNING", logger="omm_api.db_ready"):
        assert db_ready.start_local_pg(Path("pg-dev.ps1"), timeout_seconds=0.01) is False
    assert hung.killed, "超时必须杀掉 PowerShell，不能留着继续等"
    assert "未返回" in caplog.text

    def failing_popen(command: list[str], **kwargs: object) -> _ScriptedProcess:
        kwargs["stdout"].write("未找到 PostgreSQL 二进制".encode())  # type: ignore[attr-defined]
        return _ScriptedProcess(returncode=1)

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    caplog.clear()
    with caplog.at_level("WARNING", logger="omm_api.db_ready"):
        assert db_ready.start_local_pg(Path("pg-dev.ps1")) is False
    # uvicorn 默认只透出 WARNING 及以上：失败原因必须是 WARNING，用户在终端才看得见
    assert any(
        record.levelname == "WARNING" and "未找到 PostgreSQL 二进制" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.skipif(sys.platform != "win32", reason="pg-dev.ps1 只在 Windows 上被调用")
def test_start_local_pg_returns_while_grandchild_still_holds_inherited_handles(
    tmp_path: Path,
) -> None:
    """真实 PowerShell 复现事故形态：脚本用 -NoNewWindow 起一个继承标准句柄、存活 15 秒的
    cmd.exe 后立刻退出（pg_ctl → cmd.exe → postgres 就是这个结构）。start_local_pg 必须在
    脚本退出后立刻返回，而不是等那个孙进程；旧实现（capture_output 管道）在这里要等满 15 秒。"""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "pg-dev.ps1"
    script.write_text(
        "\n".join(
            [
                "param([string]$Action)",
                "$p = Start-Process -FilePath cmd.exe "
                "-ArgumentList '/c','ping -n 16 127.0.0.1 > nul' -NoNewWindow -PassThru",
                f"Set-Content -Path '{pid_file}' -Value $p.Id",
                'Write-Output "stub $Action ok"',
                "exit 0",
            ]
        ),
        encoding="ascii",
    )
    started = time.monotonic()
    try:
        assert db_ready.start_local_pg(script, timeout_seconds=30) is True
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"不该等孙进程：耗时 {elapsed:.1f}s"
    finally:
        if pid_file.exists():
            pid = pid_file.read_text(encoding="utf-8").strip()
            if pid.isdigit():
                subprocess.run(
                    ["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, check=False
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
