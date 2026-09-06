"""Graph v1 影子等价（设计文档 §6.5 / §16 H3 DoD「影子对比事件序列一致」）。

三层证据：

1. 每条全链场景在现引擎（``off``）与 Graph v1 驱动（``linear-v1``）下各跑一趟，
   控制流轨迹（事件类型 + 状态转移 + 步骤 / attempt + 审批点与闸门号）逐一相等，
   快照的控制流面相等，图驱动那趟的线性影子零分歧；
2. 同一批场景在缺省档位 ``shadow`` 下（现引擎驱动、图当影子）零分歧，且事件
   日志与 ``off`` 完全一致——影子从不碰事件；
3. 负对照：比对函数本身对内容差异不敏感、对控制流差异敏感（否则「全绿」没有意义）。
"""

import pytest
from omm_agent_core import AgentEvent, EventType, TaskState, WORK_SEQUENCE
from omm_agent_evals import (
    FULL_CHAIN_GOLDEN_EVENT_TYPES,
    SHADOW_SCENARIOS,
    compare_scenario,
    control_flow_trace,
    run_scenario,
    scenario_names,
    snapshot_control_flow,
)

SCENARIO_IDS = scenario_names()


@pytest.mark.parametrize("scenario", SHADOW_SCENARIOS, ids=SCENARIO_IDS)
def test_graph_driven_run_is_control_flow_equivalent_to_the_linear_engine(scenario):
    report = compare_scenario(scenario)

    assert report.graph_trace == report.baseline_trace
    assert report.graph_flow == report.baseline_flow
    assert report.divergences == []
    assert report.equivalent
    # 轨迹里确有控制流可比：至少一次状态转移、至少一步
    assert any(entry[0] == EventType.STATE_CHANGED.value for entry in report.graph_trace)
    assert report.graph_flow["steps"]


@pytest.mark.parametrize("scenario", SHADOW_SCENARIOS, ids=SCENARIO_IDS)
def test_shadow_mode_records_no_divergence_and_leaves_the_log_untouched(scenario):
    shadowed = run_scenario(scenario, "shadow")
    baseline = run_scenario(scenario, "off")

    assert shadowed.engine.shadow_divergences == []
    # 影子不发事件、不改推进：事件类型序列与控制流轨迹与 off 完全一致
    # （产物 id 等内容字段每趟随机，不在比对之列——§6.5）
    assert [e.event_type for e in shadowed.sink.events] == [e.event_type for e in baseline.sink.events]
    assert control_flow_trace(shadowed.sink.events) == control_flow_trace(baseline.sink.events)
    assert snapshot_control_flow(shadowed.snapshot) == snapshot_control_flow(baseline.snapshot)
    assert shadowed.replay().to_dict() == shadowed.snapshot.to_dict()


def test_graph_driven_happy_path_is_the_golden_trajectory():
    session = run_scenario(SHADOW_SCENARIOS[0], "linear-v1")
    assert [event.event_type for event in session.sink.events] == FULL_CHAIN_GOLDEN_EVENT_TYPES
    assert [step.state for step in session.snapshot.steps] == list(WORK_SEQUENCE)
    assert session.replay().to_dict() == session.snapshot.to_dict()


def test_scenarios_cover_every_control_flow_branch():
    """场景集合覆盖现引擎的全部分叉：审批放行 / 拒绝 / 回退重做 / 失败重试 / 修订 / 暂停 / 取消。"""
    seen: set[str] = set()
    for scenario in SHADOW_SCENARIOS:
        for entry in control_flow_trace(run_scenario(scenario, "off").sink.events):
            seen.add(entry[0])
            if entry[0] == EventType.REVIEW_RESOLVED.value:
                fields = dict(entry[1:])
                seen.add("approve" if fields["approved"] else "reject")
                if fields.get("rerun"):
                    seen.add("rerun")
                if fields.get("revision_round"):
                    seen.add("revision_resolved")
    assert {
        EventType.STATE_CHANGED.value,
        EventType.STEP_FAILED.value,
        EventType.REVIEW_REQUESTED.value,
        EventType.REVIEW_RESOLVED.value,
        EventType.REVISION_REQUESTED.value,
        EventType.RUN_RETRIED.value,
        EventType.RUN_PAUSED.value,
        EventType.RUN_RESUMED.value,
        EventType.RUN_CANCELLED.value,
        EventType.RUN_FAILED.value,
        EventType.RUN_COMPLETED.value,
        "approve",
        "reject",
        "rerun",
        "revision_resolved",
    } <= seen


