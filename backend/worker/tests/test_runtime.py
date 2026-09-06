import pytest

from omm_agent_core import (
    AdvanceOutcome,
    NodeResult,
    StepStatus,
    TaskState,
    WORK_SEQUENCE,
)
from omm_worker import WorkerConfig, WorkerLoop, WorkerRuntime


class EchoNode:
    def run(self, ctx, services):
        return NodeResult.succeeded(outputs={"echo": ctx.state.value})


class CrashOnceNode:
    """Simulates an executor dying mid-step: STEP_STARTED is durable, then the
    process 'dies' (SystemExit bypasses the engine's Exception guard)."""

    def __init__(self):
        self.calls = 0

    def run(self, ctx, services):
        self.calls += 1
        if self.calls == 1:
            raise SystemExit("simulated worker death")
        return NodeResult.succeeded(outputs={"echo": "recovered"})


class ReviewNode:
    def run(self, ctx, services):
        return NodeResult.needs_review(reason="确认方案", outputs={"plan": "A"})


def echo_registry(overrides=None):
    nodes = {state: EchoNode() for state in WORK_SEQUENCE}
    if overrides:
        nodes.update(overrides)
    return nodes


def make_runtime(tmp_path, overrides=None, **config_kwargs):
    config = WorkerConfig(root=tmp_path / "rt", **config_kwargs)
    return WorkerRuntime(config, nodes=echo_registry(overrides), worker_id="worker_t")


def drain(loop, limit=10):
    outcomes = []
    for _ in range(limit):
        outcome = loop.tick()
        if outcome is None:
            break
        outcomes.append(outcome)
    return outcomes


def test_full_run_via_queue_completes(tmp_path):
    runtime = make_runtime(tmp_path)
    loop = WorkerLoop(runtime)

    run_id = runtime.create_run("proj_1", inputs={"problem_statement": "题目"})
    outcomes = drain(loop)

    assert outcomes == [AdvanceOutcome.COMPLETED]
    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.COMPLETED
    assert [step.state for step in snapshot.steps] == list(WORK_SEQUENCE)
    seqs = [event.seq for event in runtime.events.load(run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert runtime.queue.counts()["done"] == 1


def test_duplicate_advance_jobs_are_harmless(tmp_path):
    runtime = make_runtime(tmp_path)
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")
    runtime.queue.enqueue(run_id, kind="advance")  # duplicate delivery

    outcomes = drain(loop)

    assert outcomes == [AdvanceOutcome.COMPLETED, AdvanceOutcome.IDLE]
    events_after = runtime.events.load(run_id)
    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.COMPLETED
    # The idle re-delivery appended nothing.
    assert events_after[-1].event_type.value == "RUN_COMPLETED"
    assert runtime.queue.counts()["done"] == 2


def test_crash_mid_step_recovers_with_new_attempt(tmp_path):
    crash_node = CrashOnceNode()
    runtime = make_runtime(
        tmp_path, overrides={TaskState.MODEL_PLANNING: crash_node}, claim_ttl_s=60.0
    )
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")

    with pytest.raises(SystemExit):
        loop.tick()  # worker "dies" mid MODEL_PLANNING

    # The job is stuck in claimed/ until its claim goes stale.
    import os
    import time

    claimed = list((runtime.queue.root / "claimed").glob("job_*.json"))
    assert len(claimed) == 1
    stale = time.time() - 3600
    os.utime(claimed[0], (stale, stale))

    outcomes = drain(loop)
    assert outcomes == [AdvanceOutcome.COMPLETED]

    snapshot = runtime.get_snapshot(run_id)
    planning_steps = [
        step for step in snapshot.steps if step.state is TaskState.MODEL_PLANNING
    ]
    assert [step.attempt for step in planning_steps] == [1, 2]
    assert planning_steps[0].status is StepStatus.FAILED
    assert "interrupted" in planning_steps[0].error
    assert planning_steps[1].status is StepStatus.SUCCEEDED
    assert crash_node.calls == 2


def test_review_gate_pauses_then_approval_resumes(tmp_path):
    runtime = make_runtime(tmp_path, overrides={TaskState.MODEL_PLANNING: ReviewNode()})
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")

    outcomes = drain(loop)
    assert outcomes == [AdvanceOutcome.REVIEW_REQUESTED]
    assert runtime.get_snapshot(run_id).state is TaskState.NEEDS_REVIEW

    state_after = runtime.apply_action(run_id, "approve", reason="方案可行")
    assert state_after == TaskState.MODEL_PLANNING.value

    outcomes = drain(loop)
    assert outcomes == [AdvanceOutcome.COMPLETED]
    assert runtime.get_snapshot(run_id).state is TaskState.COMPLETED


def test_cancel_action_finalizes_run(tmp_path):
    runtime = make_runtime(tmp_path, overrides={TaskState.MODEL_PLANNING: ReviewNode()})
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")
    drain(loop)  # reaches NEEDS_REVIEW

    runtime.apply_action(run_id, "cancel")
    outcomes = drain(loop)

    assert outcomes == [AdvanceOutcome.CANCELLED]
    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.FAILED
    assert "cancelled" in snapshot.failure.error


def test_retry_after_failure_reenters_failed_state(tmp_path):
    class FailOnceNode:
        def __init__(self):
            self.calls = 0

        def run(self, ctx, services):
            self.calls += 1
            if self.calls == 1:
                return NodeResult.failed("first try broken")
            return NodeResult.succeeded(outputs={"echo": "ok"})

    runtime = make_runtime(
        tmp_path, overrides={TaskState.EXPERIMENTING: FailOnceNode()}
    )
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")

    assert drain(loop) == [AdvanceOutcome.FAILED]
    assert runtime.get_snapshot(run_id).state is TaskState.FAILED

    runtime.apply_action(run_id, "retry")
    assert drain(loop) == [AdvanceOutcome.COMPLETED]


def test_busy_lease_requeues_job(tmp_path):
    runtime = make_runtime(tmp_path)
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run("proj_1")

    foreign = runtime.leases.acquire(run_id, "another_worker")
    assert foreign is not None

    outcome = loop.tick()
    assert outcome == WorkerRuntime.LEASE_BUSY
    assert runtime.queue.counts()["pending"] == 1  # requeued, not lost

    runtime.leases.release(foreign)
    assert drain(loop) == [AdvanceOutcome.COMPLETED]


def test_unknown_run_job_is_not_poisonous(tmp_path):
    runtime = make_runtime(tmp_path)
    loop = WorkerLoop(runtime)
    runtime.queue.enqueue("run_ghost", kind="advance")

    assert loop.tick() == WorkerRuntime.UNKNOWN_RUN
    assert runtime.queue.counts()["done"] == 1


def test_apply_action_rejects_unknown_action_and_run(tmp_path):
    runtime = make_runtime(tmp_path)
    run_id = runtime.create_run("proj_1", enqueue=False)
    with pytest.raises(ValueError):
        runtime.apply_action(run_id, "warp")
    with pytest.raises(KeyError):
        runtime.apply_action("run_missing", "pause")


# -- 调度档位 OMM_GRAPH（§4.9）：图驱动与线性推进控制流等价（§6.5）------------------------


def _control_flow(events):
    return [
        (
            event.event_type,
            event.payload.get("from"),
            event.payload.get("to"),
            event.payload.get("state"),
            event.payload.get("attempt"),
            event.payload.get("resume_state"),
            event.payload.get("approved"),
            event.payload.get("target_state"),
        )
        for event in events
    ]


def _drive_gate_reject_retry(runtime, loop):
    """审批门 → 拒绝（退回重做）→ 门再弹 → 批准 → 完成：worker 里最曲折的一条控制流。"""
    run_id = runtime.create_run("proj_1", inputs={"problem_statement": "题目"})
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]
    runtime.apply_action(run_id, "reject", reason="退回重做")
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]
    runtime.apply_action(run_id, "approve", reason="方案可行")
    assert drain(loop) == [AdvanceOutcome.COMPLETED]
    return run_id


