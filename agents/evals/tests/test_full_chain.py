"""Full-chain eval: all six REAL skill nodes end-to-end on the engine.

Invariants pinned here, per scenario:

- happy path: every stage SUCCEEDED, the event log IS the golden trajectory,
  outputs accumulate for all six stages, the paper artifact really exists;
- replay: folding the emitted events through ``replay_events`` rebuilds a
  snapshot identical to the live one (event log = single source of truth) —
  asserted for EVERY scenario below, not just the happy path;
- experiment repair round: a failing sandbox run is fed back into the next
  LLM generation (error_feedback / previous_code) and the run still completes;
- review rejection: reject → retry re-runs MODEL_PLANNING as attempt 2 and
  raises the gate again; approval then completes the run;
- experiment failure: two failed rounds fail the run at EXPERIMENTING and an
  explicit retry recovers it to COMPLETED.
"""

import pytest
from omm_agent_core import (
    WORK_SEQUENCE,
    AdvanceOutcome,
    EventType,
    StepStatus,
    TaskState,
)
from omm_agent_evals import (
    CANNED_EXPERIMENT,
    CANNED_PAPER,
    FULL_CHAIN_GOLDEN_EVENT_TYPES,
    FULL_CHAIN_METRICS,
    FULL_CHAIN_PROMPT_SEQUENCE,
    build_full_chain_session,
    sandbox_failure,
    sandbox_success,
)
from omm_agent_skills import PYTHON_TOOL_NAME


def drive_through_review(session):
    """run_until_blocked → approve the plan gate → run_until_blocked."""
    outcome = session.engine.run_until_blocked(session.snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert session.snapshot.review is not None
    assert session.snapshot.review.resume_state is TaskState.MODEL_PLANNING
    session.engine.resolve_review(session.snapshot, approved=True, reason="采用方案 A")
    return session.engine.run_until_blocked(session.snapshot)


def assert_replay_matches(session):
    """The event log alone must rebuild the exact live snapshot."""
    assert session.replay().to_dict() == session.snapshot.to_dict()


def steps_for(session, state):
    return [step for step in session.snapshot.steps if step.state is state]


def tool_events(session):
    return [
        event
        for event in session.sink.events
        if event.event_type is EventType.TOOL_CALLED
    ]


# -- 1. happy path -------------------------------------------------------------


@pytest.fixture()
def completed_session():
    session = build_full_chain_session()
    outcome = drive_through_review(session)
    assert outcome.status == AdvanceOutcome.COMPLETED
    return session


def test_happy_path_all_six_stages_succeed(completed_session):
    snapshot = completed_session.snapshot

    assert snapshot.state is TaskState.COMPLETED
    assert [step.state for step in snapshot.steps] == list(WORK_SEQUENCE)
    assert all(step.status is StepStatus.SUCCEEDED for step in snapshot.steps)

    # Outputs accumulated for every work stage, with real node content.
    assert set(snapshot.outputs) == {state.value for state in WORK_SEQUENCE}
    assert snapshot.outputs[TaskState.PROBLEM_ANALYSIS.value]["problem_type"] == "预测+优化"
    assert "历史运量" in snapshot.outputs[TaskState.DATA_PREPARATION.value]["profile_summary"]
    assert snapshot.outputs[TaskState.MODEL_PLANNING.value]["recommended_plan_id"] == "A"
    assert snapshot.outputs[TaskState.EXPERIMENTING.value]["metrics"] == FULL_CHAIN_METRICS
    assert snapshot.outputs[TaskState.VALIDATING.value]["verdict"] == "pass"
    assert snapshot.outputs[TaskState.PAPER_WRITING.value]["title"] == CANNED_PAPER["title"]

    # Prompt sequence in stage order：前五阶段各一次，论文阶段为
    # 总编规划 → 逐章写作 ×3 → 统稿收口的多轮管线。
    assert [call.prompt_id for call in completed_session.llm.calls] == (
        FULL_CHAIN_PROMPT_SEQUENCE
    )


def test_happy_path_event_log_is_the_golden_trajectory(completed_session):
    events = completed_session.sink.events
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events] == FULL_CHAIN_GOLDEN_EVENT_TYPES

    # Tool calls recorded through the engine with production payload:
    # ws_list belongs to the data stage (profiling preflight, empty here),
    # python_run to the experiment stage.
    all_tool_events = tool_events(completed_session)
    experiment_step = steps_for(completed_session, TaskState.EXPERIMENTING)[0]
    data_step = steps_for(completed_session, TaskState.DATA_PREPARATION)[0]

    ws_event, tool_event = all_tool_events
    assert ws_event.payload["tool"] == "ws_list"
    assert ws_event.payload["step_id"] == data_step.step_id
    assert tool_event.payload["tool"] == PYTHON_TOOL_NAME
    assert tool_event.payload["status"] == "succeeded"
    assert tool_event.payload["step_id"] == experiment_step.step_id

    # And the invoker itself saw exactly one python_run call with the code.
    (call,) = [
        entry for entry in completed_session.tools.calls if entry[2] == PYTHON_TOOL_NAME
    ]
    run_id, step_id, tool_name, arguments = call
    assert (run_id, step_id, tool_name) == (
        completed_session.snapshot.run_id,
        experiment_step.step_id,
        PYTHON_TOOL_NAME,
    )
    assert arguments["code"] == CANNED_EXPERIMENT["code"]


