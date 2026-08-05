import json

from omm_agent_core import (
    AgentEvent,
    EventType,
    FixedClock,
    NodeResult,
    NodeServices,
    SequentialIdGenerator,
    TaskRunEngine,
    WORK_SEQUENCE,
    replay_events,
)
from omm_worker import JsonlEventStore


def make_event(run_id: str, seq: int) -> AgentEvent:
    return AgentEvent(
        run_id=run_id,
        seq=seq,
        event_type=EventType.RUN_PAUSED,
        payload={},
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_emit_appends_and_load_roundtrips(tmp_path):
    store = JsonlEventStore(tmp_path)
    first = AgentEvent(
        run_id="run_1",
        seq=1,
        event_type=EventType.RUN_CREATED,
        payload={"project_id": "proj", "inputs": {}},
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.emit(first)
    store.emit(make_event("run_1", 2))

    loaded = store.load("run_1")
    assert [event.seq for event in loaded] == [1, 2]
    assert loaded[0] == first

    lines = (tmp_path / "run_1" / "events.jsonl").read_text(encoding="utf-8")
    assert len(lines.strip().splitlines()) == 2
    assert json.loads(lines.splitlines()[0])["event_type"] == "RUN_CREATED"


def test_duplicate_and_stale_deliveries_are_dropped(tmp_path):
    store = JsonlEventStore(tmp_path)
    store.emit(make_event("run_1", 1))
    store.emit(make_event("run_1", 2))
    store.emit(make_event("run_1", 2))  # duplicate
    store.emit(make_event("run_1", 1))  # stale redelivery

    assert [event.seq for event in store.load("run_1")] == [1, 2]


def test_fresh_instance_scans_existing_log_for_dedupe(tmp_path):
    JsonlEventStore(tmp_path).emit(make_event("run_1", 1))

    second = JsonlEventStore(tmp_path)
    assert second.max_seq("run_1") == 1
    second.emit(make_event("run_1", 1))  # must be dropped, not duplicated
    assert [event.seq for event in second.load("run_1")] == [1]


def test_runs_are_isolated_and_listed(tmp_path):
    store = JsonlEventStore(tmp_path)
    store.emit(make_event("run_a", 1))
    store.emit(make_event("run_b", 1))
    assert store.max_seq("run_a") == 1
    assert store.max_seq("run_b") == 1
    assert store.run_ids() == ["run_a", "run_b"]


class EchoNode:
    def run(self, ctx, services):
        return NodeResult.succeeded(outputs={"echo": ctx.state.value})


def test_engine_with_jsonl_sink_replays_identically(tmp_path):
    store = JsonlEventStore(tmp_path)
    clock, ids = FixedClock(), SequentialIdGenerator()
    services = NodeServices(clock=clock, ids=ids)
    engine = TaskRunEngine(
        sink=store,
        clock=clock,
        ids=ids,
        nodes={state: EchoNode() for state in WORK_SEQUENCE},
        services=services,
    )
    snapshot, _ = engine.create_run("proj_1", inputs={"k": "v"})
    engine.run_until_blocked(snapshot)

    events = store.load(snapshot.run_id)
    replayed = replay_events(snapshot.run_id, "proj_1", events)
    assert replayed.to_dict() == snapshot.to_dict()
