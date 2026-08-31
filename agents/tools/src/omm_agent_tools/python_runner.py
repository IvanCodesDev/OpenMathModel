"""Python subprocess sandbox tool.

Executes model/experiment code inside the run workspace with:

- an isolated interpreter (``-I``: no user site, no PYTHON* env influence),
- a scrubbed environment (small Windows-safe allowlist; no user secrets),
- a hard wall-clock timeout with process kill,
- capped stdout/stderr capture,
- automatic artifact capture of files the code creates in the workspace.

The subprocess runs with cwd at the WORKSPACE ROOT, not the per-step script
directory: every other surface the model sees (ws_list/ws_read/ws_write,
staged ``data/`` files, ``cleaned/`` outputs, sandbox assertions) speaks
workspace-relative paths, so relative paths inside generated code must
resolve against the same root or the model's ``open('data/x.csv')`` would
dangle while ws_list happily shows the file.

Honest boundary (documented, not hidden): this is process-level isolation
only. Network access and grandchild processes are NOT blocked here — that
level of sandboxing (job objects / containers) belongs to the infra phase and
must not be silently assumed by callers.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from omm_agent_core import ArtifactRef, ToolResult
from omm_agent_core.ports import ArtifactStore

from .registry import ToolCallContext, ToolSpec
from .workspace import TaskWorkspace, WorkspaceArtifactStore

_ENV_ALLOWLIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    # POSIX minimum for the same code path on CI/containers.
    "LANG",
    "LC_ALL",
)

_OUTPUT_LIMIT = 32 * 1024
_MAX_ARTIFACTS = 16
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

#: GPU probe budget. Generous because a cold torch import plus CUDA driver
#: init can take tens of seconds on Windows; callers cache the result so the
#: cost is paid once per process.
_GPU_PROBE_TIMEOUT_S = 60.0

#: Executed via ``python -I -c`` under the exact sandbox conditions. Prints a
#: single JSON line; ``{"cuda": false}`` covers torch missing, a CPU-only
#: torch build, and no usable device alike — callers need no distinction.
_GPU_PROBE_SCRIPT = """\
import json
info = {"cuda": False}
try:
    import torch
    if torch.cuda.is_available() and torch.cuda.device_count() > 0:
        props = torch.cuda.get_device_properties(0)
        info = {
            "cuda": True,
            "name": torch.cuda.get_device_name(0),
            "vram_gb": round(props.total_memory / 1024 ** 3, 1),
        }
except Exception:
    pass