def test_happy_path_publishes_real_artifacts(completed_session):
    blobs = completed_session.artifacts.blobs

    experiment_step = steps_for(completed_session, TaskState.EXPERIMENTING)[0]
    results_ref, code_ref = experiment_step.artifacts
    assert results_ref.uri.endswith("results.csv")
    assert results_ref.kind == "table"
    assert blobs[results_ref.uri].startswith(b"quarter,")
    # The generated script itself is published for reproducibility.
    assert code_ref.uri.endswith("experiment.py")
    assert code_ref.kind == "code"
    assert blobs[code_ref.uri].decode("utf-8") == CANNED_EXPERIMENT["code"]

    paper_step = steps_for(completed_session, TaskState.PAPER_WRITING)[0]
    (paper_ref,) = paper_step.artifacts
    assert paper_ref.uri.endswith("paper-draft.md")
    assert paper_ref.kind == "paper"
    assert paper_ref.media_type == "text/markdown"
    markdown = blobs[paper_ref.uri].decode("utf-8")
    assert f"# {CANNED_PAPER['title']}" in markdown
    assert "## 3 模型检验" in markdown

    # Artifacts are referenced from outputs-facing events too.
    produced = [
        event.payload["artifact"]["uri"]
        for event in completed_session.sink.events
        if event.event_type is EventType.ARTIFACT_PRODUCED
    ]
    assert produced == [results_ref.uri, code_ref.uri, paper_ref.uri]


# -- 2. replay consistency -------------------------------------------------------


def test_replay_rebuilds_identical_snapshot(completed_session):
    assert_replay_matches(completed_session)


# -- 3. experiment repair round ---------------------------------------------------


def test_experiment_repair_round_feeds_error_back_and_completes():
    session = build_full_chain_session(
        tool_runs=[sandbox_failure(), sandbox_success()]
    )

    outcome = drive_through_review(session)

    assert outcome.status == AdvanceOutcome.COMPLETED
    (experiment_step,) = steps_for(session, TaskState.EXPERIMENTING)
    assert experiment_step.status is StepStatus.SUCCEEDED
    assert experiment_step.metrics == {"llm_attempts": 2, "code_rounds": 2}

    # Two sandbox invocations: the failing one, then the regenerated one
    # (the data stage's ws_list profiling preflight is filtered out here).
    sandbox_calls = [call for call in session.tools.calls if call[2] == PYTHON_TOOL_NAME]
    assert [call[2] for call in sandbox_calls] == [PYTHON_TOOL_NAME] * 2
    recorded = [
        event for event in tool_events(session)
        if event.payload["tool"] == PYTHON_TOOL_NAME
    ]
    assert [event.payload["status"] for event in recorded] == ["failed", "succeeded"]
    assert all(
        event.payload["step_id"] == experiment_step.step_id for event in recorded
    )

    # The second generation carried the runtime failure back to the LLM.
    experiment_calls = [
        call for call in session.llm.calls
        if call.prompt_id == "experiment_code.default"
    ]
    assert len(experiment_calls) == 2
    assert experiment_calls[0].variables["error_feedback"] == "无"
    assert experiment_calls[0].variables["previous_code"] == "无"
    assert "NameError" in experiment_calls[1].variables["error_feedback"]
    assert experiment_calls[1].variables["previous_code"] == CANNED_EXPERIMENT["code"]

    assert_replay_matches(session)


