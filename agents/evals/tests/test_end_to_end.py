"""Golden end-to-end regression: queue → lease → engine → sandbox → artifacts.

This is the executable proof of the batch's acceptance criteria: one full
TaskRun advances CREATED → … → COMPLETED through the worker loop, pauses at
the human plan-confirmation gate, runs real Python in the sandbox, and every
byte of state is recoverable from the event log alone.
"""

import hashlib
import json
from pathlib import Path

import pytest

from omm_agent_core import (
    AdvanceOutcome,
    EventType,
    StepStatus,
    TaskState,
    WORK_SEQUENCE,
    replay_events,
)
from omm_agent_evals import GOLDEN_EVENT_TYPES, PROBLEM_STATEMENT, build_runtime
from omm_worker import WorkerLoop


@pytest.fixture()
def runtime(tmp_path):
    return build_runtime(tmp_path / "rt")


def drive(runtime):
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run(
        "proj_eval", inputs={"problem_statement": PROBLEM_STATEMENT}
    )

    outcomes = []
    while (outcome := loop.tick()) is not None:
        outcomes.append(outcome)
    assert outcomes == [AdvanceOutcome.REVIEW_REQUESTED]

    runtime.apply_action(run_id, "approve", reason="采用方案 A")

    outcomes = []
    while (outcome := loop.tick()) is not None:
        outcomes.append(outcome)
    assert outcomes == [AdvanceOutcome.COMPLETED]
    return run_id


def test_golden_trajectory_end_to_end(runtime):
    run_id = drive(runtime)
    snapshot = runtime.get_snapshot(run_id)

    # -- run reached the end with every stage succeeded ---------------------
    assert snapshot.state is TaskState.COMPLETED
    assert [step.state for step in snapshot.steps] == list(WORK_SEQUENCE)
    assert all(step.status is StepStatus.SUCCEEDED for step in snapshot.steps)

    # -- the event log IS the golden trajectory ------------------------------
    events = runtime.events.load(run_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events] == GOLDEN_EVENT_TYPES

    # -- replay from the log equals the live materialization -----------------
    replayed = replay_events(run_id, "proj_eval", events)
    assert replayed.to_dict() == snapshot.to_dict()


def test_tool_call_is_recorded_inside_experiment_step(runtime):
    run_id = drive(runtime)
    events = runtime.events.load(run_id)

    tool_events = [e for e in events if e.event_type is EventType.TOOL_CALLED]
    assert len(tool_events) == 1
    tool_event = tool_events[0]
    assert tool_event.payload["tool"] == "python_run"
    assert tool_event.payload["status"] == "succeeded"

    snapshot = runtime.get_snapshot(run_id)
    experiment_step = next(
        step for step in snapshot.steps if step.state is TaskState.EXPERIMENTING
    )
    assert tool_event.payload["step_id"] == experiment_step.step_id
    # Interleaving: the tool event sits between the step's start and success.
    types_by_seq = {event.seq: event.event_type for event in events}
    started_seq = next(
        e.seq
        for e in events
        if e.event_type is EventType.STEP_STARTED
        and e.payload["step_id"] == experiment_step.step_id
    )
    succeeded_seq = next(
        e.seq
        for e in events
        if e.event_type is EventType.STEP_SUCCEEDED
        and e.payload["step_id"] == experiment_step.step_id
    )
    assert started_seq < tool_event.seq < succeeded_seq
    assert types_by_seq[tool_event.seq] is EventType.TOOL_CALLED


def test_artifacts_exist_on_disk_with_matching_checksums(runtime):
    run_id = drive(runtime)
    snapshot = runtime.get_snapshot(run_id)

    all_artifacts = [ref for step in snapshot.steps for ref in step.artifacts]
    names = {Path(ref.uri).name for ref in all_artifacts}
    assert names == {"metrics.json", "report.md"}

    for ref in all_artifacts:
        stored = Path(ref.uri)
        assert stored.exists(), f"artifact missing on disk: {ref.uri}"
        content = stored.read_bytes()
        assert hashlib.sha256(content).hexdigest() == ref.sha256
        assert len(content) == ref.size

    metrics_ref = next(r for r in all_artifacts if Path(r.uri).name == "metrics.json")
    metrics = json.loads(Path(metrics_ref.uri).read_text(encoding="utf-8"))
    assert metrics["rmse"] < 0.1  # the least squares fit really ran


def test_outputs_flow_across_stages_into_the_report(runtime):
    run_id = drive(runtime)
    snapshot = runtime.get_snapshot(run_id)

    planning = snapshot.outputs[TaskState.MODEL_PLANNING.value]
    assert planning["recommended_plan_id"] == "A"

    validating = snapshot.outputs[TaskState.VALIDATING.value]
    assert validating["validation"] == "passed"

    paper = snapshot.outputs[TaskState.PAPER_WRITING.value]
    report_text = Path(paper["report_uri"]).read_text(encoding="utf-8")
    assert "推荐方案" in report_text
    assert "RMSE" in report_text
    assert "预测+优化" in report_text


def test_unattended_mode_completes_without_review(tmp_path):
    runtime = build_runtime(tmp_path / "rt2", require_confirmation=False)
    loop = WorkerLoop(runtime)
    run_id = runtime.create_run(
        "proj_eval", inputs={"problem_statement": PROBLEM_STATEMENT}
    )

    outcomes = []
    while (outcome := loop.tick()) is not None:
        outcomes.append(outcome)

    assert outcomes == [AdvanceOutcome.COMPLETED]
    events = runtime.events.load(run_id)
    assert EventType.REVIEW_REQUESTED not in [e.event_type for e in events]
