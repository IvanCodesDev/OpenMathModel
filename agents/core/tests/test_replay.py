import pytest

from omm_agent_core import (
    AgentEvent,
    EventType,
    NodeResult,
    SequenceError,
    TaskState,
    replay_events,
)

from conftest import ScriptedNode


class ArtifactNode:
    """Produces a real artifact through the injected store."""

    def run(self, ctx, services):
        ref = services.artifacts.put(
            run_id=ctx.run_id,
            kind="chart",
            name="loss.png",
            content=b"fake-png-bytes",
            media_type="image/png",
            producer_step=ctx.step_id,
        )
        return NodeResult.succeeded(outputs={"chart": ref.uri}, artifacts=(ref,))


def replay_of(sink, snapshot):
    return replay_events(snapshot.run_id, snapshot.project_id, sink.events)


def test_replay_equals_live_snapshot_happy_path(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1", inputs={"problem": "demo"})
    engine.run_until_blocked(snapshot)

    assert replay_of(sink, snapshot).to_dict() == snapshot.to_dict()


def test_replay_equals_live_snapshot_after_failure_and_retry(harness):
    flaky = ScriptedNode(
        state=TaskState.EXPERIMENTING,
        results=[NodeResult.failed("cuda melted"), NodeResult.succeeded()],
    )
    engine, sink, _ = harness({TaskState.EXPERIMENTING: flaky})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.retry(snapshot)
    engine.run_until_blocked(snapshot)

    assert replay_of(sink, snapshot).to_dict() == snapshot.to_dict()


def test_replay_equals_live_snapshot_with_review_and_controls(harness):
    gated = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[NodeResult.needs_review(reason="confirm", outputs={"plan": "B"})],
    )
    engine, sink, _ = harness({TaskState.MODEL_PLANNING: gated})
    snapshot, _ = engine.create_run("proj_1")
    engine.advance(snapshot)
    engine.request_pause(snapshot)
    engine.resume(snapshot)
    engine.run_until_blocked(snapshot)
    engine.resolve_review(snapshot, approved=True)
    engine.run_until_blocked(snapshot)

    assert snapshot.state is TaskState.COMPLETED
    assert replay_of(sink, snapshot).to_dict() == snapshot.to_dict()


def test_replay_equals_live_snapshot_after_a_revision_round(harness):
    """修订回合后回放仍等于快照（ADR-0011 承重不变量）。

    这一条专门盯 force_rerun：它不参与 to_dict 序列化，靠 REVIEW_RESOLVED
    置位、STEP_STARTED 清位，只有两边都对，重放才会收敛到同一状态。
    """
    planning = ScriptedNode(
        state=TaskState.MODEL_PLANNING,
        results=[
            NodeResult.succeeded(outputs={"plan": "A", "objective": "min_cost"}),
            NodeResult.succeeded(outputs={"plan": "B"}),
        ],
    )
    engine, sink, _ = harness({TaskState.MODEL_PLANNING: planning})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.request_revision(
        snapshot, TaskState.MODEL_PLANNING, reason="换目标函数", note_id="note_1"
    )
    engine.resolve_review(snapshot, approved=True)
    engine.run_until_blocked(snapshot)

    assert snapshot.state is TaskState.COMPLETED
    assert snapshot.revision_round == 1
    assert replay_of(sink, snapshot).to_dict() == snapshot.to_dict()


def test_replay_equals_live_snapshot_after_a_declined_revision(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)
    engine.request_revision(snapshot, TaskState.PAPER_WRITING, reason="想改措辞")
    engine.resolve_review(snapshot, approved=False, reason="算了")

    assert snapshot.state is TaskState.COMPLETED
    assert replay_of(sink, snapshot).to_dict() == snapshot.to_dict()


def test_artifacts_are_evented_before_step_success(harness):
    engine, sink, _ = harness({TaskState.VALIDATING: ArtifactNode()})
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    types = [event.event_type for event in sink.events]
    artifact_index = types.index(EventType.ARTIFACT_PRODUCED)
    success_after_artifact = types[artifact_index:].index(EventType.STEP_SUCCEEDED)
    assert success_after_artifact > 0  # artifact persisted before step success

    replayed = replay_of(sink, snapshot)
    validating_steps = [
        step for step in replayed.steps if step.state is TaskState.VALIDATING
    ]
    assert len(validating_steps[0].artifacts) == 1
    ref = validating_steps[0].artifacts[0]
    assert ref.sha256 and ref.size == len(b"fake-png-bytes")
    assert replayed.to_dict() == snapshot.to_dict()


def test_reducer_rejects_gaps_and_duplicates(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    events = sink.events
    with pytest.raises(SequenceError):
        replay_events(snapshot.run_id, snapshot.project_id, events[1:])  # gap
    with pytest.raises(SequenceError):
        replay_events(
            snapshot.run_id, snapshot.project_id, [events[0], events[0], *events[1:]]
        )  # duplicate


def test_sink_deduplicates_redelivered_events(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.advance(snapshot)

    before = len(sink.events)
    sink.emit(sink.events[0])  # simulate at-least-once redelivery
    assert len(sink.events) == before


def test_event_serialization_roundtrip(harness):
    engine, sink, _ = harness()
    snapshot, _ = engine.create_run("proj_1")
    engine.run_until_blocked(snapshot)

    for event in sink.events:
        assert AgentEvent.from_dict(event.to_dict()) == event
