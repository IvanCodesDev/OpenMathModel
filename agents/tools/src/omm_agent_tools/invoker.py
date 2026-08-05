"""Recording tool invoker: every call leaves an auditable event.

Architecture constraint (system-overview §5): each tool call records input
summary, output summary, duration, status and any artifacts. The record is a
TOOL_CALLED event on the run's event log, so UI and audits read tool activity
from the same stream as everything else.

Recording goes through the engine's ``record_external`` (wired in as the
``recorder`` callable) rather than straight to the sink: sequence allocation
and snapshot application must stay on one path, otherwise the engine would
reuse sequence numbers after a tool call.

Timeout semantics, honestly stated: in-process handlers run on a daemon
thread that is ABANDONED on timeout (Python cannot safely kill a thread); a
hung handler therefore leaks a daemon thread but the caller regains control.
Hard kills are only real for subprocess-backed tools (see python_runner).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from omm_agent_core import AgentEvent, EventType, ToolResult

from .registry import ToolCallContext, ToolNotAllowed, ToolRegistry

_SUMMARY_LIMIT = 512

#: Signature of the engine-provided recording callback.
EventRecorder = Callable[[EventType, dict[str, Any]], AgentEvent]


def summarize(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    """Compact, log-safe repr for event payloads (never raw megabytes)."""
    text = repr(value)
    if len(text) <= limit:
        return text
    suffix = f"...(+{len(text) - limit} chars)"
    return text[: max(limit - len(suffix), 0)] + suffix


class RecordingInvoker:
    """Implements the core ToolInvoker port around a registry and a recorder."""

    def __init__(self, registry: ToolRegistry, recorder: EventRecorder) -> None:
        self._registry = registry
        self._recorder = recorder

    def invoke(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        started = time.monotonic()
        try:
            spec = self._registry.resolve(tool_name)
        except ToolNotAllowed as exc:
            result = ToolResult(status="failed", error=str(exc))
            self._emit(step_id, tool_name, arguments, result, started)
            return result

        problem = self._registry.validate_args(spec, arguments)
        if problem is not None:
            result = ToolResult(status="failed", error=problem)
            self._emit(step_id, tool_name, arguments, result, started)
            return result

        ctx = ToolCallContext(run_id=run_id, step_id=step_id, tool_name=tool_name)
        result = self._run_with_timeout(spec, arguments, ctx)
        self._emit(step_id, tool_name, arguments, result, started)
        return result

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _run_with_timeout(spec, arguments, ctx) -> ToolResult:
        outcome: dict[str, ToolResult] = {}

        def target() -> None:
            try:
                outcome["result"] = spec.handler(dict(arguments), ctx)
            except Exception as exc:  # noqa: BLE001 - tool bugs become failed results
                outcome["result"] = ToolResult(
                    status="failed", error=f"{type(exc).__name__}: {exc}"
                )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(spec.timeout_s)
        if thread.is_alive():
            return ToolResult(
                status="timeout",
                error=f"tool {spec.name!r} exceeded {spec.timeout_s}s (handler abandoned)",
            )
        return outcome.get(
            "result", ToolResult(status="failed", error="tool produced no result")
        )

    def _emit(
        self,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        started_monotonic: float,
    ) -> None:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        self._recorder(
            EventType.TOOL_CALLED,
            {
                "step_id": step_id,
                "tool": tool_name,
                "status": result.status,
                "duration_ms": duration_ms,
                "input_summary": summarize(arguments),
                "output_summary": summarize(
                    result.output if result.ok else result.error
                ),
                "artifact_ids": [ref.artifact_id for ref in result.artifacts],
            },
        )
