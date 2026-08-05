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
