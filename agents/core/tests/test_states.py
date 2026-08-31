import pytest

from omm_agent_core.states import (
    TERMINAL_STATES,
    WORK_SEQUENCE,
    WORK_STATES,
    TaskState,
    TransitionError,
    assert_transition,
    can_transition,
    next_work_state,
)


def test_work_sequence_covers_all_work_states():
    assert set(WORK_SEQUENCE) == set(WORK_STATES)
    assert len(WORK_SEQUENCE) == len(set(WORK_SEQUENCE))


def test_happy_path_chain_is_legal():
    chain = [TaskState.CREATED, *WORK_SEQUENCE, TaskState.COMPLETED]
    for source, target in zip(chain, chain[1:]):
        assert can_transition(source, target), f"{source} -> {target}"


def test_next_work_state_walks_the_chain():
    assert next_work_state(TaskState.CREATED) is WORK_SEQUENCE[0]
    for index, state in enumerate(WORK_SEQUENCE[:-1]):
        assert next_work_state(state) is WORK_SEQUENCE[index + 1]
    assert next_work_state(WORK_SEQUENCE[-1]) is TaskState.COMPLETED
    assert next_work_state(TaskState.COMPLETED) is None
    assert next_work_state(TaskState.FAILED) is None


def test_no_skipping_forward():
    assert not can_transition(TaskState.PROBLEM_ANALYSIS, TaskState.MODEL_PLANNING)
    assert not can_transition(TaskState.CREATED, TaskState.EXPERIMENTING)
    assert not can_transition(TaskState.DATA_PREPARATION, TaskState.COMPLETED)


def test_work_states_can_fail_and_request_review():
    for state in WORK_STATES:
        assert can_transition(state, TaskState.FAILED)
        assert can_transition(state, TaskState.NEEDS_REVIEW)


def test_completed_only_reopens_through_the_review_gate():
    """跑完之后唯一的出边是评审门（ADR-0013 修订回合），别的一律封死。"""
    assert can_transition(TaskState.COMPLETED, TaskState.NEEDS_REVIEW)
    for target in TaskState:
        if target is TaskState.NEEDS_REVIEW:
            continue
        assert not can_transition(TaskState.COMPLETED, target)


def test_failed_only_retries_into_work_states():
    for target in WORK_STATES:
        assert can_transition(TaskState.FAILED, target)
    assert not can_transition(TaskState.FAILED, TaskState.COMPLETED)
    assert not can_transition(TaskState.FAILED, TaskState.NEEDS_REVIEW)
    assert not can_transition(TaskState.FAILED, TaskState.CREATED)


def test_review_resolution_targets():
    for target in WORK_STATES:
        assert can_transition(TaskState.NEEDS_REVIEW, target)
    assert can_transition(TaskState.NEEDS_REVIEW, TaskState.FAILED)
    # 回到 COMPLETED 只为「撤回修订请求」而存在（ADR-0013）；矩阵放行，
    # 由归约器保证节点自提的闸门永远走不到这条边（见 test_engine.py）。
    assert can_transition(TaskState.NEEDS_REVIEW, TaskState.COMPLETED)
    assert not can_transition(TaskState.NEEDS_REVIEW, TaskState.CREATED)


def test_self_transition_is_illegal():
    for state in TaskState:
        assert not can_transition(state, state)


def test_assert_transition_raises_with_context():
    with pytest.raises(TransitionError) as excinfo:
        assert_transition(TaskState.COMPLETED, TaskState.CREATED)
    assert excinfo.value.source is TaskState.COMPLETED
    assert excinfo.value.target is TaskState.CREATED


def test_terminal_states():
    assert TERMINAL_STATES == {TaskState.COMPLETED, TaskState.FAILED}