# -- 比对函数的负对照 ----------------------------------------------------------------------


def _event(seq, event_type, payload):
    return AgentEvent(
        run_id="r", seq=seq, event_type=event_type, payload=payload, created_at=f"t{seq}"
    )


def test_control_flow_trace_ignores_content_but_not_control_flow():
    base = [
        _event(1, EventType.RUN_CREATED, {"project_id": "p", "inputs": {"goal": "a"}}),
        _event(2, EventType.STATE_CHANGED, {"from": "CREATED", "to": "PROBLEM_ANALYSIS"}),
        _event(3, EventType.STEP_STARTED, {"step_id": "s1", "state": "PROBLEM_ANALYSIS", "attempt": 1}),
        _event(4, EventType.STEP_SUCCEEDED, {"step_id": "s1", "outputs": {"title": "x"}, "metrics": {}}),
        _event(
            5,
            EventType.REVIEW_REQUESTED,
            {"reason": "r", "requested_by_step": "s1", "resume_state": "PROBLEM_ANALYSIS", "gate": {"gate": "G1"}},
        ),
    ]
    # 内容差异（id / 时间戳 / outputs 正文 / 审批理由）不影响轨迹
    content_only = [
        _event(1, EventType.RUN_CREATED, {"project_id": "q", "inputs": {"goal": "b"}}),
        _event(2, EventType.STATE_CHANGED, {"from": "CREATED", "to": "PROBLEM_ANALYSIS"}),
        _event(3, EventType.STEP_STARTED, {"step_id": "s9", "state": "PROBLEM_ANALYSIS", "attempt": 1}),
        _event(4, EventType.STEP_SUCCEEDED, {"step_id": "s9", "outputs": {"title": "y"}, "metrics": {"a": 1}}),
        _event(
            5,
            EventType.REVIEW_REQUESTED,
            {"reason": "other", "requested_by_step": "s9", "resume_state": "PROBLEM_ANALYSIS", "gate": {"gate": "G1", "options": []}},
        ),
    ]
    assert control_flow_trace(base) == control_flow_trace(content_only)

    # 控制流差异敏感：attempt 不同 / 闸门号不同 / 状态不同 / 少一个事件
    different_attempt = list(base)
    different_attempt[2] = _event(3, EventType.STEP_STARTED, {"step_id": "s1", "state": "PROBLEM_ANALYSIS", "attempt": 2})
    assert control_flow_trace(different_attempt) != control_flow_trace(base)
    different_gate = list(base)
    different_gate[4] = _event(5, EventType.REVIEW_REQUESTED, {"reason": "r", "requested_by_step": "s1", "resume_state": "PROBLEM_ANALYSIS", "gate": {"gate": "G2"}})
    assert control_flow_trace(different_gate) != control_flow_trace(base)
    different_state = list(base)
    different_state[1] = _event(2, EventType.STATE_CHANGED, {"from": "CREATED", "to": "DATA_PREPARATION"})
    assert control_flow_trace(different_state) != control_flow_trace(base)
    assert control_flow_trace(base[:-1]) != control_flow_trace(base)

    assert control_flow_trace(base)[4] == (
        "REVIEW_REQUESTED",
        ("resume_state", "PROBLEM_ANALYSIS"),
        ("gate", "G1"),
    )


def test_snapshot_control_flow_reads_the_control_plane_only():
    session = run_scenario(SHADOW_SCENARIOS[0], "off")
    flow = snapshot_control_flow(session.snapshot)
    assert flow["state"] == TaskState.COMPLETED.value
    assert flow["steps"] == [(state.value, 1, "SUCCEEDED") for state in WORK_SEQUENCE]
    assert flow["review"] is None and flow["failure"] is None
    assert flow["review_decisions"] == ["MODEL_PLANNING", "PAPER_WRITING"]
    assert flow["revision_round"] == 0
