import time

from omm_agent_core import EventType, ToolResult
from omm_agent_tools import RecordingInvoker, ToolRegistry, ToolSpec, summarize


class RecorderSpy:
    def __init__(self):
        self.records = []

    def __call__(self, event_type, payload):
        self.records.append((event_type, payload))
        return None  # invoker ignores the returned event


def echo_spec(**overrides):
    defaults = dict(
        name="echo",
        description="echo arguments back",
        handler=lambda args, ctx: ToolResult(status="succeeded", output=args),
        required_args=("message",),
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


def build(registry):
    recorder = RecorderSpy()
    return RecordingInvoker(registry, recorder), recorder


def test_successful_call_is_recorded_with_summaries():
    registry = ToolRegistry()
    registry.register(echo_spec())
    invoker, recorder = build(registry)

    result = invoker.invoke("run_1", "step_1", "echo", {"message": "hi"})

    assert result.ok
    assert result.output == {"message": "hi"}
    (event_type, payload), = recorder.records
    assert event_type is EventType.TOOL_CALLED
    assert payload["tool"] == "echo"
    assert payload["status"] == "succeeded"
    assert payload["step_id"] == "step_1"
    assert "hi" in payload["input_summary"]
    assert payload["duration_ms"] >= 0


def test_unregistered_tool_fails_and_is_still_recorded():
    invoker, recorder = build(ToolRegistry())

    result = invoker.invoke("run_1", "step_1", "ghost", {})

    assert result.status == "failed"
    assert "not registered" in result.error
    assert recorder.records[0][1]["status"] == "failed"


def test_allowlist_blocks_registered_tools():
    registry = ToolRegistry()
    registry.register(echo_spec())
    restricted = registry.with_allowlist(["something_else"])
    invoker, recorder = build(restricted)

    result = invoker.invoke("run_1", "step_1", "echo", {"message": "hi"})

    assert result.status == "failed"
    assert "allowlist" in result.error


def test_missing_required_args_fail_before_handler_runs():
    calls = []

    def handler(args, ctx):
        calls.append(args)
        return ToolResult(status="succeeded")

    registry = ToolRegistry()
    registry.register(echo_spec(handler=handler))
    invoker, _ = build(registry)

    result = invoker.invoke("run_1", "step_1", "echo", {})

    assert result.status == "failed"
    assert "missing required arguments" in result.error
    assert calls == []


def test_handler_exception_becomes_failed_result():
    def handler(args, ctx):
        raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(echo_spec(handler=handler))
    invoker, recorder = build(registry)

    result = invoker.invoke("run_1", "step_1", "echo", {"message": "x"})

    assert result.status == "failed"
    assert "kaboom" in result.error
    assert recorder.records[0][1]["status"] == "failed"


def test_slow_handler_times_out():
    def handler(args, ctx):
        time.sleep(0.5)
        return ToolResult(status="succeeded")

    registry = ToolRegistry()
    registry.register(echo_spec(handler=handler, timeout_s=0.05))
    invoker, recorder = build(registry)

    result = invoker.invoke("run_1", "step_1", "echo", {"message": "x"})

    assert result.status == "timeout"
    assert recorder.records[0][1]["status"] == "timeout"


def test_summarize_truncates_large_values():
    text = summarize({"blob": "x" * 5000})
    assert len(text) <= 512
    assert "chars)" in text
