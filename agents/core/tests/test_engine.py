import pytest

from omm_agent_core import (
    AdvanceOutcome,
    EventType,
    NodeResult,
    StepStatus,
    TaskState,
    WORK_SEQUENCE,
)

from conftest import ScriptedNode


def test_happy_path_runs_all_states_to_completion(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1", inputs={"problem": "demo"})

    outcome = engine.run_until_blocked(snapshot)

    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED
    executed = [step.state for step in snapshot.steps]
    assert executed == list(WORK_SEQUENCE)
    assert all(step.status is StepStatus.SUCCEEDED for step in snapshot.steps)
    # Outputs accumulate per state.
    assert snapshot.outputs[TaskState.PROBLEM_ANALYSIS.value] == {
        "echo": "PROBLEM_ANALYSIS"
    }
    # Event log is strictly monotonic from 1.
    seqs = [event.seq for event in sink.events]
    assert seqs == list(range(1, len(seqs) + 1))


def test_event_order_writes_events_before_state(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.advance(snapshot)

    types = [event.event_type for event in sink.events]
    assert types == [
        EventType.RUN_CREATED,
        EventType.STATE_CHANGED,
        EventType.STEP_STARTED,
        EventType.STEP_SUCCEEDED,
    ]


def test_step_failure_fails_run_and_retry_reenters(harness):
    failing = ScriptedNode(
        state=TaskState.DATA_PREPARATION,
        results=[
            NodeResult.failed("bad data"),
            NodeResult.succeeded(outputs={"echo": "second try"}),
        ],
    )
    engine, sink, _ = harness({TaskState.DATA_PREPARATION: failing})
    snapshot, _ = engine.create_run("proj_1")

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.FAILED
    assert snapshot.state is TaskState.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.state is TaskState.DATA_PREPARATION
    assert snapshot.failure.error == "bad data"

    engine.retry(snapshot)
    assert snapshot.state is TaskState.DATA_PREPARATION
    assert snapshot.failure is None

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED
    retried_steps = [
        step for step in snapshot.steps if step.state is TaskState.DATA_PREPARATION
    ]
    assert [step.attempt for step in retried_steps] == [1, 2]
    assert retried_steps[0].status is StepStatus.FAILED
    assert retried_steps[1].status is StepStatus.SUCCEEDED


def test_node_exception_is_captured_as_step_failure(harness):
    class ExplodingNode:
        def run(self, ctx, services):
            raise RuntimeError("boom")

    engine, _, _ = harness({TaskState.MODEL_PLANNING: ExplodingNode()})
    snapshot, _ = engine.create_run("proj_1")

    outcome = engine.run_until_blocked(snapshot)

    assert outcome.status == AdvanceOutcome.FAILED
    assert snapshot.failure is not None
    assert "boom" in snapshot.failure.error


def test_missing_node_registration_fails_cleanly(harness):
    engine, _, nodes = harness()
    del nodes[TaskState.VALIDATING]
    snapshot, _ = engine.create_run("proj_1")

    outcome = engine.run_until_blocked(snapshot)

    assert outcome.status == AdvanceOutcome.FAILED
    assert "no node registered" in (snapshot.failure.error or "")


def test_review_gate_blocks_then_resumes_forward(harness):
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[
            NodeResult.needs_review(
                reason="confirm plan A/B", outputs={"plan": "A"}
            )
        ],
    )
    engine, sink, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert snapshot.state is TaskState.NEEDS_REVIEW
    assert snapshot.review is not None
    assert snapshot.review.resume_state is TaskState.MODEL_PLANNING
    # The gated step's outputs are preserved before the review pause.
    assert snapshot.outputs[TaskState.MODEL_PLANNING.value] == {"plan": "A"}

    # advance() while awaiting review is a no-op
    idle = engine.advance(snapshot)
    assert idle.status == AdvanceOutcome.IDLE
    assert idle.events == []

    engine.resolve_review(snapshot, approved=True)
    assert snapshot.state is TaskState.MODEL_PLANNING

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED
    # Planning ran once: approval moves on without re-running the step.
    assert planning.calls == 1


def test_review_meta_rides_the_event_and_decision_folds_into_snapshot(harness):
    """G2 形态：needs_review 带闸门元数据 → REVIEW_REQUESTED 载荷带 gate；
    批准时 reason=option_id 折进 snapshot.review_decisions 供下游读取。"""
    prep = ScriptedNode(
        state=TaskState.DATA_PREPARATION,
        results=[
            NodeResult.needs_review(
                reason="数据清洗影响面较大，请确认",
                outputs={"profile_summary": "画像"},
                review_meta={"gate": "G2", "impact": {"rows_deleted_ratio": 0.2}},
            )
        ],
    )
    engine, sink, _ = harness({TaskState.DATA_PREPARATION: prep})
    snapshot, _ = engine.create_run("proj_1")

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    requested = [
        event for event in sink.events
        if event.event_type is EventType.REVIEW_REQUESTED
    ][-1]
    assert requested.payload["gate"] == {
        "gate": "G2",
        "impact": {"rows_deleted_ratio": 0.2},
    }

    engine.resolve_review(snapshot, approved=True, reason="use_raw")
    assert snapshot.review_decisions == {"DATA_PREPARATION": "use_raw"}
    # 下游节点经 NodeContext 读到该决策
    from omm_agent_core import NodeContext

    ctx = NodeContext.for_step(snapshot, TaskState.MODEL_PLANNING, "step_x", 1)
    assert ctx.review_decisions["DATA_PREPARATION"] == "use_raw"


def test_review_meta_absent_keeps_payload_shape(harness):
    """不带 review_meta 时 REVIEW_REQUESTED 载荷保持三键形状（金轨迹稳定）。"""
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[NodeResult.needs_review(reason="confirm plan")],
    )
    engine, sink, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    requested = [
        event for event in sink.events
        if event.event_type is EventType.REVIEW_REQUESTED
    ][-1]
    assert set(requested.payload) == {"reason", "requested_by_step", "resume_state"}


