"""Per-run isolated workspace and filesystem-backed artifact store.

Security invariants (see repo security rules on paths):

- Every resolved path MUST stay inside the workspace root; ``..``, absolute
  paths and drive changes are rejected AFTER normalization, not by string
  matching.
- Writes go through an atomic temp-file + replace so a crash never leaves a
  half-written file that later reads would trust.
- A byte quota bounds total workspace size so a runaway tool cannot fill the
  disk.

The workspace root defaults to ``<repo>/runs/workspaces/<run_id>`` which is
covered by .gitignore (``runs/``): run products never enter git.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from omm_agent_core import ArtifactRef


class WorkspaceViolation(Exception):
    """A path escaped the workspace or exceeded its quota."""


class TaskWorkspace:
    def __init__(
        self,
        root: str | os.PathLike[str],
        run_id: str,
        quota_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.run_id = run_id
        self.root = (Path(root) / run_id).resolve()
        self.quota_bytes = quota_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path safety --------------------------------------------------------

    def resolve(self, relative: str | os.PathLike[str]) -> Path:
        """Resolve a workspace-relative path, refusing escapes."""
        candidate = Path(relative)
        if candidate.is_absolute():
            raise WorkspaceViolation(f"absolute paths are not allowed: {relative!r}")
        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceViolation(f"path escapes workspace: {relative!r}")
        return resolved

    # -- io ------------------------------------------------------------------

    def used_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def write_bytes(self, relative: str, content: bytes) -> Path:
        if self.used_bytes() + len(content) > self.quota_bytes:
            raise WorkspaceViolation(
                f"workspace quota exceeded ({self.quota_bytes} bytes)"
            )
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp"
        temp.write_bytes(content)
        os.replace(temp, target)  # atomic on the same volume
        return target

    def write_text(self, relative: str, content: str, encoding: str = "utf-8") -> Path:
        return self.write_bytes(relative, content.encode(encoding))

    def read_bytes(self, relative: str) -> bytes:
        return self.resolve(relative).read_bytes()

    def read_text(self, relative: str, encoding: str = "utf-8") -> str:
        return self.resolve(relative).read_text(encoding=encoding)

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).exists()
        except WorkspaceViolation:
            return False

    def list_files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.root)).replace("\\", "/")
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class WorkspaceArtifactStore:
    """ArtifactStore port backed by the run workspace.

    Content is addressed under ``artifacts/<artifact_id>/<name>`` inside the
    workspace; the URI is the plain filesystem path for now. When
    packages/contracts formalizes artifact URIs (object storage), this class
    is the single place to adapt.
    """

    def __init__(self, workspace: TaskWorkspace) -> None:
        self._workspace = workspace

    def put(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        producer_step: str,
    ) -> ArtifactRef:
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        safe_name = Path(name).name  # strip any directory part from tool input
        stored = self._workspace.write_bytes(
            f"artifacts/{artifact_id}/{safe_name}", content
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=str(stored),
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type=media_type,
            producer_step=producer_step,
        )

    def import_file(
        self, source: Path, kind: str, media_type: str, producer_step: str
    ) -> ArtifactRef:
        return self.put(
            run_id=self._workspace.run_id,
            kind=kind,
            name=source.name,
            content=source.read_bytes(),
            media_type=media_type,
            producer_step=producer_step,
        )
