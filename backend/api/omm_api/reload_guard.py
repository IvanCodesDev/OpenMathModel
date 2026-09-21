"""开发期护栏：识别「uvicorn --reload 的监视范围盖住了沙盒工作区」这一自伤配置。

沙盒每次 ``python_run`` 都把脚本落成 ``<workspaces_dir>/<run>/steps/<step>/main.py``；
``uvicorn --reload`` 不带 ``--reload-dir`` 时监视整个 cwd 下的 ``*.py``——脚本一落盘
API 就被重载，在途阶段被打断，下个进程把它判成 executor lost 再自动重跑，阶段永远
跑不完（2026-09-07 / 2026-09-18 两次真实事故，用户侧只看到「本次调用中断」反复出现，
并不知道是热重载在杀进程）。API 进程改不了父进程（reloader）的监视范围，但它能在
启动时看出这一点：spawn 出来的子进程继承父进程的 ``sys.argv`` 与 cwd。诊断只进服务端
日志，不进用户界面。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("omm.reload_guard")

RECOMMENDED_COMMAND = "npm run dev:api"
RECOMMENDED_FLAGS = "--reload-dir backend/api/omm_api --reload-dir agents"


def _option_values(argv: Sequence[str], name: str) -> list[str]:
    """取 click 风格选项的全部取值：``--opt VALUE`` 与 ``--opt=VALUE`` 两种写法。"""
    values: list[str] = []
    prefix = f"{name}="
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif arg.startswith(prefix):
            values.append(arg[len(prefix):])
    return values


def _resolve(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def reload_watch_dirs(
    argv: Sequence[str], cwd: Path, environ: dict[str, str] | None = None
) -> list[Path] | None:
    """这条命令行下 uvicorn 热重载监视的目录；不是 ``--reload`` 启动时返回 None。

    与 uvicorn ``Config`` 同一口径：给了 ``--reload-dir`` 就只看那些目录，否则监视 cwd；
    click 允许用环境变量 ``UVICORN_RELOAD_DIRS``（空白分隔）代替选项。
    """
    if "--reload" not in argv:
        return None
    raw_dirs = _option_values(argv, "--reload-dir")
    env = os.environ if environ is None else environ
    raw_dirs.extend((env.get("UVICORN_RELOAD_DIRS") or "").split())
    if not raw_dirs:
        return [cwd.resolve()]
    return [_resolve(cwd, raw) for raw in raw_dirs]


def reload_watch_hazard(
    argv: Sequence[str],
    cwd: Path,
    workspaces_dir: Path,
    environ: dict[str, str] | None = None,
) -> str | None:
    """沙盒工作区落在热重载监视范围内时给出开发者诊断；不是这种情形返回 None。

    ``--reload-exclude`` 给的是目录且盖住工作区时视为已排除；glob 形式的排除不认——
    uvicorn 的过滤器只用它匹配文件名末段，挡不住多层子目录里的 ``main.py``。
    """
    watched = reload_watch_dirs(argv, cwd, environ)
    if not watched:
        return None
    workspace_root = _resolve(cwd, str(workspaces_dir))
    culprits = [directory for directory in watched if _contains(directory, workspace_root)]
    if not culprits:
        return None
    for raw in _option_values(argv, "--reload-exclude"):
        excluded = _resolve(cwd, raw)
        if excluded.is_dir() and _contains(excluded, workspace_root):
            return None
    watched_text = "、".join(str(path) for path in culprits)
    return (
        f"uvicorn --reload 正在监视 {watched_text}，而沙盒工作区 {workspace_root} 就在这个范围内："
        "实验阶段每次 python_run 写出 steps/*/main.py 都会触发一次重载、把正在执行的阶段打断，"
        "下个进程只能把它判成中断再从头重跑，阶段永远跑不完。请改用 "
        f"`{RECOMMENDED_COMMAND}`（已带 {RECOMMENDED_FLAGS}）启动 API，或在手敲命令里加上这两个参数。"
    )


def current_reload_watch_hazard(workspaces_dir: Path) -> str | None:
    """以本进程真实的 argv / cwd 做诊断（uvicorn reload 子进程继承父进程这两项）。"""
    return reload_watch_hazard(sys.argv, Path.cwd(), workspaces_dir)


def warn_if_reload_watches_sandbox(workspaces_dir: Path, *, log: logging.Logger | None = None) -> bool:
    """API 启动时调用：命中就以 ERROR 级别写一条醒目日志。返回是否命中。"""
    diagnosis = current_reload_watch_hazard(workspaces_dir)
    if diagnosis is None:
        return False
    (log or logger).error(
        "\n%s\n! 热重载配置会让实验阶段无法完成\n! %s\n%s",
        "!" * 78,
        diagnosis,
        "!" * 78,
    )
    return True


__all__ = (
    "RECOMMENDED_COMMAND",
    "RECOMMENDED_FLAGS",
    "current_reload_watch_hazard",
    "reload_watch_dirs",
    "reload_watch_hazard",
    "warn_if_reload_watches_sandbox",
)