def test_step_started_clears_stale_gate_decision():
    """闸门决策随该状态重跑而失效（归约器层面）：旧产出的决策不得指导新产出。"""
    from omm_agent_core import AgentEvent, TaskRunSnapshot
    from omm_agent_core.reducer import apply_event

    snapshot = TaskRunSnapshot(
        run_id="r1", project_id="p1", state=TaskState.DATA_PREPARATION
    )
    snapshot.review_decisions["DATA_PREPARATION"] = "adopt_cleaned"
    apply_event(
        snapshot,
        AgentEvent(
            run_id="r1",
            seq=1,
            event_type=EventType.STEP_STARTED,
            payload={"step_id": "s1", "state": "DATA_PREPARATION", "attempt": 2},
            created_at="2026-01-01T00:00:00+00:00",
        ),
    )
    assert "DATA_PREPARATION" not in snapshot.review_decisions


def test_review_rejection_fails_run(harness):
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[NodeResult.needs_review(reason="confirm plan")],
    )
    engine, _, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    engine.resolve_review(snapshot, approved=False, reason="plan rejected")

    assert snapshot.state is TaskState.FAILED
    assert snapshot.review is None
    assert snapshot.failure is not None
    assert snapshot.failure.error == "plan rejected"


def test_resolve_review_requires_pending_review(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    with pytest.raises(ValueError):
        engine.resolve_review(snapshot, approved=True)


def test_forward_approval_payload_omits_rerun(harness):
    """顺延批准的载荷不带 rerun 键——既有金轨迹逐字节不变（ADR-0013）。"""
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[NodeResult.needs_review(reason="confirm plan")],
    )
    engine, sink, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.resolve_review(snapshot, approved=True, reason="plan_a")

    resolved = [
        event for event in sink.events
        if event.event_type is EventType.REVIEW_RESOLVED
    ][-1]
    assert set(resolved.payload) == {"approved", "resume_state", "reason"}


# -- 修订回合（ADR-0013）：跑完之后还能接着改 ---------------------------------