# -- 4. review rejection + retry --------------------------------------------------


def test_review_rejection_then_retry_replans_and_completes():
    session = build_full_chain_session()
    engine, snapshot = session.engine, session.snapshot

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED

    engine.resolve_review(snapshot, approved=False, reason="预算约束未满足，重新规划")
    assert snapshot.state is TaskState.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.state is TaskState.MODEL_PLANNING
    assert snapshot.failure.error == "预算约束未满足，重新规划"

    engine.retry(snapshot)
    assert snapshot.state is TaskState.MODEL_PLANNING
    assert snapshot.failure is None

    # The re-run raises the confirmation gate again, as attempt 2.
    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    planning_steps = steps_for(session, TaskState.MODEL_PLANNING)
    assert [step.attempt for step in planning_steps] == [1, 2]
    assert all(step.status is StepStatus.SUCCEEDED for step in planning_steps)

    engine.resolve_review(snapshot, approved=True, reason="第二版方案通过")
    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED

    # Trajectory shape: two gates, two resolutions, one explicit retry.
    types = [event.event_type for event in session.sink.events]
    assert types.count(EventType.REVIEW_REQUESTED) == 2
    assert types.count(EventType.REVIEW_RESOLVED) == 2
    assert types.count(EventType.RUN_RETRIED) == 1

    assert_replay_matches(session)


# -- 5. experiment failure + retry recovery ---------------------------------------


def test_experiment_double_failure_fails_run_then_retry_recovers():
    session = build_full_chain_session(
        tool_runs=[sandbox_failure(), sandbox_failure(), sandbox_success()]
    )
    engine, snapshot = session.engine, session.snapshot

    outcome = drive_through_review(session)

    assert outcome.status == AdvanceOutcome.FAILED
    assert snapshot.state is TaskState.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.state is TaskState.EXPERIMENTING
    assert "after 2 rounds" in snapshot.failure.error
    assert "NameError" in snapshot.failure.error

    def sandbox_calls():
        return [call for call in session.tools.calls if call[2] == PYTHON_TOOL_NAME]

    assert len(sandbox_calls()) == 2  # both rounds of attempt 1

    engine.retry(snapshot)
    assert snapshot.state is TaskState.EXPERIMENTING

    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED

    experiment_steps = steps_for(session, TaskState.EXPERIMENTING)
    assert [step.attempt for step in experiment_steps] == [1, 2]
    assert [step.status for step in experiment_steps] == [
        StepStatus.FAILED,
        StepStatus.SUCCEEDED,
    ]
    assert len(sandbox_calls()) == 3
    sandbox_events = [
        event for event in tool_events(session)
        if event.payload["tool"] == PYTHON_TOOL_NAME
    ]
    assert [event.payload["status"] for event in sandbox_events] == [
        "failed",
        "failed",
        "succeeded",
    ]

    assert_replay_matches(session)


# -- assembly knobs ----------------------------------------------------------------


def test_unattended_full_chain_completes_without_review():
    session = build_full_chain_session(require_confirmation=False)

    outcome = session.engine.run_until_blocked(session.snapshot)

    assert outcome.status == AdvanceOutcome.COMPLETED
    types = [event.event_type for event in session.sink.events]
    assert EventType.REVIEW_REQUESTED not in types
    assert_replay_matches(session)
