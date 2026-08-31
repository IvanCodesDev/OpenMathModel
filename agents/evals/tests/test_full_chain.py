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
    CANNED_EXPERIMENT_CODE,
    CANNED_PAPER,
    FULL_CHAIN_CHAT_SEQUENCE,
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


def _user_prompt(chat_call):
    """沙盒会话的 user 段：目标 / 任务说明 / 种子 / 验收 / 修复反馈。"""
    return next(m["content"] for m in chat_call.messages if m["role"] == "user")


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

    # 模型出口分两条：模板单发（complete）与沙盒会话（chat_text）。
    # 模板序列按阶段顺序，论文阶段是总编规划 → 逐章写作 ×3 → 统稿收口；
    # 实验阶段不在其中——它已迁移为沙盒执行体，只走会话通道。
    assert [call.prompt_id for call in completed_session.llm.calls] == (
        FULL_CHAIN_PROMPT_SEQUENCE
    )
    assert [call.label for call in completed_session.llm.chat_calls] == (
        FULL_CHAIN_CHAT_SEQUENCE
    )


def test_happy_path_event_log_is_the_golden_trajectory(completed_session):
    events = completed_session.sink.events
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events] == FULL_CHAIN_GOLDEN_EVENT_TYPES

    # Tool calls recorded through the engine with production payload: the
    # data stage lists data/ once (profiling preflight, empty here); the
    # experiment stage is a sandbox agent, so it surveys the workspace and
    # environment, runs code, then re-lists for assertion evidence.
    all_tool_events = tool_events(completed_session)
    experiment_step = steps_for(completed_session, TaskState.EXPERIMENTING)[0]
    data_step = steps_for(completed_session, TaskState.DATA_PREPARATION)[0]

    ws_event, *sandbox_events = all_tool_events
    assert ws_event.payload["tool"] == "ws_list"
    assert ws_event.payload["step_id"] == data_step.step_id
    assert [event.payload["tool"] for event in sandbox_events] == [
        "ws_list",
        "env_probe",
        PYTHON_TOOL_NAME,
        "ws_list",
    ]
    assert all(event.payload["status"] == "succeeded" for event in sandbox_events)
    assert all(
        event.payload["step_id"] == experiment_step.step_id
        for event in sandbox_events
    )

    # And the invoker itself saw exactly one python_run call, carrying the
    # code the model wrote through the tool envelope.
    (call,) = [
        entry for entry in completed_session.tools.calls if entry[2] == PYTHON_TOOL_NAME
    ]
    run_id, step_id, tool_name, arguments = call
    assert (run_id, step_id, tool_name) == (
        completed_session.snapshot.run_id,
        experiment_step.step_id,
        PYTHON_TOOL_NAME,
    )
    assert arguments["code"] == CANNED_EXPERIMENT_CODE


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
    assert blobs[code_ref.uri].decode("utf-8") == CANNED_EXPERIMENT_CODE

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
    # 两波修复，每波两次会话（工具轮 + 终答）；code_rounds 是真实沙箱运行次数。
    assert experiment_step.metrics == {
        "llm_attempts": 4,
        "code_rounds": 2,
        "waves": 2,
    }

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

    # 第二波任务卡带上第一波的真实报错与上一轮代码（结构化反馈，非全对话转录）。
    waves = [
        call for call in session.llm.chat_calls
        if call.label == "experiment_code.sandbox"
    ]
    assert len(waves) == 4
    first_wave_prompt = _user_prompt(waves[0])
    assert "上一轮未通过验收" not in first_wave_prompt
    second_wave_prompt = _user_prompt(waves[2])
    assert "NameError" in second_wave_prompt
    assert CANNED_EXPERIMENT_CODE in second_wave_prompt

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


def test_experiment_double_failure_recovers_within_single_attempt():
    # 波次 3 的核心收益：修复波再引入新 bug（真实案例 e896 的死法）不再
    # 让整个任务报废，第三波自愈后本次尝试直接成功。
    session = build_full_chain_session(
        tool_runs=[sandbox_failure(), sandbox_failure(), sandbox_success()]
    )

    outcome = drive_through_review(session)

    assert outcome.status == AdvanceOutcome.COMPLETED
    (experiment_step,) = steps_for(session, TaskState.EXPERIMENTING)
    assert experiment_step.status is StepStatus.SUCCEEDED
    assert experiment_step.metrics == {
        "llm_attempts": 6,
        "code_rounds": 3,
        "waves": 3,
    }

    assert_replay_matches(session)


def test_experiment_rounds_exhausted_fails_run_then_retry_recovers():
    session = build_full_chain_session(
        tool_runs=[
            sandbox_failure(),
            sandbox_failure(),
            sandbox_failure(),
            sandbox_success(),
        ]
    )
    engine, snapshot = session.engine, session.snapshot

    outcome = drive_through_review(session)

    assert outcome.status == AdvanceOutcome.FAILED
    assert snapshot.state is TaskState.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.state is TaskState.EXPERIMENTING
    assert "after 3 wave(s), 3 run(s)" in snapshot.failure.error
    assert "[run_ok]" in snapshot.failure.error
    assert "NameError" in snapshot.failure.error

    def sandbox_calls():
        return [call for call in session.tools.calls if call[2] == PYTHON_TOOL_NAME]

    assert len(sandbox_calls()) == 3  # all waves of attempt 1

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
    assert len(sandbox_calls()) == 4
    sandbox_events = [
        event for event in tool_events(session)
        if event.payload["tool"] == PYTHON_TOOL_NAME
    ]
    assert [event.payload["status"] for event in sandbox_events] == [
        "failed",
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
