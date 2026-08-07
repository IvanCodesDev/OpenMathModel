"""Artifact 二进制内容存储：本地内容寻址实现 + 可替换协议。

- 内容按 sha256 寻址：``data/artifacts/<aa>/<bb>/<sha256>``，天然去重、不可变；
- 原子写：先写临时文件再 rename，崩溃不会留下半个对象；
- MinIO/S3 后续以同一协议接入（B3 底座就绪后），API 层不感知实现。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable


@runtime_checkable
class ArtifactBlobStore(Protocol):
    def put(self, content: bytes) -> tuple[str, int]:
        """存入内容，返回 (sha256, size)。同内容重复写入是幂等的。"""

    def open(self, sha256: str) -> BinaryIO | None:
        """按哈希打开内容；不存在返回 None。"""

    def exists(self, sha256: str) -> bool: ...


class LocalContentStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256[2:4] / sha256

    def put(self, content: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(content).hexdigest()
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        return digest, len(content)

    def open(self, sha256: str) -> BinaryIO | None:
        target = self._path(sha256)
        if not target.exists():
            return None
        return target.open("rb")

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()
