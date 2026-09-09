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

Since H7 slice 1 the execution core (script staging, snapshot, subprocess,
timeout kill, output clipping, artifact capture) lives in ``runners.py`` and
is shared by every language; this module keeps the ``python_run`` tool as a
thin shell over the Python :class:`~omm_agent_tools.runners.LanguageSpec` so
its behaviour, messages and event payloads stay byte-identical (budget ledger
and evals count on the ``python_run`` name). ``code_run`` (``code_run.py``) is
the language-dispatching entry that will supersede it once assembly points
migrate.

Honest boundary (documented, not hidden): this is process-level isolation
only. Network access and grandchild processes are NOT blocked here — that
level of sandboxing (job objects / containers) belongs to the infra phase and
must not be silently assumed by callers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from omm_agent_core import ToolResult
from omm_agent_core.ports import ArtifactStore

from .registry import ToolCallContext, ToolSpec
from .runners import (
    ENV_ALLOWLIST,
    KIND_BY_SUFFIX,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    OUTPUT_LIMIT,
    PYTHON_SPEC,
    SubprocessRunner,
    artifact_kind,
    clip_output,
    runner_env,
)
from .workspace import TaskWorkspace

#: Kept under their historical names: the constants moved to runners.py (single
#: source for every language) but callers / docs still refer to them here.
_ENV_ALLOWLIST = ENV_ALLOWLIST
_OUTPUT_LIMIT = OUTPUT_LIMIT
_MAX_ARTIFACTS = MAX_ARTIFACTS
_MAX_ARTIFACT_BYTES = MAX_ARTIFACT_BYTES
_KIND_BY_SUFFIX = KIND_BY_SUFFIX

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
    """The scrubbed environment every Python sandbox subprocess (and probe) gets."""
    return runner_env(PYTHON_SPEC)


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


def _artifact_kind(name: str) -> str:
    return artifact_kind(name)


def _clip(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    return clip_output(text, limit)


class PythonSandbox:
    """``python_run``: the Python-only tool, now a shell over the shared runner core.

    Kept as its own tool (name, spec, output shape, messages unchanged) so the
    budget ledger, evals golden traces and the assembly points that still
    register it keep working; ``code_run`` is the multi-language entry.
    """

    TOOL_NAME = "python_run"

    def __init__(
        self,
        workspace: TaskWorkspace,
        python_executable: str | None = None,
        timeout_s: float = 60.0,
        store: ArtifactStore | None = None,
    ) -> None:
        self._workspace = workspace
        self._python = python_executable or sys.executable
        self.timeout_s = timeout_s
        # Injectable so an embedding runtime (API/worker) can capture created
        # files straight into its durable artifact store; the workspace-local
        # store remains the zero-infrastructure default (runner falls back to it).
        self._runner = SubprocessRunner(
            PYTHON_SPEC,
            workspace,
            executable=self._python,
            timeout_s=timeout_s,
            store=store,
        )

    @property
    def runner(self) -> SubprocessRunner:
        return self._runner

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
        return self._runner.run(code, ctx, timeout_s)
