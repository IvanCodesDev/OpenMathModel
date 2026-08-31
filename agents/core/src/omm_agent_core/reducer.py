"""Single event reducer shared by the engine and by replay/recovery.

The event log is the only source of truth for a run. The engine mutates a
snapshot exclusively by applying events through ``apply_event`` — the same
function replay uses — so "live" state and "recovered" state cannot diverge
by construction.
"""

from __future__ import annotations

from typing import Iterable

from .models import (
    AgentEvent,
    ArtifactRef,
    EventType,
    Failure,
    ReviewRequest,
    StepRun,
    StepStatus,
    TaskRunSnapshot,
)
from .states import WORK_SEQUENCE, WORK_STATES, TaskState, assert_transition


class SequenceError(Exception):
    """Event does not continue the snapshot's sequence (gap, dupe or reorder)."""


class ReduceError(Exception):
    """Event is structurally valid but illegal for the current snapshot."""


def apply_event(snapshot: TaskRunSnapshot, event: AgentEvent) -> TaskRunSnapshot:
    """Apply one event in place. Sequence must be strictly last_event_seq + 1.

    Deduplication of at-least-once deliveries is a SINK concern; by the time
    events reach the reducer they must form a gapless ordered stream.
    """
    if event.run_id != snapshot.run_id:
        raise ReduceError(
            f"event run_id {event.run_id!r} does not match snapshot {snapshot.run_id!r}"
        )
    if event.seq != snapshot.last_event_seq + 1:
        raise SequenceError(
            f"expected seq {snapshot.last_event_seq + 1}, got {event.seq}"
        )

    handler = _HANDLERS.get(event.event_type)
    if handler is None:
        raise ReduceError(f"no reducer for event type {event.event_type}")
    handler(snapshot, event)

    snapshot.last_event_seq = event.seq
    snapshot.updated_at = event.created_at
    return snapshot


def replay_events(
    run_id: str, project_id: str, events: Iterable[AgentEvent]
) -> TaskRunSnapshot:
    """Rebuild a snapshot from scratch by folding the event log."""
    snapshot = TaskRunSnapshot(run_id=run_id, project_id=project_id)
    for event in events:
        apply_event(snapshot, event)
    return snapshot


# --------------------------------------------------------------------------
# Per-event handlers
# --------------------------------------------------------------------------


def _require_step(snapshot: TaskRunSnapshot, step_id: str) -> StepRun:
    step = snapshot.find_step(step_id)
    if step is None:
        raise ReduceError(f"unknown step_id {step_id!r}")
    return step


