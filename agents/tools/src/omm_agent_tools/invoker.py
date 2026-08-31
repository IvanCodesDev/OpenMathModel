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

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, MutableMapping, Sequence

from omm_agent_core import AgentEvent, EventType, ToolResult

from .registry import ToolCallContext, ToolNotAllowed, ToolRegistry

_SUMMARY_LIMIT = 512

#: Failed calls carry a bounded stderr/stdout tail in the event payload.
#: Without it the audit trail only says "exited with code 1" — the actual
#: crash reason lived solely in the in-memory repair loop and was lost to
#: both the UI trace and post-hoc diagnosis.
_FAILURE_DETAIL_LIMIT = 2000

#: §4.3: at most this many tool calls run concurrently within one turn.
MAX_TURN_PARALLELISM = 2

#: Signature of the engine-provided recording callback.
EventRecorder = Callable[[EventType, dict[str, Any]], AgentEvent]

#: Idempotency storage: (step_id, call_index) -> (args_hash, result). The
#: default is in-memory; a durable mapping can be injected for crash replay.
IdempotencyCache = MutableMapping[tuple[str, int], tuple[str, ToolResult]]


def args_fingerprint(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=repr)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    """Compact, log-safe repr for event payloads (never raw megabytes)."""
    text = repr(value)
    if len(text) <= limit:
        return text
    suffix = f"...(+{len(text) - limit} chars)"
    return text[: max(limit - len(suffix), 0)] + suffix


def failure_detail(result: ToolResult, limit: int = _FAILURE_DETAIL_LIMIT) -> str:
    """Crash evidence for a failed/timeout call: stderr tail, else stdout tail.

    Tail, not head — Python tracebacks put the failing frame and exception
    type at the end. Bounded so a runaway print loop cannot flood the event
    table.
    """
    output = result.output if isinstance(result.output, dict) else {}
    stderr = str(output.get("stderr") or "").strip()
    if stderr:
        return stderr[-limit:]
    stdout = str(output.get("stdout") or "").strip()
    if stdout:
        return stdout[-limit:]
    return ""


class RecordingInvoker:
    """Implements the core ToolInvoker port around a registry and a recorder.

    Optional H0 enhancements (§4.3), both off unless configured:

    - ``caller_max_tier``: minimal-grant enforcement; a tool demanding a
      higher tier fails with an ``[E240]`` result (assembly defect).
    - ``idempotency_cache``: keyed ``(step_id, call_index, args_hash)``.
      Replaying the same call slot with identical arguments returns the
      stored result WITHOUT re-executing the handler (audited with
      ``idempotent_replay``); the same slot with different arguments is an
      ``[E250]`` conflict — a non-deterministic replay must not run tools.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        recorder: EventRecorder,
        *,
        caller_max_tier: str | None = None,
        idempotency_cache: IdempotencyCache | None = None,
    ) -> None:
        self._registry = registry
        self._recorder = recorder
        self._caller_max_tier = caller_max_tier
        self._cache = idempotency_cache
        self._call_indices: dict[str, int] = {}
        self._index_lock = threading.Lock()

    def invoke(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        call_index = self.reserve_call_indices(step_id, 1)
        return self.invoke_indexed(run_id, step_id, tool_name, arguments, call_index)

    def reserve_call_indices(self, step_id: str, count: int) -> int:
        """Reserve ``count`` consecutive call indices; returns the first one."""
        with self._index_lock:
            first = self._call_indices.get(step_id, 0)
            self._call_indices[step_id] = first + count
            return first

    def invoke_indexed(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        call_index: int,
    ) -> ToolResult:
        started = time.monotonic()
        fingerprint = args_fingerprint(arguments)

        if self._cache is not None:
            cached = self._cache.get((step_id, call_index))
            if cached is not None:
                stored_fingerprint, stored_result = cached
                if stored_fingerprint != fingerprint:
                    result = ToolResult(
                        status="failed",
                        error=(
                            f"[E250] idempotency conflict: step {step_id!r} call #{call_index} "
                            f"was recorded with different arguments"
                        ),
                    )
                    self._emit(step_id, tool_name, arguments, result, started)
                    return result
                self._emit(
                    step_id, tool_name, arguments, stored_result, started,
                    idempotent_replay=True,
                )
                return stored_result

        try:
            spec = self._registry.resolve(tool_name, caller_max_tier=self._caller_max_tier)
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
        if self._cache is not None:
            self._cache[(step_id, call_index)] = (fingerprint, result)
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
        *,
        idempotent_replay: bool = False,
    ) -> None:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        payload: dict[str, Any] = {
            "step_id": step_id,
            "tool": tool_name,
            "status": result.status,
            "duration_ms": duration_ms,
            "input_summary": summarize(arguments),
            "output_summary": summarize(
                result.output if result.ok else result.error
            ),
            "artifact_ids": [ref.artifact_id for ref in result.artifacts],
        }
        if not result.ok:
            detail = failure_detail(result)
            if detail:
                payload["failure_detail"] = detail
        if idempotent_replay:
            payload["idempotent_replay"] = True
        self._recorder(EventType.TOOL_CALLED, payload)


def execute_parallel(
    invoker: RecordingInvoker,
    run_id: str,
    step_id: str,
    calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    max_parallel: int = MAX_TURN_PARALLELISM,
) -> list[ToolResult]:
    """Run one turn's tool calls concurrently, capped at 2 (§4.3).

    Call indices are assigned by list position BEFORE execution so the
    idempotency key stays deterministic regardless of thread scheduling.
    Results come back in input order.
    """
    workers = max(1, min(max_parallel, MAX_TURN_PARALLELISM, len(calls) or 1))
    base_index = invoker.reserve_call_indices(step_id, len(calls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                invoker.invoke_indexed,
                run_id,
                step_id,
                tool_name,
                arguments,
                base_index + offset,
            )
            for offset, (tool_name, arguments) in enumerate(calls)
        ]
        return [future.result() for future in futures]