def test_revision_round_redoes_the_chosen_stage_and_everything_after(harness):
    """金轨迹：跑完 → 提修订 → 从建模方案重做 → 再次完成。"""
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    assert snapshot.state is TaskState.COMPLETED
    assert snapshot.revision_round == 0

    engine.request_revision(
        snapshot,
        TaskState.MODEL_PLANNING,
        reason="目标函数改成加权总成本",
        note_id="note_1",
    )
    # 不直接落到目标阶段：先挂进评审门，等人确认重做起点。
    assert snapshot.state is TaskState.NEEDS_REVIEW
    assert snapshot.revision_round == 1
    assert snapshot.review is not None
    assert snapshot.review.resume_state is TaskState.MODEL_PLANNING
    assert snapshot.review.revision_round == 1
    # 空的 requested_by_step 把用户发起的修订门与节点自提的闸门区分开。
    assert snapshot.review.requested_by_step == ""
    requested = [
        event for event in sink.events
        if event.event_type is EventType.REVISION_REQUESTED
    ][-1]
    assert requested.payload == {
        "target_state": "MODEL_PLANNING",
        "reason": "目标函数改成加权总成本",
        "round": 1,
        "note_id": "note_1",
    }
    # 门开着的时候引擎不推进。
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE

    engine.resolve_review(snapshot, approved=True)
    assert snapshot.state is TaskState.MODEL_PLANNING
    resolved = [
        event for event in sink.events
        if event.event_type is EventType.REVIEW_RESOLVED
    ][-1]
    assert resolved.payload["rerun"] is True

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED

    attempts = {}
    for step in snapshot.steps:
        attempts.setdefault(step.state, []).append(step.attempt)
    # 起点及其下游真的重跑了一趟；上游两段没被碰。
    assert attempts[TaskState.PROBLEM_ANALYSIS] == [1]
    assert attempts[TaskState.DATA_PREPARATION] == [1]
    for state in (
        TaskState.MODEL_PLANNING,
        TaskState.EXPERIMENTING,
        TaskState.VALIDATING,
        TaskState.PAPER_WRITING,
    ):
        assert attempts[state] == [1, 2], state
    # 历史逐趟留档，不被覆盖。
    assert all(step.status is StepStatus.SUCCEEDED for step in snapshot.steps)


def test_revision_without_rerun_flag_would_skip_the_stage(harness):
    """回归护栏：回退必须置 rerun，否则「退回建模方案」等于直接跳到实验。

    _select_target 见目标阶段最近一步已 SUCCEEDED 就顺延——forward-only 语义
    正是靠这条实现的。这里显式验证 rerun 关掉后确有此坑，免得日后有人把
    reducer 里那行当成冗余删掉。
    """
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.request_revision(snapshot, TaskState.MODEL_PLANNING, reason="改一下")
    engine.resolve_review(snapshot, approved=True)

    snapshot.force_rerun = False  # 模拟漏置标志
    engine.advance(snapshot)

    started = [step for step in snapshot.steps if step.attempt == 2]
    assert started and started[0].state is TaskState.EXPERIMENTING


def test_revision_discards_stale_outputs_of_redone_stages(harness):
    """第二轮少写的键不得残留——outputs 是 update 合并写入的，会串轮。"""
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[
            NodeResult.succeeded(outputs={"plan": "A", "objective": "min_cost"}),
            NodeResult.succeeded(outputs={"plan": "B"}),
        ],
    )
    engine, _, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    assert snapshot.outputs["MODEL_PLANNING"] == {
        "plan": "A",
        "objective": "min_cost",
    }

    engine.request_revision(snapshot, TaskState.MODEL_PLANNING, reason="换目标函数")
    engine.resolve_review(snapshot, approved=True)
    # 回退落地的瞬间，起点及下游的产出就已清空，节点读不到过期结果。
    assert "MODEL_PLANNING" not in snapshot.outputs
    assert "PAPER_WRITING" not in snapshot.outputs
    # 上游成果留着——它们本轮不重做，正是这一轮返工的依据。
    assert snapshot.outputs["DATA_PREPARATION"] == {"echo": "DATA_PREPARATION"}

    engine.run_until_blocked(snapshot)
    assert snapshot.outputs["MODEL_PLANNING"] == {"plan": "B"}


def test_revision_keeps_upstream_gate_decisions(harness):
    """上游闸门决策跨轮存活：G2 选了「用原始数据」，从建模方案重做不该忘掉。"""
    prep = ScriptedNode(
        state=TaskState.DATA_PREPARATION,
        results=[
            NodeResult.needs_review(
                reason="清洗影响面较大",
                outputs={"profile": "画像"},
                review_meta={"gate": "G2"},
            ),
            NodeResult.succeeded(outputs={"profile": "画像"}),
        ],
    )
    engine, _, _ = harness({TaskState.DATA_PREPARATION: prep})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.resolve_review(snapshot, approved=True, reason="use_raw")
    engine.run_until_blocked(snapshot)
    assert snapshot.state is TaskState.COMPLETED
    assert snapshot.review_decisions["DATA_PREPARATION"] == "use_raw"

    engine.request_revision(snapshot, TaskState.MODEL_PLANNING, reason="改目标函数")
    engine.resolve_review(snapshot, approved=True)

    assert snapshot.review_decisions["DATA_PREPARATION"] == "use_raw"
    # 而重做起点自身的决策不留——那是对上一轮产出的裁决，已经作废。
    assert "MODEL_PLANNING" not in snapshot.review_decisions