def _on_run_created(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    if snapshot.last_event_seq != 0:
        raise ReduceError("RUN_CREATED must be the first event of a run")
    snapshot.project_id = event.payload["project_id"]
    snapshot.inputs = dict(event.payload.get("inputs") or {})
    snapshot.state = TaskState.CREATED
    snapshot.created_at = event.created_at


def _on_state_changed(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    target = TaskState(event.payload["to"])
    assert_transition(snapshot.state, target)
    snapshot.state = target


def _on_step_started(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    payload = event.payload
    state = TaskState(payload["state"])
    if state is not snapshot.state:
        raise ReduceError(
            f"step for state {state.value} started while run is in {snapshot.state.value}"
        )
    # 新步骤开始意味着 retry 已被兑现；清掉重跑标志，重放才能收敛（否则每次
    # 重放 RUN_RETRIED 都会重新置位，导致该状态被无限重跑）。
    snapshot.force_rerun = False
    # 该状态重新执行时，其历史闸门决策随旧产出一并失效（新产出需要新决策）。
    snapshot.review_decisions.pop(state.value, None)
    snapshot.steps.append(
        StepRun(
            step_id=payload["step_id"],
            state=state,
            attempt=int(payload["attempt"]),
            status=StepStatus.RUNNING,
            started_at=event.created_at,
        )
    )


def _on_artifact_produced(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    step = _require_step(snapshot, event.payload["step_id"])
    step.artifacts.append(ArtifactRef.from_dict(event.payload["artifact"]))


def _on_step_succeeded(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    payload = event.payload
    step = _require_step(snapshot, payload["step_id"])
    step.status = StepStatus.SUCCEEDED
    step.ended_at = event.created_at
    step.outputs = dict(payload.get("outputs") or {})
    step.metrics = dict(payload.get("metrics") or {})
    bucket = snapshot.outputs.setdefault(step.state.value, {})
    bucket.update(step.outputs)


def _on_step_failed(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    payload = event.payload
    step = _require_step(snapshot, payload["step_id"])
    step.status = StepStatus.FAILED
    step.ended_at = event.created_at
    step.error = payload.get("error")


def _on_review_requested(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    payload = event.payload
    assert_transition(snapshot.state, TaskState.NEEDS_REVIEW)
    snapshot.review = ReviewRequest(
        reason=payload["reason"],
        requested_by_step=payload["requested_by_step"],
        resume_state=TaskState(payload["resume_state"]),
    )
    snapshot.state = TaskState.NEEDS_REVIEW


def _on_revision_requested(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    """修订回合（ADR-0013）：已完成的运行重新打开，挂进评审门等人确认起点。

    不直接落到目标阶段，是因为重做起点必须经人确认——从问题分析重做和从论文
    撰写重做，花费与耗时差一个数量级。``target_state`` 只是服务端算出的建议，
    审批时可以改选，最终以 REVIEW_RESOLVED 的 ``resume_state`` 为准。
    """
    payload = event.payload
    target = TaskState(payload["target_state"])
    if target not in WORK_STATES:
        raise ReduceError(f"{target.value} is not a work state")
    assert_transition(snapshot.state, TaskState.NEEDS_REVIEW)
    snapshot.revision_round = int(payload["round"])
    snapshot.review = ReviewRequest(
        reason=payload["reason"],
        # 修订由用户发起，没有触发步骤；空串把它与节点自提的闸门评审区分开。
        requested_by_step="",
        resume_state=target,
        revision_round=snapshot.revision_round,
    )
    snapshot.state = TaskState.NEEDS_REVIEW


def _discard_from(snapshot: TaskRunSnapshot, start: TaskState) -> None:
    """丢弃 ``start`` 及其下游各阶段的产出与闸门决策。

    这些阶段马上要重做，留着上一轮的产出会串轮：``snapshot.outputs`` 每段是
    ``bucket.update`` 合并写入的，新一轮少写某个键时旧值会残留；而且回退落地
    的瞬间下游各段仍挂着上一轮结果，节点读 ``prior_outputs`` 会读到过期数据。
    上游各段不动——它们本轮不重做，其成果正是这一轮返工的依据。

    历史不会因此丢失：``steps`` 逐趟保留，产出的版本化归档在 stage_outputs。
    """
    for state in WORK_SEQUENCE[WORK_SEQUENCE.index(start) :]:
        snapshot.outputs.pop(state.value, None)
        snapshot.review_decisions.pop(state.value, None)


def _on_review_resolved(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    # Both branches are folded into ONE event so a crash can never leave the
    # run stuck between "review cleared" and "state moved".
    if snapshot.state is not TaskState.NEEDS_REVIEW or snapshot.review is None:
        raise ReduceError("REVIEW_RESOLVED without a pending review")
    payload = event.payload
    review = snapshot.review
    snapshot.review = None
    if payload["approved"]:
        resume = TaskState(payload["resume_state"])
        assert_transition(TaskState.NEEDS_REVIEW, resume)
        snapshot.state = resume
        if payload.get("rerun"):
            # 回退重做（ADR-0013）：目标阶段最近一次步骤已 SUCCEEDED，
            # _select_target 会把它读成「做完了、往下走」，必须显式要求重跑，
            # 否则「退回建模方案」的实际效果是直接跳到实验，等于什么都没重做。
            snapshot.force_rerun = True
            _discard_from(snapshot, resume)
        else:
            # 决策台账的快照面：批准时 reason 携带所选 option_id（控制面
            # resolve_approval 的约定），按请求确认的状态记账，供下游节点读取
            # （如 G2 的「采用清洗结果/改用原始数据」）。缺 reason 的旧事件记
            # 兜底值，重放兼容。重做分支不记：那不是对产出的闸门决策，而是
            # 重做起点的选择，且该阶段一开步就会被 _on_step_started 清掉。
            snapshot.review_decisions[resume.value] = str(
                payload.get("reason") or "approve"
            )
    elif review.revision_round > 0:
        # 撤回修订：这个门是用户在已完成的运行上主动打开的，不批准就把运行
        # 放回它原本的终态。判成 FAILED 是错的——什么都没跑坏，用户只是
        # 改了主意。revision_round 不回退，它记的是发起过几轮。
        assert_transition(TaskState.NEEDS_REVIEW, TaskState.COMPLETED)
        snapshot.state = TaskState.COMPLETED
    else:
        snapshot.state = TaskState.FAILED
        snapshot.failure = Failure(
            state=review.resume_state,
            error=payload.get("reason") or "review rejected",
        )


def _on_run_paused(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    snapshot.paused = True


def _on_run_resumed(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    snapshot.paused = False


def _on_run_cancelled(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    snapshot.cancel_requested = True


def _on_run_retried(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    target = TaskState(event.payload["target_state"])
    assert_transition(snapshot.state, target)
    snapshot.state = target
    snapshot.failure = None
    snapshot.cancel_requested = False
    # retry 语义是"重新执行该状态"：即使其最近一次步骤已 SUCCEEDED（审批拒绝重做），
    # 下一次 advance 也必须重跑而不是顺延到下一阶段。
    snapshot.force_rerun = True


def _on_run_completed(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    assert_transition(snapshot.state, TaskState.COMPLETED)
    snapshot.state = TaskState.COMPLETED


def _on_run_failed(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    payload = event.payload
    assert_transition(snapshot.state, TaskState.FAILED)
    snapshot.state = TaskState.FAILED
    snapshot.failure = Failure(
        state=TaskState(payload["failed_state"]),
        error=payload.get("error") or "unknown failure",
    )


def _on_tool_called(snapshot: TaskRunSnapshot, event: AgentEvent) -> None:
    # Observability only: the tool ledger lives in the event log itself.
    return None


_HANDLERS = {
    EventType.RUN_CREATED: _on_run_created,
    EventType.STATE_CHANGED: _on_state_changed,
    EventType.STEP_STARTED: _on_step_started,
    EventType.ARTIFACT_PRODUCED: _on_artifact_produced,
    EventType.STEP_SUCCEEDED: _on_step_succeeded,
    EventType.STEP_FAILED: _on_step_failed,
    EventType.REVIEW_REQUESTED: _on_review_requested,
    EventType.REVIEW_RESOLVED: _on_review_resolved,
    EventType.RUN_PAUSED: _on_run_paused,
    EventType.RUN_RESUMED: _on_run_resumed,
    EventType.RUN_CANCELLED: _on_run_cancelled,
    EventType.RUN_RETRIED: _on_run_retried,
    EventType.REVISION_REQUESTED: _on_revision_requested,
    EventType.RUN_COMPLETED: _on_run_completed,
    EventType.RUN_FAILED: _on_run_failed,
    EventType.TOOL_CALLED: _on_tool_called,
}
