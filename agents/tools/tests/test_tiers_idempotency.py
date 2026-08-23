"""H0 enhancements (§4.3): permission tiers, idempotency keys, turn parallelism."""

import threading

import pytest
from omm_agent_core import ToolResult
from omm_agent_tools import (
    MAX_TURN_PARALLELISM,
    RecordingInvoker,
    ToolNotAllowed,
    ToolRegistry,
    ToolSpec,
    args_fingerprint,
    execute_parallel,
    tier_rank,
)


class RecorderSpy:
    def __init__(self):
        self.records = []

    def __call__(self, event_type, payload):
        self.records.append((event_type, payload))
        return None


def spec(name="echo", tier="readonly", handler=None, **overrides):
    defaults = dict(
        name=name,
        description="test tool",
        handler=handler or (lambda args, ctx: ToolResult(status="succeeded", output=args)),
        tier=tier,
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


# ── tiers ─────────────────────────────────────────────────────────────────────


def test_tier_order_is_ascending_privilege():
    assert tier_rank("readonly") < tier_rank("workspace_write")
    assert tier_rank("workspace_write") < tier_rank("execute")
    assert tier_rank("execute") < tier_rank("spawn")
    with pytest.raises(ValueError, match="unknown tool tier"):
        tier_rank("root")


def test_registering_unknown_tier_fails_fast():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="unknown tool tier"):
        registry.register(spec(tier="root"))


def test_caller_tier_denies_higher_tier_tool_with_e240():
    executed = []
    registry = ToolRegistry()
    registry.register(
        spec(name="code_run", tier="execute",
             handler=lambda args, ctx: executed.append(args) or ToolResult(status="succeeded"))
    )
    recorder = RecorderSpy()
    invoker = RecordingInvoker(registry, recorder, caller_max_tier="readonly")

    result = invoker.invoke("run_1", "step_1", "code_run", {})

    assert result.status == "failed"
    assert "[E240]" in result.error
    assert executed == []  # the handler must never run on a tier violation
    assert recorder.records[0][1]["status"] == "failed"


def test_caller_tier_allows_equal_and_lower_tiers():
    registry = ToolRegistry()
    registry.register(spec(name="ws_read", tier="readonly"))
    registry.register(spec(name="code_run", tier="execute"))
    invoker = RecordingInvoker(registry, RecorderSpy(), caller_max_tier="execute")

    assert invoker.invoke("run_1", "step_1", "ws_read", {}).ok
    assert invoker.invoke("run_1", "step_1", "code_run", {}).ok


def test_resolve_without_caller_tier_keeps_old_behavior():
    registry = ToolRegistry()
    registry.register(spec(name="code_run", tier="spawn"))
    assert registry.resolve("code_run").tier == "spawn"
    with pytest.raises(ToolNotAllowed, match=r"\[E240\]"):
        registry.resolve("code_run", caller_max_tier="execute")


# ── idempotency ───────────────────────────────────────────────────────────────


def test_replayed_slot_returns_cached_result_without_rerunning():
    executions = []

    def handler(args, ctx):
        executions.append(args)
        return ToolResult(status="succeeded", output={"n": len(executions)})

    registry = ToolRegistry()
    registry.register(spec(handler=handler))
    cache = {}
    recorder = RecorderSpy()

    first_invoker = RecordingInvoker(registry, recorder, idempotency_cache=cache)
    first = first_invoker.invoke("run_1", "step_1", "echo", {"message": "hi"})

    # Crash-recovery replay: a fresh invoker instance, same cache, same order.
    second_invoker = RecordingInvoker(registry, recorder, idempotency_cache=cache)
    second = second_invoker.invoke("run_1", "step_1", "echo", {"message": "hi"})

    assert first.output == {"n": 1}
    assert second.output == {"n": 1}  # cached result, not a second execution
    assert len(executions) == 1
    replay_payload = recorder.records[-1][1]
    assert replay_payload["idempotent_replay"] is True


def test_same_slot_with_different_args_is_e250_conflict():
    executions = []

    def handler(args, ctx):
        executions.append(args)
        return ToolResult(status="succeeded")

    registry = ToolRegistry()
    registry.register(spec(handler=handler))
    cache = {}

    RecordingInvoker(registry, RecorderSpy(), idempotency_cache=cache).invoke(
        "run_1", "step_1", "echo", {"message": "hi"}
    )
    conflict = RecordingInvoker(registry, RecorderSpy(), idempotency_cache=cache).invoke(
        "run_1", "step_1", "echo", {"message": "DIFFERENT"}
    )

    assert conflict.status == "failed"
    assert "[E250]" in conflict.error
    assert len(executions) == 1


def test_args_fingerprint_is_order_insensitive():
    assert args_fingerprint({"a": 1, "b": 2}) == args_fingerprint({"b": 2, "a": 1})
    assert args_fingerprint({"a": 1}) != args_fingerprint({"a": 2})


# ── turn parallelism ──────────────────────────────────────────────────────────


def test_execute_parallel_caps_concurrency_at_two_and_keeps_order():
    active = []
    peak = []
    lock = threading.Lock()
    barrier_release = threading.Event()

    def handler(args, ctx):
        with lock:
            active.append(1)
            peak.append(len(active))
        barrier_release.wait(0.2)
        with lock:
            active.pop()
        return ToolResult(status="succeeded", output={"i": args["i"]})

    registry = ToolRegistry()
    registry.register(spec(handler=handler))
    invoker = RecordingInvoker(registry, RecorderSpy(), idempotency_cache={})

    calls = [("echo", {"i": index}) for index in range(4)]
    results = execute_parallel(invoker, "run_1", "step_1", calls, max_parallel=8)

    assert [r.output["i"] for r in results] == [0, 1, 2, 3]
    assert max(peak) <= MAX_TURN_PARALLELISM  # the ≤2 cap holds even when asked for 8


def test_execute_parallel_assigns_deterministic_indices():
    registry = ToolRegistry()
    registry.register(spec())
    cache = {}
    invoker = RecordingInvoker(registry, RecorderSpy(), idempotency_cache=cache)

    calls = [("echo", {"i": index}) for index in range(3)]
    execute_parallel(invoker, "run_1", "step_1", calls)

    assert sorted(index for (_, index) in cache) == [0, 1, 2]
    # replaying the same three calls hits the cache in order
    replay_invoker = RecordingInvoker(registry, RecorderSpy(), idempotency_cache=cache)
    replayed = execute_parallel(replay_invoker, "run_1", "step_1", calls)
    assert [r.output["i"] for r in replayed] == [0, 1, 2]