def test_declining_a_revision_restores_completed(harness):
    """撤回修订不是失败：什么都没跑坏，用户只是改了主意。"""
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.request_revision(snapshot, TaskState.PAPER_WRITING, reason="想改措辞")

    engine.resolve_review(snapshot, approved=False, reason="算了")

    assert snapshot.state is TaskState.COMPLETED
    assert snapshot.failure is None
    assert snapshot.review is None
    # 记的是发起过几轮，撤回不回退——防的是反复开关刷额度。
    assert snapshot.revision_round == 1
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE


def test_second_revision_round_increments_the_ledger(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    engine.request_revision(snapshot, TaskState.PAPER_WRITING, reason="第一次")
    engine.resolve_review(snapshot, approved=True)
    engine.run_until_blocked(snapshot)
    assert snapshot.revision_round == 1

    engine.request_revision(snapshot, TaskState.PAPER_WRITING, reason="第二次")
    assert snapshot.revision_round == 2
    engine.resolve_review(snapshot, approved=True)
    engine.run_until_blocked(snapshot)
    assert snapshot.state is TaskState.COMPLETED
    paper = [
        step.attempt for step in snapshot.steps if step.state is TaskState.PAPER_WRITING
    ]
    assert paper == [1, 2, 3]


def test_request_revision_rejects_unfinished_run_and_non_work_target(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    with pytest.raises(ValueError):
        engine.request_revision(snapshot, TaskState.MODEL_PLANNING, reason="太早了")

    engine.run_until_blocked(snapshot)
    with pytest.raises(ValueError):
        engine.request_revision(snapshot, TaskState.COMPLETED, reason="不是工作状态")


def test_revision_payload_omits_note_id_when_absent(harness):
    """没有 note 时不写该键，载荷形状保持最小（金轨迹稳定）。"""
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.request_revision(snapshot, TaskState.VALIDATING, reason="复核一下")

    requested = [
        event for event in sink.events
        if event.event_type is EventType.REVISION_REQUESTED
    ][-1]
    assert set(requested.payload) == {"target_state", "reason", "round"}


def test_pause_blocks_scheduling_and_resume_continues(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.advance(snapshot)  # PROBLEM_ANALYSIS done

    engine.request_pause(snapshot)
    outcome = engine.advance(snapshot)
    assert outcome.status == AdvanceOutcome.IDLE
    assert snapshot.paused is True
    assert len(snapshot.steps) == 1  # nothing new scheduled

    engine.resume(snapshot)
    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED


def test_cancel_finalizes_run_as_failed(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.advance(snapshot)

    engine.request_cancel(snapshot)
    outcome = engine.advance(snapshot)

    assert outcome.status == AdvanceOutcome.CANCELLED
    assert snapshot.state is TaskState.FAILED
    assert "cancelled" in snapshot.failure.error
    # Terminal now: further advances are no-ops.
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE


def test_control_actions_are_idempotent_noops_when_not_applicable(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")

    assert engine.resume(snapshot) == []  # not paused
    engine.request_pause(snapshot)
    assert engine.request_pause(snapshot) == []  # already paused

    engine.resume(snapshot)
    engine.run_until_blocked(snapshot)  # completes
    assert engine.request_pause(snapshot) == []  # terminal
    assert engine.request_cancel(snapshot) == []
    with pytest.raises(ValueError):
        engine.retry(snapshot)  # not failed


def test_run_until_blocked_respects_step_budget(harness):
    engine, _, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    outcome = engine.run_until_blocked(snapshot, max_steps=2)
    # Budget exhausted mid-run: last outcome is ADVANCED, run not terminal.
    assert outcome.status == AdvanceOutcome.ADVANCED
    assert not snapshot.is_terminal
