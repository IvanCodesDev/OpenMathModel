"""执行代码身份：每个步骤是哪版代码跑的，执行进程是不是在跑旧代码。

控制面 API 与 worker 执行面都是长驻进程、都不热重载（长任务经不起重载打断）：改了智能体代码却没把
进程整个重启，真跑用的就一直是启动时的旧代码，结果却容易被算到新代码头上（2026-10-03 排查：e898
跑在 v3.60 之前起的 API 进程上，扁平信封被解析成空参数、复跑核对没豁免耗时类指标，看上去像修复
没生效）。

``ProcessCode`` 在进程启动时给本进程执行的源码拍一张快照：调用方给定的各包 ``*.py`` 加提示词模板
（注册表同样是进程内缓存），记下逐文件内容摘要、汇总指纹、git HEAD 与相对 HEAD 有改动的文件。之后：

- 每开一个步骤，``stamp()`` 随 STEP_STARTED 落进持久化的事件（``executor`` 键），E6 看板据此列出
  一条运行跑过哪几版代码；
- 快照之后磁盘上的源码又改过 = 本进程在跑旧代码：步骤照常执行，盖章带上 ``stale``，日志提醒整个
  重启（同一组改动只提醒一次）；``summary()`` 是不带路径的公开摘要。

各执行面只定自己的口径（快照哪些包、告警里怎么称呼进程、怎么重启）与进程级单例：API 见
``omm_api.code_identity``，worker 见 ``omm_worker.code_identity``。两边用同一份实现，同一份代码
算出的指纹一致。只读文件，不改任何状态；拍照或检查失败只让盖章缺项，绝不影响推进。
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("omm.code_identity")

#: 盖章与日志里点名的文件数上限；改动更多时另给总数。
LISTED_FILES = 8

#: 两次旧代码检查的最短间隔（秒）：模拟链一个 tick 一步，不必每步都扫一遍磁盘。
STALE_CHECK_INTERVAL_S = 5.0

_GIT_TIMEOUT_S = 10.0
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class SourceRoot:
    """一处源码：``directory`` 下文件名匹配 ``pattern`` 的文件（``recursive`` 时含子目录）。"""

    directory: Path
    pattern: str
    recursive: bool = True

    def contains(self, path: Path) -> bool:
        if not fnmatch.fnmatch(path.name, self.pattern):
            return False
        if self.recursive:
            return self.directory in path.parents
        return path.parent == self.directory

    def files(self) -> Iterable[Path]:
        if not self.directory.is_dir():
            return ()
        found = (
            self.directory.rglob(self.pattern)
            if self.recursive
            else self.directory.glob(self.pattern)
        )
        return (path for path in found if path.is_file())


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int
    digest: str


@dataclass(frozen=True)
class CodeSnapshot:
    """进程启动时的源码快照；``files`` 以展示路径为键（仓库内即仓库相对路径）。"""

    roots: tuple[SourceRoot, ...]
    repo_root: Path | None
    files: Mapping[str, FileState]
    fingerprint: str
    taken_at: datetime
    pid: int
    git_head: str | None = None
    git_branch: str | None = None
    #: 相对 HEAD 有改动（含未跟踪的新文件与已删除的文件）的源码；None = 没查到（无 git / 命令失败）
    dirty: tuple[str, ...] | None = None

    @property
    def short_head(self) -> str | None:
        return self.git_head[:7] if self.git_head else None


def default_roots(
    packages: Sequence[str], prompts_dir: Path | None = None
) -> tuple[SourceRoot, ...]:
    """本进程执行的源码位置：``packages``（import 名）各包目录下全部 ``*.py``，再加提示词模板目录
    本层的 ``*.prompt.md``。按 import 名解析位置：开发态 editable 安装与打包部署都适用，解析不到的
    包跳过。"""
    roots: list[SourceRoot] = []
    for name in packages:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        locations = list(spec.submodule_search_locations or []) if spec is not None else []
        if locations:
            roots.append(SourceRoot(Path(locations[0]).resolve(), "*.py"))
    if prompts_dir is not None:
        roots.append(SourceRoot(Path(prompts_dir).resolve(), "*.prompt.md", recursive=False))
    return tuple(roots)


def find_repo_root(roots: Sequence[SourceRoot]) -> Path | None:
    """源码所在的 git 工作树根（最近一个含 ``.git`` 目录或文件的祖先）；不在仓库里返回 None。"""
    for root in roots:
        for candidate in (root.directory, *root.directory.parents):
            if (candidate / ".git").exists():
                return candidate
    return None


def _display(path: Path, roots: Sequence[SourceRoot], repo_root: Path | None) -> str:
    if repo_root is not None:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            pass
    for root in roots:
        try:
            return f"{root.directory.name}/{path.relative_to(root.directory).as_posix()}"
        except ValueError:
            continue
    return path.as_posix()


def _scan(
    roots: Sequence[SourceRoot], repo_root: Path | None
) -> dict[str, tuple[Path, os.stat_result]]:
    found: dict[str, tuple[Path, os.stat_result]] = {}
    for root in roots:
        for path in root.files():
            try:
                stat = path.stat()
            except OSError:
                continue
            found.setdefault(_display(path, roots, repo_root), (path, stat))
    return found


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(files: Mapping[str, FileState]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(f"{name}\0{files[name].digest}\n".encode())
    return digest.hexdigest()[:12]


# -- git：直接读 .git，只有「相对 HEAD 的改动」才起一次子进程 ------------------------------------


def _is_sha(text: str) -> bool:
    return len(text) == 40 and set(text) <= _HEX


def _git_dir(repo_root: Path) -> Path | None:
    """``.git`` 是目录就是它；是文件（linked worktree）就按 ``gitdir:`` 的指向。"""
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    return target if target.is_absolute() else (repo_root / target).resolve()


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """loose ref 优先，其次 packed-refs；linked worktree 的分支在 ``commondir`` 指向的主库里。"""
    directories = [git_dir]
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        common = ""
    if common:
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
        directories.append(common_dir)
    for directory in directories:
        try:
            value = (directory / ref).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if _is_sha(value):
            return value
        try:
            lines = (directory / "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _is_sha(sha):
                return sha
    return None


def read_git_head(repo_root: Path) -> tuple[str | None, str | None]:
    """（HEAD 提交，分支名）；分离 HEAD 时分支为 None，读不到时两者都是 None。"""
    git_dir = _git_dir(repo_root)
    if git_dir is None:
        return None, None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        return _resolve_ref(git_dir, ref), branch
    return (head if _is_sha(head) else None), None


def git_dirty_files(repo_root: Path, roots: Sequence[SourceRoot]) -> tuple[str, ...] | None:
    """相对 HEAD 有改动的源码文件（含未跟踪的新文件与已删除的文件），仓库相对路径。

    ``--no-optional-locks``：只读查询，不顺手刷新索引、不抢 index.lock（开发者可能同时在用
    git）。git 不在、超时或报错一律返回 None（未知），不当作「没有改动」。
    """
    pathspecs: list[str] = []
    for root in roots:
        try:
            pathspecs.append(root.directory.relative_to(repo_root).as_posix())
        except ValueError:
            continue
    if not pathspecs:
        return None
    command = [
        "git", "--no-optional-locks", "-C", str(repo_root), "status",
        "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    tokens = completed.stdout.decode("utf-8", errors="replace").split("\0")
    dirty: set[str] = set()
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        status, relative = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # 重命名 / 复制：紧跟的一段是原路径
        if any(root.contains(repo_root / relative) for root in roots):
            dirty.add(relative)
    return tuple(sorted(dirty))


# -- 快照与比对 ----------------------------------------------------------------------------------


def take_snapshot(roots: Sequence[SourceRoot], *, with_git: bool = True) -> CodeSnapshot:
    resolved = tuple(roots)
    repo_root = find_repo_root(resolved)
    files: dict[str, FileState] = {}
    for name, (path, stat) in _scan(resolved, repo_root).items():
        try:
            files[name] = FileState(stat.st_mtime_ns, stat.st_size, _digest(path))
        except OSError:
            continue
    head: str | None = None
    branch: str | None = None
    dirty: tuple[str, ...] | None = None
    if repo_root is not None and with_git:
        head, branch = read_git_head(repo_root)
        dirty = git_dirty_files(repo_root, resolved)
    return CodeSnapshot(
        roots=resolved,
        repo_root=repo_root,
        files=files,
        fingerprint=_fingerprint(files),
        taken_at=datetime.now(UTC),
        pid=os.getpid(),
        git_head=head,
        git_branch=branch,
        dirty=dirty,
    )


def changed_files(snapshot: CodeSnapshot) -> list[str]:
    """快照之后磁盘上改过的源码文件（新增 / 删除 / 内容变了），排好序的展示路径。

    先比修改时间与大小，对不上再比内容摘要：切分支再切回、编辑器原样保存这类只动了修改时间
    的情形不算改动。
    """
    current = _scan(snapshot.roots, snapshot.repo_root)
    changed: list[str] = []
    for name, (path, stat) in current.items():
        before = snapshot.files.get(name)
        if before is None:
            changed.append(name)
            continue
        if (stat.st_mtime_ns, stat.st_size) == (before.mtime_ns, before.size):
            continue
        try:
            if _digest(path) != before.digest:
                changed.append(name)
        except OSError:
            changed.append(name)
    changed.extend(name for name in snapshot.files if name not in current)
    return sorted(changed)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _listed(names: Sequence[str]) -> str:
    text = "、".join(names[:LISTED_FILES])
    return f"{text} 等 {len(names)} 个" if len(names) > LISTED_FILES else text


class ProcessCode:
    """一个进程的代码身份：快照只拍一次，旧代码检查按 ``STALE_CHECK_INTERVAL_S`` 节流。

    ``roots`` 缺省时按 ``packages`` 与 ``prompts_dir`` 现解析（``default_roots``）；
    ``process_label`` / ``restart_hint`` 是旧代码告警里对这个进程的称呼与重启办法。
    """

    def __init__(
        self,
        roots: Sequence[SourceRoot] | None = None,
        *,
        packages: Sequence[str] = (),
        prompts_dir: Path | None = None,
        process_label: str = "执行进程",
        restart_hint: str = "把这个进程整个停掉再起。",
        with_git: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._roots = tuple(roots) if roots is not None else None
        self._packages = tuple(packages)
        self._prompts_dir = prompts_dir
        self._process_label = process_label
        self._restart_hint = restart_hint
        self._with_git = with_git
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: CodeSnapshot | None = None
        self._failed = False
        self._stale: list[str] = []
        self._checked_at: float | None = None
        self._warned: set[tuple[str, ...]] = set()

    def snapshot(self) -> CodeSnapshot | None:
        """本进程的源码快照；第一次调用时拍（各执行面在进程启动时先调一次）。"""
        with self._lock:
            if self._snapshot is None and not self._failed:
                try:
                    roots = (
                        self._roots
                        if self._roots is not None
                        else default_roots(self._packages, self._prompts_dir)
                    )
                    self._snapshot = take_snapshot(roots, with_git=self._with_git)
                except Exception:
                    logger.exception("执行代码快照失败：之后的步骤不带代码身份")
                    self._failed = True
            return self._snapshot

    def stale_files(self) -> list[str]:
        """快照之后改过的源码文件；空 = 本进程跑的就是磁盘上的代码。"""
        snapshot = self.snapshot()
        if snapshot is None:
            return []
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= STALE_CHECK_INTERVAL_S:
                try:
                    self._stale = changed_files(snapshot)
                except Exception:
                    logger.exception("旧代码检查失败")
                    self._stale = []
                self._checked_at = now
            return list(self._stale)

    def stamp(self, *, run_id: str | None = None, stage: str | None = None) -> dict[str, Any]:
        """STEP_STARTED 的 ``executor`` 盖章；拍不到快照返回空字典（不盖）。

        ``code`` 启动时源码的内容指纹；``git`` 启动时的 HEAD / 分支 / 相对 HEAD 改动；
        ``pid`` 与 ``started_at`` 认进程；``stale`` 只在快照之后磁盘源码又改过时出现。
        """
        snapshot = self.snapshot()
        if snapshot is None:
            return {}
        stamp: dict[str, Any] = {
            "code": snapshot.fingerprint,
            "pid": snapshot.pid,
            "started_at": _iso(snapshot.taken_at),
        }
        if snapshot.git_head is not None:
            git: dict[str, Any] = {"head": snapshot.short_head}
            if snapshot.git_branch:
                git["branch"] = snapshot.git_branch
            if snapshot.dirty is not None:
                git["dirty_count"] = len(snapshot.dirty)
                if snapshot.dirty:
                    git["dirty"] = list(snapshot.dirty[:LISTED_FILES])
            stamp["git"] = git
        stale = self.stale_files()
        if stale:
            stamp["stale"] = {"count": len(stale), "files": stale[:LISTED_FILES]}
            self._warn_stale(snapshot, stale, run_id=run_id, stage=stage)
        return stamp

    def summary(self) -> dict[str, Any]:
        """公开摘要：指纹、提交与计数，不含任何路径（API 的 ``/api/system`` 用它）。"""
        snapshot = self.snapshot()
        if snapshot is None:
            return {"available": False}
        stale = self.stale_files()
        return {
            "available": True,
            "code": snapshot.fingerprint,
            "git": snapshot.short_head,
            "dirty": None if snapshot.dirty is None else len(snapshot.dirty),
            "started_at": _iso(snapshot.taken_at),
            "stale": bool(stale),
            "stale_files": len(stale),
        }

    def _warn_stale(
        self,
        snapshot: CodeSnapshot,
        stale: list[str],
        *,
        run_id: str | None,
        stage: str | None,
    ) -> None:
        key = tuple(stale)
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        where = f"run {run_id}「{stage or '?'}」" if run_id else "本进程"
        started = snapshot.taken_at.astimezone().strftime("%m-%d %H:%M:%S")
        logger.warning(
            "\n%s\n! %s：%s（PID %d，%s 启动）之后，磁盘上的源码改过：%s\n"
            "! 本进程没有热重载，这一步仍按启动时的旧代码执行，结果不代表磁盘上的最新代码。\n"
            "! 要让真跑用上新代码：%s\n%s",
            "!" * 78,
            where,
            self._process_label,
            snapshot.pid,
            started,
            _listed(stale),
            self._restart_hint,
            "!" * 78,
        )


__all__ = (
    "LISTED_FILES",
    "STALE_CHECK_INTERVAL_S",
    "CodeSnapshot",
    "FileState",
    "ProcessCode",
    "SourceRoot",
    "changed_files",
    "default_roots",
    "find_repo_root",
    "git_dirty_files",
    "logger",
    "read_git_head",
    "take_snapshot",
)
