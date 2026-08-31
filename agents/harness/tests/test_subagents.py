"""SubagentSupervisor（§4.8/§8.3）：协议校验、深度=1、并发闸、收割与审计。

E4 层：Spawn/Envelope 违约拒绝、超时收割、深度=1 全部逐条锚定；
并发上限用探针 runner 记录同时在跑的峰值。
"""

from __future__ import annotations

import threading
import time

import pytest

from omm_agent_core.errors import AgentError
from omm_agent_harness import (
    CONTEXT_SLICE_MAX_CHARS,
    ResultEnvelope,
    RunBudget,
    SpawnSpec,
    SubagentSupervisor,
    Usage,
)


def make_spec(**overrides) -> SpawnSpec:
    defaults = dict(
        kind="sandbox",
        goal="构造数据画像",
        context_slice={"profile_target": "orders.csv"},
        toolset=("ws_list", "python_run"),
        tool_tier="execute",
        budgets=RunBudget(max_wall_clock_s=5.0),
        output_schema_id="sandbox-run-report.v1",
    )
    defaults.update(overrides)
    return SpawnSpec(**defaults)


def done_runner(spec: SpawnSpec) -> ResultEnvelope:
    return ResultEnvelope(status="done", output={"ok": True}, usage=Usage(10, 5, 3))


# -- happy path 与审计 -----------------------------------------------------------


def test_spawn_returns_envelope_and_audits_both_phases() -> None:
    events: list[dict] = []
    supervisor = SubagentSupervisor(audit=events.append)
    envelope = supervisor.spawn(make_spec(), done_runner, parent_tier="execute")

    assert envelope.ok and envelope.output == {"ok": True}
    assert [e["phase"] for e in events] == ["spawn", "result"]
    assert events[0]["tool"] == "subagent:sandbox"
    assert events[1]["envelope_status"] == "done"
    assert events[1]["prompt_tokens"] == 10


# -- E510：SpawnSpec 违约（缺陷 fail fast） ---------------------------------------


@pytest.mark.parametrize(
    "overrides, hint",
    [
        ({"kind": "hacker"}, "角色目录"),
        ({"kind": "proposer:"}, "角色目录"),
        ({"goal": "  "}, "goal"),
        ({"output_schema_id": ""}, "output_schema_id"),
    ],
)
def test_invalid_spec_raises_e510(overrides, hint) -> None:
    supervisor = SubagentSupervisor()
    with pytest.raises(AgentError) as excinfo:
        supervisor.spawn(make_spec(**overrides), done_runner, parent_tier="spawn")
    assert excinfo.value.code.value == "E510"
    assert hint in str(excinfo.value)


def test_oversized_context_slice_rejected_as_transcript_smell() -> None:
    supervisor = SubagentSupervisor()
    huge = {"conversation": "x" * (CONTEXT_SLICE_MAX_CHARS + 1)}
    with pytest.raises(AgentError) as excinfo:
        supervisor.spawn(make_spec(context_slice=huge), done_runner, parent_tier="spawn")
    assert excinfo.value.code.value == "E510"
    assert "转录" in str(excinfo.value)


def test_child_tier_must_not_exceed_parent() -> None:
    supervisor = SubagentSupervisor()
    with pytest.raises(AgentError) as excinfo:
        supervisor.spawn(
            make_spec(tool_tier="execute"), done_runner, parent_tier="readonly"
        )
    assert excinfo.value.code.value == "E510"
    assert "子 ≤ 父" in str(excinfo.value)


# -- E540：深度=1 ---------------------------------------------------------------


def test_subagent_cannot_spawn_again() -> None:
    supervisor = SubagentSupervisor()
    with pytest.raises(AgentError) as excinfo:
        supervisor.spawn(make_spec(), done_runner, parent_tier="spawn", caller_depth=1)
    assert excinfo.value.code.value == "E540"


# -- E530：超时收割 --------------------------------------------------------------


def test_timeout_reaps_to_envelope_not_exception() -> None:
    def hanging(_spec: SpawnSpec) -> ResultEnvelope:
        time.sleep(5)
        return ResultEnvelope(status="done")

    supervisor = SubagentSupervisor()
    spec = make_spec(budgets=RunBudget(max_wall_clock_s=0.1))
    envelope = supervisor.spawn(spec, hanging, parent_tier="execute")
    assert envelope.status == "timeout"
    assert envelope.error_code == "E530"


def test_infinite_wall_clock_means_no_timeout_not_overflow() -> None:
    """控制面墙钟未启用（inf）时：join 按无超时等待而非 OverflowError。"""
    supervisor = SubagentSupervisor()
    spec = make_spec(budgets=RunBudget(max_wall_clock_s=float("inf")))
    envelope = supervisor.spawn(spec, done_runner, parent_tier="execute")
    assert envelope.ok and envelope.output == {"ok": True}


# -- E520：Envelope 输出违约 ------------------------------------------------------


def test_invalid_output_is_withheld_with_e520() -> None:
    supervisor = SubagentSupervisor()
    envelope = supervisor.spawn(
        make_spec(),
        done_runner,
        parent_tier="execute",
        output_validator=lambda output: ["missing required key: report"],
    )
    assert envelope.status == "failed"
    assert envelope.error_code == "E520"
    assert envelope.output is None, "违约输出不得下发给父节点"
    assert envelope.usage.prompt_tokens == 10, "用量事实保留"


def test_valid_output_passes_validator_untouched() -> None:
    supervisor = SubagentSupervisor()
    envelope = supervisor.spawn(
        make_spec(), done_runner, parent_tier="execute", output_validator=lambda _o: []
    )
    assert envelope.ok and envelope.output == {"ok": True}


# -- 崩溃收割 ---------------------------------------------------------------------


def test_runner_crash_becomes_failed_envelope() -> None:
    def crashing(_spec: SpawnSpec) -> ResultEnvelope:
        raise RuntimeError("subagent bug")

    supervisor = SubagentSupervisor()
    envelope = supervisor.spawn(make_spec(), crashing, parent_tier="execute")
    assert envelope.status == "failed" and envelope.output is None


# -- 并发闸 ≤3 -------------------------------------------------------------------


def test_global_concurrency_is_capped_at_three() -> None:
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def probing(_spec: SpawnSpec) -> ResultEnvelope:
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return ResultEnvelope(status="done", output={"ok": True})

    supervisor = SubagentSupervisor()
    threads = [
        threading.Thread(
            target=lambda: supervisor.spawn(make_spec(), probing, parent_tier="execute")
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert live["peak"] <= 3, f"并发峰值 {live['peak']} 超过全局上限 3"
    assert live["now"] == 0
