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
from .states import TaskState, assert_transition


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
    EventType.RUN_COMPLETED: _on_run_completed,
    EventType.RUN_FAILED: _on_run_failed,
    EventType.TOOL_CALLED: _on_tool_called,
}
