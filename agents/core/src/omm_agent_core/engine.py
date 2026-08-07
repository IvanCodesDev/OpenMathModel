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
                self._record(
                    snapshot,
                    EventType.REVIEW_REQUESTED,
                    {
                        "reason": result.review_reason or "review requested",
                        "requested_by_step": step_id,
                        "resume_state": target.value,
                    },
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
        self, snapshot: TaskRunSnapshot, approved: bool, reason: str | None = None
    ) -> list[AgentEvent]:
        """Resolve a pending human review.

        MVP semantics are forward-only: approval resumes the requesting state
        (whose step already succeeded), so the next ``advance`` moves on to
        the following stage; rejection fails the run (retry can re-enter).
        Sending a run BACKWARDS to redo earlier stages is intentionally out of
        scope until pass-aware step tracking exists.
        """
        if snapshot.state is not TaskState.NEEDS_REVIEW or snapshot.review is None:
            raise ValueError("no pending review to resolve")
        events: list[AgentEvent] = []
        self._record(
            snapshot,
            EventType.REVIEW_RESOLVED,
            {
                "approved": approved,
                "resume_state": snapshot.review.resume_state.value,
                "reason": reason,
            },
            events,
        )
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
