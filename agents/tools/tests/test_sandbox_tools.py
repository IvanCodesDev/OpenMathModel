"""沙盒工作区工具集（§7.1 五件套中的四件）：行为、边界与最小授权标注。"""

from __future__ import annotations

import pytest

from omm_agent_core import ToolResult
from omm_agent_tools import (
    RecordingInvoker,
    TaskWorkspace,
    ToolRegistry,
    env_fingerprint,
    sandbox_workspace_specs,
)


@pytest.fixture()
def workspace(tmp_path) -> TaskWorkspace:
    return TaskWorkspace(tmp_path, "run_sandboxtools")


@pytest.fixture()
def invoker(workspace) -> RecordingInvoker:
    registry = ToolRegistry()
    for spec in sandbox_workspace_specs(workspace):
        registry.register(spec)
    events: list = []

    def record(event_type, payload):
        events.append((event_type, payload))
        return None

    bus = RecordingInvoker(registry, record, caller_max_tier="execute")
    bus.recorded_events = events  # type: ignore[attr-defined]
    return bus


def call(invoker, tool: str, **arguments) -> ToolResult:
    return invoker.invoke("run_sandboxtools", "step_1", tool, arguments)


def test_ws_write_then_read_roundtrip(invoker) -> None:
    written = call(invoker, "ws_write", path="src/main.py", text="print('hi')")
    assert written.ok and written.output["bytes"] == len(b"print('hi')")

    read = call(invoker, "ws_read", path="src/main.py")
    assert read.ok
    assert read.output["text"] == "print('hi')"
    assert read.output["truncated"] is False


def test_ws_list_with_prefix_filter(invoker) -> None:
    call(invoker, "ws_write", path="src/a.py", text="a")
    call(invoker, "ws_write", path="data/b.csv", text="b")

    everything = call(invoker, "ws_list")
    assert everything.output["files"] == ["data/b.csv", "src/a.py"]

    only_src = call(invoker, "ws_list", prefix="src/")
    assert only_src.output["files"] == ["src/a.py"]


def test_path_escape_is_rejected_not_raised(invoker) -> None:
    for tool, arguments in (
        ("ws_read", {"path": "../outside.txt"}),
        ("ws_write", {"path": "../outside.txt", "text": "x"}),
    ):
        result = call(invoker, tool, **arguments)
        assert not result.ok and "escapes workspace" in (result.error or "")


def test_ws_read_missing_file_fails_cleanly(invoker) -> None:
    result = call(invoker, "ws_read", path="nope.txt")
    assert not result.ok and "不存在" in (result.error or "")


def test_ws_read_truncates_oversized_text(invoker, workspace) -> None:
    workspace.write_text("big.txt", "x" * 25_000)
    result = call(invoker, "ws_read", path="big.txt")
    assert result.ok
    assert result.output["truncated"] is True
    assert len(result.output["text"]) == 20_000
    assert result.output["total_chars"] == 25_000


def test_env_probe_reports_reproducibility_fingerprint(invoker) -> None:
    result = call(invoker, "env_probe")
    assert result.ok
    output = result.output
    assert output["runtime"] == "python"
    assert output["version"].count(".") == 2
    assert len(output["deps_hash"]) == 64
    # 指纹是确定性的：同环境两次探测一致
    assert output["deps_hash"] == env_fingerprint()["deps_hash"]


def test_minimal_grant_tiers(workspace) -> None:
    """读者档位（readonly）可用读类工具，但 ws_write 必须被 E240 拒绝。"""
    registry = ToolRegistry()
    for spec in sandbox_workspace_specs(workspace):
        registry.register(spec)
    readonly_bus = RecordingInvoker(registry, lambda *_: None, caller_max_tier="readonly")

    assert readonly_bus.invoke("r", "s", "ws_list", {}).ok
    assert readonly_bus.invoke("r", "s", "env_probe", {}).ok
    denied = readonly_bus.invoke("r", "s", "ws_write", {"path": "a", "text": "b"})
    assert not denied.ok and "[E240]" in (denied.error or "")


def test_every_call_is_audited(invoker) -> None:
    call(invoker, "ws_write", path="a.txt", text="1")
    call(invoker, "ws_read", path="a.txt")
    audited_tools = [payload["tool"] for _type, payload in invoker.recorded_events]
    assert audited_tools == ["ws_write", "ws_read"]