def _gated_runtime(root, graph_mode):
    return WorkerRuntime(
        WorkerConfig(root=root),
        nodes=echo_registry({TaskState.MODEL_PLANNING: ReviewNode()}),
        worker_id=f"worker_{graph_mode}",
        graph_mode=graph_mode,
    )


def test_graph_driven_worker_matches_linear_control_flow(tmp_path):
    baseline = _gated_runtime(tmp_path / "linear", "off")
    graph = _gated_runtime(tmp_path / "graph", "linear-v1")
    assert (baseline.graph_mode, graph.graph_mode) == ("off", "linear-v1")

    base_run = _drive_gate_reject_retry(baseline, WorkerLoop(baseline))
    graph_run = _drive_gate_reject_retry(graph, WorkerLoop(graph))

    base_events = baseline.events.load(base_run)
    graph_events = graph.events.load(graph_run)
    assert _control_flow(graph_events) == _control_flow(base_events)
    assert [step.attempt for step in graph.get_snapshot(graph_run).steps
            if step.state is TaskState.MODEL_PLANNING] == [1, 2]
    # 图驱动时线性当影子：每一步的决策也逐一相同
    assert graph.shadow_divergences == []


def test_default_graph_mode_is_shadow_and_records_no_divergence(tmp_path, monkeypatch):
    monkeypatch.delenv("OMM_GRAPH", raising=False)
    runtime = make_runtime(tmp_path, overrides={TaskState.MODEL_PLANNING: ReviewNode()})
    assert runtime.graph_mode == "shadow"

    _drive_gate_reject_retry(runtime, WorkerLoop(runtime))

    assert runtime.shadow_divergences == []


def test_graph_mode_comes_from_env_and_bad_values_fall_back_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMM_GRAPH", "linear-v1")
    assert make_runtime(tmp_path / "a").graph_mode == "linear-v1"
    monkeypatch.setenv("OMM_GRAPH", "off")
    assert make_runtime(tmp_path / "b").graph_mode == "off"

    monkeypatch.setenv("OMM_GRAPH", "modeling-v2")
    with caplog.at_level("WARNING", logger="omm_worker.runtime"):
        runtime = make_runtime(tmp_path / "c")
    assert runtime.graph_mode == "shadow"
    assert any("OMM_GRAPH='modeling-v2'" in record.getMessage() for record in caplog.records)
    # 显式参数优先于环境变量
    assert WorkerRuntime(
        WorkerConfig(root=tmp_path / "d"), nodes=echo_registry(), graph_mode="off"
    ).graph_mode == "off"
