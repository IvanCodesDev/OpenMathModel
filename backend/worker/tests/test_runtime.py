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
