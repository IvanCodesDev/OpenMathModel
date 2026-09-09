"""运行记忆化原语（设计 §7.5「本地最大效率四抓手」之三，H7）。

同一份脚本 × 同一批输入数据 × 同一组参数 / 种子 × 同一语言运行时，按 §7.3 可复现性硬要求冷启动
重跑指标应一致——那就没必要再烧一次沙箱：命中直接复用既有 SandboxRunReport（与产物引用）。
这里只做**纯原语**：指纹计算、数据文件哈希、存取端口与两个实现（内存 / 目录文件）。什么时候查、
命中后怎么用（复用报告、事件里标 memo 命中、**复现验收仍以冷启动为准**、用户显式「重跑」时绕过）
归节点侧接线刀，不在这里预设。

指纹口径（缺一维就可能把不同实验当成同一个）：

- ``code``：脚本正文，只做行尾归一与行尾空白剥离（注释 / 变量名的差异照样算不同——
  不做语义等价）；
- ``language`` + ``runtime_version`` + ``deps_hash``：取 env_probe ``languages[lang]`` 三键，
  运行时或依赖清单变了就不命中；
- ``data_hashes``：输入文件 相对路径 → sha256（``data_file_hashes`` 按前缀采集，缺省 data/
  与 cleaned/）；
- ``params``：显式参数 / 种子（任务卡 ``seeds`` 与调用方认为影响结果的一切），canonical JSON。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .workspace import TaskWorkspace

__all__ = [
    "DEFAULT_DATA_PREFIXES",
    "FileRunMemo",
    "InMemoryRunMemo",
    "MemoEntry",
    "RunMemo",
    "data_file_hashes",
    "normalize_code",
    "run_fingerprint",
]

#: 指纹里输入数据的缺省采集范围：任务卡下发的原始数据与清洗产物。
DEFAULT_DATA_PREFIXES: tuple[str, ...] = ("data/", "cleaned/")

#: 指纹格式版本：口径变了就换号，旧条目自然全部不命中（不必清目录）。
_FINGERPRINT_VERSION = 1


def normalize_code(code: str) -> str:
    """行尾归一（CRLF → LF）+ 去每行行尾空白 + 去文末空行：编辑器噪声不该让缓存失效。"""
    unified = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr
    )


def run_fingerprint(
    *,
    code: str,
    language: str,
    runtime_version: str,
    deps_hash: str = "",
    data_hashes: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """(代码, 语言运行时, 输入数据, 参数) → 64 位十六进制指纹。同输入恒同值，任一维变化即不同。"""
    data = sorted(
        (str(path).replace("\\", "/"), str(digest)) for path, digest in (data_hashes or {}).items()
    )
    payload = {
        "v": _FINGERPRINT_VERSION,
        "code_sha256": _sha256(normalize_code(code)),
        "language": str(language or "").strip().lower(),
        "runtime_version": str(runtime_version or ""),
        "deps_hash": str(deps_hash or ""),
        "data": data,
        "params": _canonical(dict(params or {})),
    }
    return _sha256(_canonical(payload))


def data_file_hashes(
    workspace: TaskWorkspace, prefixes: Iterable[str] = DEFAULT_DATA_PREFIXES
) -> dict[str, str]:
    """工作区内给定前缀下每个文件的 sha256（键为工作区相对路径、`/` 分隔、排序）。"""
    wanted = tuple(prefixes)
    hashes: dict[str, str] = {}
    for relative in workspace.list_files():
        if wanted and not any(relative.startswith(prefix) for prefix in wanted):
            continue
        hashes[relative] = hashlib.sha256(workspace.read_bytes(relative)).hexdigest()
    return dict(sorted(hashes.items()))


@dataclass(frozen=True)
class MemoEntry:
    """一次已完成沙盒任务的可复用事实：报告（sandbox-run-report.v1 形状 dict）+ 产物引用 + 来源。"""

    fingerprint: str
    language: str
    report: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...] = ()
    #: 首次落缓存时的来源运行 / 步骤（审计用：命中时事件里能说清「复用的是哪次运行」）。
    source_run_id: str = ""
    source_step_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "language": self.language,
            "report": dict(self.report),
            "artifacts": [dict(item) for item in self.artifacts],
            "source_run_id": self.source_run_id,
            "source_step_id": self.source_step_id,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> MemoEntry:
        return MemoEntry(
            fingerprint=str(raw["fingerprint"]),
            language=str(raw.get("language") or ""),
            report=dict(raw.get("report") or {}),
            artifacts=tuple(dict(item) for item in raw.get("artifacts") or ()),
            source_run_id=str(raw.get("source_run_id") or ""),
            source_step_id=str(raw.get("source_step_id") or ""),
            created_at=float(raw.get("created_at") or 0.0),
        )


class RunMemo(Protocol):
    """存取端口：节点侧按指纹查 / 存；实现可换（内存 / 目录 / 将来的数据库表）。"""

    def get(self, fingerprint: str) -> MemoEntry | None: ...

    def put(self, entry: MemoEntry) -> None: ...


class InMemoryRunMemo:
    """进程内字典实现：测试与单进程装配用。"""

    def __init__(self) -> None:
        self._entries: dict[str, MemoEntry] = {}

    def get(self, fingerprint: str) -> MemoEntry | None:
        return self._entries.get(fingerprint)

    def put(self, entry: MemoEntry) -> None:
        self._entries[entry.fingerprint] = entry

    def __len__(self) -> int:
        return len(self._entries)


class FileRunMemo:
    """目录文件实现：``<directory>/<fingerprint>.json``，一条一文件、原子写、跨运行 / 跨进程可见。

    目录应放在运行工作区**之外**（如 ``<workspaces_dir>/_memo``）：放进工作区会被 ws_list 列给模型、
    也会混进产物扫描。坏文件按未命中处理（记忆化是加速器，不是事实来源，坏了就当没有）。
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, fingerprint: str) -> Path:
        safe = "".join(ch for ch in str(fingerprint) if ch in "0123456789abcdef")
        if len(safe) != 64:
            raise ValueError(f"fingerprint must be a 64-hex sha256, got {fingerprint!r}")
        return self.directory / f"{safe}.json"

    def get(self, fingerprint: str) -> MemoEntry | None:
        path = self._path(fingerprint)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("fingerprint") != fingerprint:
            return None
        try:
            return MemoEntry.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, entry: MemoEntry) -> None:
        path = self._path(entry.fingerprint)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(entry.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)

    def __len__(self) -> int:
        return sum(1 for _ in self.directory.glob("*.json"))

    def clear(self) -> None:
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
