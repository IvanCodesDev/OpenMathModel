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
    CANNED_VALIDATION_CODE,
    FULL_CHAIN_CHAT_SEQUENCE,
    FULL_CHAIN_GOLDEN_EVENT_TYPES,
    FULL_CHAIN_METRICS,
    FULL_CHAIN_PROMPT_SEQUENCE,
    FULL_CHAIN_ROBUSTNESS_CHECKS,
    build_full_chain_session,
    robustness_success,
    sandbox_failure,
    sandbox_success,
)
from omm_agent_skills import (
    EXPERIMENT_SCRIPT_PATH,
    G3_ACCEPT_OPTION_ID,
    G4_CONFIRM_OPTION_ID,
    PYTHON_TOOL_NAME,
)


def drive_through_review(session):
    """run_until_blocked → approve the plan gate → run_until_blocked."""
    outcome = session.engine.run_until_blocked(session.snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert session.snapshot.review is not None
    assert session.snapshot.review.resume_state is TaskState.MODEL_PLANNING
    session.engine.resolve_review(session.snapshot, approved=True, reason="采用方案 A")
    return session.engine.run_until_blocked(session.snapshot)


def confirm_delivery(session, outcome):
    """G4 定稿交付闸门（必停）：论文发布后停在 PAPER_WRITING，确认交付 → COMPLETED。"""
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert session.snapshot.review is not None
    assert session.snapshot.review.resume_state is TaskState.PAPER_WRITING
    session.engine.resolve_review(session.snapshot, approved=True, reason=G4_CONFIRM_OPTION_ID)
    return session.engine.run_until_blocked(session.snapshot)


def g4_gate_event(session):
    gates = [
        event for event in session.sink.events
        if event.event_type is EventType.REVIEW_REQUESTED
        and (event.payload.get("gate") or {}).get("gate") == "G4"
    ]
    return gates[-1]


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


def experiment_step_ids(session):
    return {step.step_id for step in steps_for(session, TaskState.EXPERIMENTING)}


def experiment_sandbox_calls(session):
    """实验步骤的 python_run 调用（验证阶段的稳健性复跑也走 python_run，要按步骤过滤）。"""
    ids = experiment_step_ids(session)
    return [
        call for call in session.tools.calls
        if call[2] == PYTHON_TOOL_NAME and call[1] in ids
    ]


def experiment_sandbox_events(session):
    ids = experiment_step_ids(session)
    return [
        event for event in tool_events(session)
        if event.payload["tool"] == PYTHON_TOOL_NAME and event.payload["step_id"] in ids
    ]


def _user_prompt(chat_call):
    """沙盒会话的 user 段：目标 / 任务说明 / 种子 / 验收 / 修复反馈。"""
    return next(m["content"] for m in chat_call.messages if m["role"] == "user")


# -- 1. happy path -------------------------------------------------------------


@pytest.fixture()
def completed_session():
    session = build_full_chain_session()
    outcome = confirm_delivery(session, drive_through_review(session))
    assert outcome.status == AdvanceOutcome.COMPLETED
    return session


def test_happy_path_all_six_stages_succeed(completed_session):
    snapshot = completed_session.snapshot

    assert snapshot.state is TaskState.COMPLETED
    assert [step.state for step in snapshot.steps] == list(WORK_SEQUENCE)
    assert all(step.status is StepStatus.SUCCEEDED for step in snapshot.steps)
    # G4 定稿闸门：论文发布后必停，卡片带数字冻结清单与审计发现的统计；确认进台账
    gate = g4_gate_event(completed_session).payload["gate"]
    assert [option["id"] for option in gate["options"]] == [
        G4_CONFIRM_OPTION_ID, "redo:PAPER_WRITING",
    ]
    assert [o["id"] for o in gate["options"] if o.get("recommended")] == [G4_CONFIRM_OPTION_ID]
    assert gate["impact"]["audit_findings_total"] == 0
    assert gate["impact"]["frozen_numbers_total"] >= len(FULL_CHAIN_METRICS)
    assert snapshot.review_decisions[TaskState.PAPER_WRITING.value] == G4_CONFIRM_OPTION_ID
    paper = snapshot.outputs[TaskState.PAPER_WRITING.value]
    assert {e["id"] for e in paper["frozen_numbers"]} >= {
        f"metrics.{name}" for name in FULL_CHAIN_METRICS
    }
    assert paper["audit_findings"] == []

    # Outputs accumulated for every work stage, with real node content.
    assert set(snapshot.outputs) == {state.value for state in WORK_SEQUENCE}
    assert snapshot.outputs[TaskState.PROBLEM_ANALYSIS.value]["problem_type"] == "预测+优化"
    assert "历史运量" in snapshot.outputs[TaskState.DATA_PREPARATION.value]["profile_summary"]
    assert snapshot.outputs[TaskState.MODEL_PLANNING.value]["recommended_plan_id"] == "A"
    assert snapshot.outputs[TaskState.EXPERIMENTING.value]["metrics"] == FULL_CHAIN_METRICS
    assert snapshot.outputs[TaskState.EXPERIMENTING.value]["script_path"] == EXPERIMENT_SCRIPT_PATH
    assert snapshot.outputs[TaskState.VALIDATING.value]["verdict"] == "pass"
    # 验证阶段真的复跑了：三项检查数字来自检验脚本的标记行，全过不惊动用户
    robustness = snapshot.outputs[TaskState.VALIDATING.value]["robustness"]
    assert robustness["executed"] is True and robustness["status"] == "passed"
    assert robustness["checks_total"] == 3 and robustness["checks_failed"] == 0
    assert [check["id"] for check in robustness["checks"]] == [
        check["id"] for check in FULL_CHAIN_ROBUSTNESS_CHECKS
    ]
    assert TaskState.VALIDATING.value not in snapshot.review_decisions, "全过不上 G3"
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
    # environment, runs code, re-lists for assertion evidence, then stages
    # the final script into the workspace; the validation stage finds that
    # script, reads it into the task card and re-runs it in the sandbox.
    all_tool_events = tool_events(completed_session)
    experiment_step = steps_for(completed_session, TaskState.EXPERIMENTING)[0]
    validation_step = steps_for(completed_session, TaskState.VALIDATING)[0]
    data_step = steps_for(completed_session, TaskState.DATA_PREPARATION)[0]

    by_step = {}
    for event in all_tool_events:
        by_step.setdefault(event.payload["step_id"], []).append(event.payload["tool"])
    assert by_step == {
        data_step.step_id: ["ws_list"],
        experiment_step.step_id: [
            "ws_list",
            "env_probe",
            PYTHON_TOOL_NAME,
            "ws_list",
            "ws_write",
        ],
        validation_step.step_id: [
            "ws_list",
            "ws_read",
            "env_probe",
            PYTHON_TOOL_NAME,
            "ws_list",
        ],
    }
    assert all(event.payload["status"] == "succeeded" for event in all_tool_events)

    # The invoker saw one python_run per sandbox stage, each carrying the code
    # the model wrote through the tool envelope; the validation stage read back
    # exactly the script the experiment stage staged.
    experiment_call, validation_call = [
        entry for entry in completed_session.tools.calls if entry[2] == PYTHON_TOOL_NAME
    ]
    assert experiment_call[:3] == (
        completed_session.snapshot.run_id,
        experiment_step.step_id,
        PYTHON_TOOL_NAME,
    )
    assert experiment_call[3]["code"] == CANNED_EXPERIMENT_CODE
    assert validation_call[1] == validation_step.step_id
    assert validation_call[3]["code"] == CANNED_VALIDATION_CODE
    assert completed_session.tools.workspace_texts == {
        EXPERIMENT_SCRIPT_PATH: CANNED_EXPERIMENT_CODE
    }
    robustness_card = next(
        m["content"]
        for call in completed_session.llm.chat_calls
        if call.label == "validating.sandbox"
        for m in call.messages
        if m["role"] == "system"
    )
    assert CANNED_EXPERIMENT_CODE in robustness_card
    assert "长周期外推失真" in robustness_card, "评审风险进复跑任务卡"


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

    # The validation stage publishes its robustness-check script the same way.
    validation_step = steps_for(completed_session, TaskState.VALIDATING)[0]
    (checks_ref,) = validation_step.artifacts
    assert checks_ref.uri.endswith("validation_checks.py")
    assert checks_ref.kind == "code"
    assert blobs[checks_ref.uri].decode("utf-8") == CANNED_VALIDATION_CODE

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
    assert produced == [results_ref.uri, code_ref.uri, checks_ref.uri, paper_ref.uri]


# -- 2. replay consistency -------------------------------------------------------


def test_replay_rebuilds_identical_snapshot(completed_session):
    assert_replay_matches(completed_session)


# -- 3. experiment repair round ---------------------------------------------------


def test_experiment_repair_round_feeds_error_back_and_completes():
    session = build_full_chain_session(
        tool_runs=[sandbox_failure(), sandbox_success()]
    )

    outcome = confirm_delivery(session, drive_through_review(session))

    assert outcome.status == AdvanceOutcome.COMPLETED
    (experiment_step,) = steps_for(session, TaskState.EXPERIMENTING)
    assert experiment_step.status is StepStatus.SUCCEEDED
    # 两波修复，每波两次会话（工具轮 + 终答）；code_rounds 是真实沙箱运行次数。
    assert experiment_step.metrics == {
        "llm_attempts": 4,
        "code_rounds": 2,
        "waves": 2,
    }

    # Two experiment sandbox invocations: the failing one, then the regenerated
    # one (the validation stage's robustness re-run is a third python_run on
    # its own step and is filtered out here).
    sandbox_calls = experiment_sandbox_calls(session)
    assert [call[2] for call in sandbox_calls] == [PYTHON_TOOL_NAME] * 2
    recorded = experiment_sandbox_events(session)
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
    outcome = confirm_delivery(session, engine.run_until_blocked(snapshot))
    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED

    # Trajectory shape: two plan gates + the delivery gate, three resolutions, one explicit retry.
    types = [event.event_type for event in session.sink.events]
    assert types.count(EventType.REVIEW_REQUESTED) == 3
    assert types.count(EventType.REVIEW_RESOLVED) == 3
    assert types.count(EventType.RUN_RETRIED) == 1

    assert_replay_matches(session)


# -- 5. experiment failure + retry recovery ---------------------------------------


def test_experiment_double_failure_recovers_within_single_attempt():
    # 波次 3 的核心收益：修复波再引入新 bug（真实案例 e896 的死法）不再
    # 让整个任务报废，第三波自愈后本次尝试直接成功。
    session = build_full_chain_session(
        tool_runs=[sandbox_failure(), sandbox_failure(), sandbox_success()]
    )

    outcome = confirm_delivery(session, drive_through_review(session))

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

    assert len(experiment_sandbox_calls(session)) == 3  # all waves of attempt 1

    engine.retry(snapshot)
    assert snapshot.state is TaskState.EXPERIMENTING

    outcome = confirm_delivery(session, engine.run_until_blocked(snapshot))
    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED

    experiment_steps = steps_for(session, TaskState.EXPERIMENTING)
    assert [step.attempt for step in experiment_steps] == [1, 2]
    assert [step.status for step in experiment_steps] == [
        StepStatus.FAILED,
        StepStatus.SUCCEEDED,
    ]
    assert len(experiment_sandbox_calls(session)) == 4
    assert [event.payload["status"] for event in experiment_sandbox_events(session)] == [
        "failed",
        "failed",
        "failed",
        "succeeded",
    ]

    assert_replay_matches(session)


# -- 6. G3 result gate: robustness check fails --------------------------------------


def failing_robustness_run():
    """三项检查中 bootstrap 稳定性未过（<50%）：上 G3，推荐「接受并记录局限」。"""
    checks = [dict(check) for check in FULL_CHAIN_ROBUSTNESS_CHECKS]
    checks[1].update(passed=False, value=0.42, detail="重采样 RMSE 波动 42%，超出阈值")
    return robustness_success(checks=checks)


def g3_gate_event(session):
    gates = [
        event for event in session.sink.events
        if event.event_type is EventType.REVIEW_REQUESTED
        and (event.payload.get("gate") or {}).get("gate") == "G3"
    ]
    return gates[-1]


def test_g3_gate_stops_on_failed_check_and_accept_carries_limitation_into_paper():
    session = build_full_chain_session(validation_run=failing_robustness_run())
    engine, snapshot = session.engine, session.snapshot

    outcome = drive_through_review(session)

    # 验证阶段停在 G3：请求确认的是 VALIDATING，载荷带闸门元数据与推荐项
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert snapshot.review is not None
    assert snapshot.review.resume_state is TaskState.VALIDATING
    assert "3 项中 1 项未通过" in snapshot.review.reason
    gate = g3_gate_event(session).payload["gate"]
    assert [option["id"] for option in gate["options"]] == [
        G3_ACCEPT_OPTION_ID,
        "redo:EXPERIMENTING",
        "redo:MODEL_PLANNING",
    ]
    assert [o["id"] for o in gate["options"] if o.get("recommended")] == [G3_ACCEPT_OPTION_ID]
    assert gate["impact"]["failed"][0]["id"] == "bootstrap_stability"
    # 闸门未拍板前检验产出已落库（含未通过项），G1 之外没有别的门
    assert snapshot.outputs[TaskState.VALIDATING.value]["robustness"]["checks_failed"] == 1
    (validation_step,) = steps_for(session, TaskState.VALIDATING)
    assert validation_step.status is StepStatus.SUCCEEDED

    # 用户接受并记录局限：决策进台账，论文材料带上稳健性结论与「不得淡化」纪律
    engine.resolve_review(snapshot, approved=True, reason=G3_ACCEPT_OPTION_ID)
    outcome = confirm_delivery(session, engine.run_until_blocked(snapshot))

    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.review_decisions[TaskState.VALIDATING.value] == G3_ACCEPT_OPTION_ID
    outline_call = next(
        call for call in session.llm.calls if call.prompt_id == "paper_outline.default"
    )
    material = outline_call.variables["validation_summary"]
    assert "bootstrap_stability" in material and "value 0.42" in material
    assert "接受并记录局限" in material and "不得淡化" in material
    # 未通过项的实测值 / 阈值进了数字冻结清单：论文只能原样引用，不能改写
    frozen = {
        e["id"]: e["value"]
        for e in snapshot.outputs[TaskState.PAPER_WRITING.value]["frozen_numbers"]
    }
    assert frozen["robustness.bootstrap_stability.value"] == 0.42
    assert "robustness.bootstrap_stability.value" in outline_call.variables["frozen_numbers"]
    assert [step.state for step in snapshot.steps] == list(WORK_SEQUENCE)

    types = [event.event_type for event in session.sink.events]
    assert types.count(EventType.REVIEW_REQUESTED) == 3  # G1 + G3 + G4
    assert types.count(EventType.REVIEW_RESOLVED) == 3
    assert_replay_matches(session)


def test_g3_redo_experiment_reruns_experiment_and_validation_then_completes():
    """G3 选「重做实验」：复用修订门的回退语义——实验与验证各跑第二趟，
    第一趟的产出被丢弃，闸门再次弹出后接受即完成。"""
    session = build_full_chain_session(validation_run=failing_robustness_run())
    engine, snapshot = session.engine, session.snapshot

    outcome = drive_through_review(session)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert snapshot.review.resume_state is TaskState.VALIDATING

    engine.resolve_review(
        snapshot,
        approved=True,
        reason="redo:EXPERIMENTING",
        resume_state=TaskState.EXPERIMENTING,
    )
    assert snapshot.state is TaskState.EXPERIMENTING
    assert snapshot.force_rerun is True
    # 回退丢弃实验及其下游的旧产出（串轮防线），上游原样保留
    assert TaskState.EXPERIMENTING.value not in snapshot.outputs
    assert TaskState.VALIDATING.value not in snapshot.outputs
    assert TaskState.MODEL_PLANNING.value in snapshot.outputs
    # G1 的决策台账（上游）保留，回退起点及下游的闸门决策一并清掉
    assert set(snapshot.review_decisions) == {TaskState.MODEL_PLANNING.value}

    # 同一份失败脚本 → 第二趟仍上 G3；这次接受
    outcome = engine.run_until_blocked(snapshot)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert snapshot.review.resume_state is TaskState.VALIDATING
    assert [step.attempt for step in steps_for(session, TaskState.EXPERIMENTING)] == [1, 2]
    assert [step.attempt for step in steps_for(session, TaskState.VALIDATING)] == [1, 2]
    assert len(experiment_sandbox_calls(session)) == 2

    engine.resolve_review(snapshot, approved=True, reason=G3_ACCEPT_OPTION_ID)
    outcome = confirm_delivery(session, engine.run_until_blocked(snapshot))

    assert outcome.status == AdvanceOutcome.COMPLETED
    assert snapshot.state is TaskState.COMPLETED
    types = [event.event_type for event in session.sink.events]
    assert types.count(EventType.REVIEW_REQUESTED) == 4  # G1 + G3 ×2 + G4
    assert types.count(EventType.RUN_RETRIED) == 0, "回退不是 retry，是审批内的回退"
    assert_replay_matches(session)


def test_g4_redo_rewrites_paper_from_scratch_and_gates_again():
    """G4 选「退回修改」：复用修订门的回退语义——论文阶段跑第二趟（总编重新
    规划、逐章重写），第一趟产出被丢弃，闸门再次弹出后确认即完成。"""
    session = build_full_chain_session()
    engine, snapshot = session.engine, session.snapshot

    outcome = drive_through_review(session)
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED
    assert snapshot.review.resume_state is TaskState.PAPER_WRITING
    first_paper_calls = len([c for c in session.llm.calls if c.prompt_id.startswith("paper_")])

    # 重做的正是提出闸门的阶段：引擎按状态推断不出「要重跑」，控制面显式传 rerun
    engine.resolve_review(
        snapshot,
        approved=True,
        reason="redo:PAPER_WRITING",
        resume_state=TaskState.PAPER_WRITING,
        rerun=True,
    )
    assert snapshot.state is TaskState.PAPER_WRITING
    assert snapshot.force_rerun is True
    assert TaskState.PAPER_WRITING.value not in snapshot.outputs
    assert TaskState.VALIDATING.value in snapshot.outputs, "上游产出原样保留"

    outcome = confirm_delivery(session, engine.run_until_blocked(snapshot))

    assert outcome.status == AdvanceOutcome.COMPLETED
    assert [step.attempt for step in steps_for(session, TaskState.PAPER_WRITING)] == [1, 2]
    # 第二趟整篇重写：总编 + 三章 + 统稿又各调一次（不是复用第一趟的章节）
    second_paper_calls = len([c for c in session.llm.calls if c.prompt_id.startswith("paper_")])
    assert second_paper_calls == first_paper_calls * 2
    types = [event.event_type for event in session.sink.events]
    assert types.count(EventType.REVIEW_REQUESTED) == 3  # G1 + G4 ×2
    assert_replay_matches(session)


# -- assembly knobs ----------------------------------------------------------------


def test_unattended_full_chain_completes_without_review():
    session = build_full_chain_session(require_confirmation=False)

    outcome = session.engine.run_until_blocked(session.snapshot)

    assert outcome.status == AdvanceOutcome.COMPLETED
    types = [event.event_type for event in session.sink.events]
    assert EventType.REVIEW_REQUESTED not in types
    assert_replay_matches(session)
