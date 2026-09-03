"""Task run engine: executes one state-machine step at a time.

Durability contract: every event is emitted to the sink (which must persist
it) BEFORE it is applied to the snapshot. Consumers therefore never observe a
state the log cannot reproduce, and a crash between emit and apply is healed
by replaying the log.

Concurrency contract: one run is advanced by at most one caller at a time.
That mutual exclusion is owned by the worker's lease (backend/worker), not
by this engine.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any

from .models import AgentEvent, EventType, StepStatus, TaskRunSnapshot
from .nodes import NodeContext, NodeRegistry, NodeResult
from .ports import Clock, EventSink, IdGenerator, NodeServices
from .reducer import apply_event
from .states import TaskState, WORK_STATES, next_work_state


@dataclass
class AdvanceOutcome:
    """Result of one ``advance`` call."""

    status: str
    snapshot: TaskRunSnapshot
    events: list[AgentEvent] = field(default_factory=list)

    ADVANCED = "advanced"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEW_REQUESTED = "review_requested"
    CANCELLED = "cancelled"
    IDLE = "idle"


class TaskRunEngine:
    def __init__(
        self,
        sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        nodes: NodeRegistry,
        services: NodeServices,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._ids = ids
        self._nodes = nodes
        self._services = services

    # -- event plumbing ----------------------------------------------------

    def _record(
        self,
        snapshot: TaskRunSnapshot,
        event_type: EventType,
        payload: dict[str, Any],
        collected: list[AgentEvent],
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=snapshot.run_id,
            seq=snapshot.last_event_seq + 1,
            event_type=event_type,
            payload=payload,
            created_at=self._clock.now_iso(),
        )
        self._sink.emit(event)  # persist first ...
        apply_event(snapshot, event)  # ... then advance the materialized state
        collected.append(event)
        return event

    def record_external(
        self,
        snapshot: TaskRunSnapshot,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> AgentEvent:
        """Record an event produced OUTSIDE the engine loop (tool calls).

        Tool invokers must not write to the sink directly: seq allocation and
        snapshot application have to stay on the single emit→apply path or the
        engine would hand out duplicate sequence numbers afterwards. Only
        observability events are accepted; lifecycle events remain engine-only.
        """
        if event_type is not EventType.TOOL_CALLED:
            raise ValueError(f"external recording not allowed for {event_type}")
        return self._record(snapshot, event_type, payload, [])

    # -- lifecycle ---------------------------------------------------------

    def create_run(
        self,
        project_id: str,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> tuple[TaskRunSnapshot, list[AgentEvent]]:
        snapshot = TaskRunSnapshot(
            run_id=run_id or self._ids.new_id("run"), project_id=project_id
        )
        events: list[AgentEvent] = []
        self._record(
            snapshot,
            EventType.RUN_CREATED,
            {"project_id": project_id, "inputs": inputs or {}},
            events,
        )
        return snapshot, events

    def advance(self, snapshot: TaskRunSnapshot) -> AdvanceOutcome:
        """Execute at most one step. Callers loop until a non-ADVANCED status."""
        events: list[AgentEvent] = []

        if snapshot.is_terminal:
            return AdvanceOutcome(AdvanceOutcome.IDLE, snapshot, events)
        if snapshot.cancel_requested:
            self._record(
                snapshot,
                EventType.RUN_FAILED,
                {"failed_state": snapshot.state.value, "error": "cancelled by user"},
                events,
            )
            return AdvanceOutcome(AdvanceOutcome.CANCELLED, snapshot, events)
        if snapshot.state is TaskState.NEEDS_REVIEW or snapshot.paused:
            return AdvanceOutcome(AdvanceOutcome.IDLE, snapshot, events)

        target = self._select_target(snapshot)
        if target is TaskState.COMPLETED:
            self._record(snapshot, EventType.RUN_COMPLETED, {}, events)
            return AdvanceOutcome(AdvanceOutcome.COMPLETED, snapshot, events)

        if target is not snapshot.state:
            self._record(
                snapshot,
                EventType.STATE_CHANGED,
                {"from": snapshot.state.value, "to": target.value},
                events,
            )

        step_id = self._ids.new_id("step")
        attempt = snapshot.attempts_for(target) + 1
        self._record(
            snapshot,
            EventType.STEP_STARTED,
            {"step_id": step_id, "state": target.value, "attempt": attempt},
            events,
        )

        result = self._run_node(snapshot, target, step_id, attempt)

        if result.status == NodeResult.SUCCEEDED or result.status == NodeResult.NEEDS_REVIEW:
            for artifact in result.artifacts:
                self._record(
                    snapshot,
                    EventType.ARTIFACT_PRODUCED,
                    {"step_id": step_id, "artifact": artifact.to_dict()},
                    events,
                )
            self._record(
                snapshot,
                EventType.STEP_SUCCEEDED,
                {
                    "step_id": step_id,
                    "outputs": result.outputs,
                    "metrics": result.metrics,
                    "artifact_ids": [a.artifact_id for a in result.artifacts],
                },
                events,
            )
            if result.status == NodeResult.NEEDS_REVIEW:
                review_payload: dict[str, Any] = {
                    "reason": result.review_reason or "review requested",
                    "requested_by_step": step_id,
                    "resume_state": target.value,
                }
                if result.review_meta:
                    # 闸门元数据（闸门号/选项/证据）只在节点声明时携带；
                    # 缺省时载荷与历史版本逐字节一致（金轨迹稳定）。
                    review_payload["gate"] = dict(result.review_meta)
                self._record(
                    snapshot,
                    EventType.REVIEW_REQUESTED,
                    review_payload,
                    events,
                )
                return AdvanceOutcome(AdvanceOutcome.REVIEW_REQUESTED, snapshot, events)
            return AdvanceOutcome(AdvanceOutcome.ADVANCED, snapshot, events)

        # failure path
        self._record(
            snapshot,
            EventType.STEP_FAILED,
            {"step_id": step_id, "error": result.error or "step failed"},
            events,
        )
        self._record(
            snapshot,
            EventType.RUN_FAILED,
            {"failed_state": target.value, "error": result.error or "step failed"},
            events,
        )
        return AdvanceOutcome(AdvanceOutcome.FAILED, snapshot, events)

    def run_until_blocked(
        self, snapshot: TaskRunSnapshot, max_steps: int = 32
    ) -> AdvanceOutcome:
        """Drive a run until it completes, fails, pauses or asks for review.

        ``max_steps`` bounds the loop so a misbehaving node registry cannot
        spin forever (loop-budget rule for agent runtimes).
        """
        outcome = AdvanceOutcome(AdvanceOutcome.IDLE, snapshot)
        for _ in range(max_steps):
            outcome = self.advance(snapshot)
            if outcome.status != AdvanceOutcome.ADVANCED:
                return outcome
        return outcome

    def heal_interrupted(self, snapshot: TaskRunSnapshot) -> list[AgentEvent]:
        """Fail dangling RUNNING steps left behind by a crashed executor.

        Callers must hold the run's mutual exclusion (worker lease) so a
        RUNNING step provably has no live executor. The run itself stays in
        its work state; the next ``advance`` re-runs the step as attempt+1.
        """
        events: list[AgentEvent] = []
        for step in snapshot.steps:
            if step.status is StepStatus.RUNNING:
                self._record(
                    snapshot,
                    EventType.STEP_FAILED,
                    {
                        "step_id": step.step_id,
                        "error": "interrupted: executor lost before completion",
                    },
                    events,
                )
        return events

    # -- control actions ---------------------------------------------------

    def request_pause(self, snapshot: TaskRunSnapshot) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if not snapshot.is_terminal and not snapshot.paused:
            self._record(snapshot, EventType.RUN_PAUSED, {}, events)
        return events

    def resume(self, snapshot: TaskRunSnapshot) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if snapshot.paused:
            self._record(snapshot, EventType.RUN_RESUMED, {}, events)
        return events

    def request_cancel(self, snapshot: TaskRunSnapshot) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        if not snapshot.is_terminal and not snapshot.cancel_requested:
            self._record(snapshot, EventType.RUN_CANCELLED, {}, events)
        return events

    def resolve_review(
        self,
        snapshot: TaskRunSnapshot,
        approved: bool,
        reason: str | None = None,
        resume_state: TaskState | None = None,
        rerun: bool | None = None,
    ) -> list[AgentEvent]:
        """Resolve a pending human review.

        Default is forward-only, unchanged: approval resumes the requesting
        state (whose step already succeeded), so the next ``advance`` moves on
        to the following stage; rejection fails the run (retry can re-enter).

        Sending the run BACKWARDS is now supported in two cases (ADR-0013):
        resolving a revision review, or passing ``resume_state`` to override
        the restart point. Both mean the target's previous attempt already
        SUCCEEDED, which ``_select_target`` would otherwise read as "done,
        move on" — so the event carries ``rerun`` to force a fresh pass. The
        key is omitted when it is not needed, keeping forward-only payloads
        byte-identical to earlier versions (golden traces stay stable).

        ``rerun`` makes the intent explicit when inference cannot: a gate may
        offer "redo THIS stage" (G4's 退回修改 restarts PAPER_WRITING, the very
        state that raised the gate), which looks exactly like a forward
        approval to the inference above. Callers resolving a ``redo:<STATE>``
        option pass ``rerun=True``; ``None`` keeps the inferred behaviour.
        """
        if snapshot.state is not TaskState.NEEDS_REVIEW or snapshot.review is None:
            raise ValueError("no pending review to resolve")
        review = snapshot.review
        resume = resume_state or review.resume_state
        if approved and resume not in WORK_STATES:
            raise ValueError(f"{resume.value} is not a work state")
        payload: dict[str, Any] = {
            "approved": approved,
            "resume_state": resume.value,
            "reason": reason,
        }
        if rerun is None:
            rerun = review.revision_round > 0 or resume is not review.resume_state
        if approved and rerun:
            payload["rerun"] = True
        if review.revision_round > 0:
            # Which gate this was cannot be recovered from the pair of states
            # alone, and the projection layer needs to know: declining a
            # revision restores COMPLETED, declining a node-raised gate fails
            # the run. Omitted for node-raised gates, so their payloads stay
            # byte-identical to earlier versions.
            payload["revision_round"] = review.revision_round
        events: list[AgentEvent] = []
        self._record(snapshot, EventType.REVIEW_RESOLVED, payload, events)
        return events

    def request_revision(
        self,
        snapshot: TaskRunSnapshot,
        target_state: TaskState,
        reason: str,
        note_id: str | None = None,
    ) -> list[AgentEvent]:
        """Open a revision round on a finished run (ADR-0013).

        A completed run is not a dead end: the user can ask for changes and
        have the work actually redone. This suspends the run into the review
        gate carrying the stage they want to restart from, so the restart
        point is confirmed by a human before anything is re-run — redoing from
        problem analysis and redoing from paper writing differ by an order of
        magnitude in cost. Approving lands the run on ``target_state`` and
        re-runs it and every stage after it.

        ``note_id`` links the round to the run note holding the user's actual
        wording, which the projection layer shows as the gate's evidence.
        """
        if snapshot.state is not TaskState.COMPLETED:
            raise ValueError("revision rounds are only valid for a completed run")
        if target_state not in WORK_STATES:
            raise ValueError(f"{target_state.value} is not a work state")
        payload: dict[str, Any] = {
            "target_state": target_state.value,
            "reason": reason,
            "round": snapshot.revision_round + 1,
        }
        if note_id is not None:
            payload["note_id"] = note_id
        events: list[AgentEvent] = []
        self._record(snapshot, EventType.REVISION_REQUESTED, payload, events)
        return events

    def retry(self, snapshot: TaskRunSnapshot) -> list[AgentEvent]:
        if snapshot.state is not TaskState.FAILED or snapshot.failure is None:
            raise ValueError("retry is only valid for a failed run")
        events: list[AgentEvent] = []
        self._record(
            snapshot,
            EventType.RUN_RETRIED,
            {"target_state": snapshot.failure.state.value},
            events,
        )
        return events

    # -- internals ----------------------------------------------------------

    def _select_target(self, snapshot: TaskRunSnapshot) -> TaskState:
        if snapshot.state is TaskState.CREATED:
            return next_work_state(TaskState.CREATED)  # type: ignore[return-value]
        if snapshot.state in WORK_STATES:
            if snapshot.force_rerun:
                # RUN_RETRIED 要求重跑当前状态（覆盖"最近步骤已 SUCCEEDED 则顺延"的规则）
                snapshot.force_rerun = False
                return snapshot.state
            latest = self._latest_step(snapshot, snapshot.state)
            if latest is not None and latest.status.value == "SUCCEEDED":
                return next_work_state(snapshot.state)  # type: ignore[return-value]
            # No step yet (resumed via retry/review) or last attempt failed:
            # re-run the current state.
            return snapshot.state
        raise RuntimeError(f"advance called in unexpected state {snapshot.state}")

    @staticmethod
    def _latest_step(snapshot: TaskRunSnapshot, state: TaskState):
        for step in reversed(snapshot.steps):
            if step.state is state:
                return step
        return None

    def _run_node(
        self, snapshot: TaskRunSnapshot, state: TaskState, step_id: str, attempt: int
    ) -> NodeResult:
        node = self._nodes.get(state)
        if node is None:
            return NodeResult.failed(f"no node registered for state {state.value}")
        ctx = NodeContext.for_step(snapshot, state, step_id, attempt)
        try:
            return node.run(ctx, self._services)
        except Exception:  # noqa: BLE001 - a node crash must fail the step, not the loop
            return NodeResult.failed(
                "node raised:\n" + traceback.format_exc(limit=8)
            )
