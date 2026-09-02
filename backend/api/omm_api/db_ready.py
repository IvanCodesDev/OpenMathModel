"""启动前的数据库就绪检查（含本地免安装 PostgreSQL 的自动拉起）。

`npm run dev`（tools/dev-local.mjs）起 API 前会先探 5433、没起就跑 `pg-dev.ps1 start`；
但按文档「分开启动」直接跑 uvicorn 的人拿不到这层照顾——PG 一停（开机后没起、或哪个
会话收尾时顺手 stop 了），API 就只剩百行 psycopg 超时 traceback，前端全部接口跟着
ECONNREFUSED。这里把同一判断搬进 API 启动路径：

1. 先 `SELECT 1` 探库；
2. 连不上且目标就是 tools/pg-dev.ps1 管的本地实例（Windows、127.0.0.1/localhost:5433、
   脚本在场）→ 自动拉起一次再探；Docker 5432、远端库、非 Windows 一律不插手；
3. 仍连不上 → 抛 `DatabaseUnavailableError`，一行说清「连不上哪、怎么起」。
"""

from __future__ import annotations

import locale
import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import SERVICE_ROOT, Settings
from .db import Database

logger = logging.getLogger(__name__)

LOCAL_PG_PORT = 5433
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# tools/pg-dev.ps1 在仓库根；SERVICE_ROOT = backend/api
PG_DEV_SCRIPT = SERVICE_ROOT.parents[1] / "tools" / "pg-dev.ps1"
# pg_ctl start -w -t 60 的等待上限，再留 PowerShell 启动开销
START_TIMEOUT_SECONDS = 90.0


class DatabaseUnavailableError(RuntimeError):
    """启动时数据库不可达。消息即用户可照着做的提示，不夹带驱动 traceback。"""


def describe_target(database_url: str) -> str:
    """连接串的可读目标（不含凭据）：backend://host:port/database。"""
    try:
        url = make_url(database_url)
    except Exception:  # 非法连接串也要能原样报出去
        return database_url
    backend = url.get_backend_name()
    if backend == "sqlite":
        return f"sqlite:{url.database or ':memory:'}"
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    return f"{backend}://{host}{port}/{url.database or ''}"


def manages_local_pg(
    database_url: str,
    *,
    script: Path = PG_DEV_SCRIPT,
    platform: str = sys.platform,
) -> bool:
    """连接串是否指向 tools/pg-dev.ps1 管理的本地实例（且脚本在、本机是 Windows）。"""
    if platform != "win32" or not script.is_file():
        return False
    try:
        url = make_url(database_url)
    except Exception:
        return False
    if url.get_backend_name() != "postgresql":
        return False
    return (url.host or "") in _LOCAL_HOSTS and url.port == LOCAL_PG_PORT


def start_local_pg(
    script: Path = PG_DEV_SCRIPT, timeout_seconds: float = START_TIMEOUT_SECONDS
) -> bool:
    """跑一次 `pg-dev.ps1 start`；返回是否成功退出。脚本输出并入本进程日志。"""
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "start",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout_seconds, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("自动拉起本地 PostgreSQL 失败：%s", exc)
        return False
    output = (_decode_console(completed.stdout) + _decode_console(completed.stderr)).strip()
    if output:
        logger.info("pg-dev.ps1 start（exit=%s）：%s", completed.returncode, output)
    return completed.returncode == 0


def _decode_console(raw: bytes) -> str:
    """PowerShell 子进程的输出编码随控制台代码页变（UTF-8 或 GBK 都可能）：
    先按 UTF-8 严格解，解不出再按本机首选编码兜底，别把脚本的中文提示解成乱码。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def ensure_database_ready(db: Database, settings: Settings) -> None:
    """探库；本地 pg-dev 实例没起就拉起一次；仍不可达则抛 DatabaseUnavailableError。"""
    error = db.ping()
    if error is None:
        return
    target = describe_target(settings.database_url)
    if settings.local_pg_autostart and manages_local_pg(settings.database_url):
        logger.warning("数据库连不上（%s：%s），尝试自动拉起本地 PostgreSQL…", target, error)
        if start_local_pg():
            error = db.ping()
            if error is None:
                logger.info("本地 PostgreSQL 已拉起，数据库就绪：%s", target)
                return
    message = (
        f"数据库连不上：{target}（{error}）。"
        "本地开发请先起 PostgreSQL：.\\tools\\pg-dev.ps1 start（首次先 init），"
        "或直接 npm run dev（会自动拉起）；连接串由 OMM_DATABASE_URL / backend/api/.env 决定。"
    )
    logger.error(message)
    raise DatabaseUnavailableError(message)