print(json.dumps(info, ensure_ascii=False))
"""


def _sandbox_env() -> dict[str, str]:
    """The scrubbed environment every sandbox subprocess (and probe) gets."""
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def parse_gpu_probe_output(stdout: str) -> str | None:
    """Extract a GPU descriptor from probe stdout, or None for CPU-only.

    Scans from the last line backwards because stray package warnings may
    precede the probe's own JSON line.
    """
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(info, dict):
            continue
        if info.get("cuda") is not True:
            return None
        name = str(info.get("name") or "").strip() or "CUDA GPU"
        vram_gb = info.get("vram_gb")
        if isinstance(vram_gb, (int, float)) and vram_gb > 0:
            return f"{name}, {vram_gb} GB VRAM"
        return name
    return None


def probe_sandbox_gpu(
    python_executable: str | None = None,
    timeout_s: float = _GPU_PROBE_TIMEOUT_S,
) -> str | None:
    """Report the CUDA GPU usable from sandbox code, or None for CPU-only.

    Probes with the exact conditions ``python_run`` uses — same interpreter,
    ``-I`` isolation, scrubbed environment — because that is the only honest
    answer to "will generated GPU code actually run here?" (e.g. torch
    installed only in user site-packages imports fine in the parent process
    but not under ``-I``). Every failure mode — no torch, CPU-only build,
    driver trouble, timeout — degrades to None so callers fall back to CPU
    wording instead of steering code onto hardware that is not there.
    """
    try:
        proc = subprocess.run(
            [python_executable or sys.executable, "-I", "-c", _GPU_PROBE_SCRIPT],
            env=_sandbox_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return parse_gpu_probe_output(proc.stdout)

#: Artifact kind by file suffix, using the packages/contracts vocabulary
#: (artifact.schema.json kind enum) so captured files project onto the v1
#: contract without a translation layer downstream.
_KIND_BY_SUFFIX = {
    ".svg": "figure",
    ".png": "figure",
    ".jpg": "figure",
    ".jpeg": "figure",
    ".gif": "figure",
    ".csv": "table",
    ".tsv": "table",
    ".py": "code",
    ".log": "log",
    ".txt": "log",
    ".json": "dataset",
}


def _artifact_kind(name: str) -> str:
    return _KIND_BY_SUFFIX.get(os.path.splitext(name)[1].lower(), "other")


def _clip(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...(+{len(text) - limit} chars truncated)"
    return text


class PythonSandbox:
    TOOL_NAME = "python_run"

    def __init__(
        self,
        workspace: TaskWorkspace,
        python_executable: str | None = None,
        timeout_s: float = 60.0,
        store: ArtifactStore | None = None,
    ) -> None:
        self._workspace = workspace
        # Injectable so an embedding runtime (API/worker) can capture created
        # files straight into its durable artifact store; the workspace-local
        # store remains the zero-infrastructure default.
        self._store = store or WorkspaceArtifactStore(workspace)
        self._python = python_executable or sys.executable
        self.timeout_s = timeout_s

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.TOOL_NAME,
            description="Run Python code inside the isolated run workspace.",
            handler=self._handle,
            risk="high",
            # The invoker's thread-join guard is a backstop; the real kill
            # happens in-process below, so give it headroom to fire first.
            timeout_s=self.timeout_s + 10.0,
            required_args=("code",),
            tier="execute",
        )

    # -- handler ---------------------------------------------------------

    def _handle(self, arguments: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        code = arguments["code"]
        if not isinstance(code, str) or not code.strip():
            return ToolResult(status="failed", error="'code' must be a non-empty string")
        timeout_s = min(float(arguments.get("timeout_s", self.timeout_s)), self.timeout_s)

        step_dir_rel = f"steps/{ctx.step_id}"
        script_path = self._workspace.write_text(f"{step_dir_rel}/main.py", code)

        # Snapshot AFTER writing main.py so the script itself is never
        # captured as an artifact; scan the whole workspace because code runs
        # at the root and may legitimately create files anywhere inside it.
        scan_root = self._workspace.root
        before = {path for path in scan_root.rglob("*") if path.is_file()}

        env = _sandbox_env()

        try:
            proc = subprocess.run(
                [self._python, "-I", str(script_path)],
                cwd=str(scan_root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return ToolResult(
                status="timeout",
                error=f"python run exceeded {timeout_s}s and was killed",
                output={"stdout": _clip(stdout), "stderr": _clip(stderr)},
            )

        artifacts, skipped = self._collect_artifacts(scan_root, before, ctx)
        output = {
            "exit_code": proc.returncode,
            "stdout": _clip(proc.stdout),
            "stderr": _clip(proc.stderr),
            "files": [ref.uri for ref in artifacts],
        }
        if skipped:
            output["skipped_files"] = skipped

        if proc.returncode != 0:
            return ToolResult(
                status="failed",
                error=f"python exited with code {proc.returncode}",
                output=output,
                artifacts=tuple(artifacts),
            )
        return ToolResult(status="succeeded", output=output, artifacts=tuple(artifacts))

    def _collect_artifacts(
        self, scan_root: Path, before: set[Path], ctx: ToolCallContext
    ) -> tuple[list[ArtifactRef], list[str]]:
        artifacts: list[ArtifactRef] = []
        skipped: list[str] = []
        created = sorted(
            path
            for path in scan_root.rglob("*")
            if path.is_file() and path not in before
        )
        for path in created:
            if len(artifacts) >= _MAX_ARTIFACTS:
                skipped.append(f"{path.name} (artifact limit {_MAX_ARTIFACTS})")
                continue
            size = path.stat().st_size
            if size > _MAX_ARTIFACT_BYTES:
                skipped.append(f"{path.name} ({size} bytes over limit)")
                continue
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            artifacts.append(
                self._store.put(
                    run_id=self._workspace.run_id,
                    kind=_artifact_kind(path.name),
                    name=path.name,
                    content=path.read_bytes(),
                    media_type=media_type,
                    producer_step=ctx.step_id,
                )
            )
        return artifacts, skipped
