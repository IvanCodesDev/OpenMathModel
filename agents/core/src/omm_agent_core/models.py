"""Domain models for a task run.

These are the engine's INTERNAL shapes. The cross-language source of truth is
``packages/contracts``; once its generated Python models land, an adapter maps
between the two. Keeping the core on plain dataclasses (stdlib only) preserves
the "framework-free, embeddable" constraint from agents/core/README.md.

Every model round-trips through ``to_dict``/``from_dict`` with JSON-safe
primitives so snapshots and events can be checkpointed to disk verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .states import TaskState


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EventType(str, Enum):
    RUN_CREATED = "RUN_CREATED"
    STATE_CHANGED = "STATE_CHANGED"
    STEP_STARTED = "STEP_STARTED"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_FAILED = "STEP_FAILED"
    ARTIFACT_PRODUCED = "ARTIFACT_PRODUCED"
    TOOL_CALLED = "TOOL_CALLED"
    REVIEW_REQUESTED = "REVIEW_REQUESTED"
    REVIEW_RESOLVED = "REVIEW_RESOLVED"
    RUN_PAUSED = "RUN_PAUSED"
    RUN_RESUMED = "RUN_RESUMED"
    RUN_CANCELLED = "RUN_CANCELLED"
    RUN_RETRIED = "RUN_RETRIED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


@dataclass(frozen=True)
class AgentEvent:
    """Uniform event envelope; the append-only truth of a run.

    ``seq`` is strictly monotonic per run and assigned by the engine.
    ``event_id`` is derived from (run_id, seq) so sinks can deduplicate
    at-least-once deliveries without extra state.
    """

    run_id: str
    seq: int
    event_type: EventType
    payload: dict[str, Any]
    created_at: str  # ISO-8601 UTC

    @property
    def event_id(self) -> str:
        return f"{self.run_id}:{self.seq}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "event_type": self.event_type.value,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "AgentEvent":
        return AgentEvent(
            run_id=raw["run_id"],
            seq=int(raw["seq"]),
            event_type=EventType(raw["event_type"]),
            payload=dict(raw.get("payload") or {}),
            created_at=raw["created_at"],
        )


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to a produced artifact (content lives in a store, not here)."""

    artifact_id: str
    kind: str
    uri: str
    sha256: str
    size: int
    media_type: str
    producer_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "uri": self.uri,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "producer_step": self.producer_step,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ArtifactRef":
        return ArtifactRef(
            artifact_id=raw["artifact_id"],
            kind=raw["kind"],
            uri=raw["uri"],
            sha256=raw["sha256"],
            size=int(raw["size"]),
            media_type=raw["media_type"],
            producer_step=raw["producer_step"],
        )


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool invocation, as seen by nodes."""

    status: str  # "succeeded" | "failed" | "timeout"
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    artifacts: tuple[ArtifactRef, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


@dataclass
class StepRun:
    """Execution record of one state-machine step (one attempt)."""

    step_id: str
    state: TaskState
    attempt: int
    status: StepStatus = StepStatus.PENDING
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "outputs": self.outputs,
            "metrics": self.metrics,
            "artifacts": [ref.to_dict() for ref in self.artifacts],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "StepRun":
        return StepRun(
            step_id=raw["step_id"],
            state=TaskState(raw["state"]),
            attempt=int(raw["attempt"]),
            status=StepStatus(raw["status"]),
            started_at=raw.get("started_at"),
            ended_at=raw.get("ended_at"),
            error=raw.get("error"),
            outputs=dict(raw.get("outputs") or {}),
            metrics=dict(raw.get("metrics") or {}),
            artifacts=[ArtifactRef.from_dict(item) for item in raw.get("artifacts") or []],
        )


@dataclass
class ReviewRequest:
    reason: str
    requested_by_step: str
    resume_state: TaskState
    #: 0 = 节点自己提的闸门评审（批准后从 resume_state 顺延，不重做）；
    #: >0 = 第 N 轮修订评审（ADR-0013），批准后回退到 resume_state 重做。
    #: 修订没有发起步骤，requested_by_step 为空串。
    revision_round: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "requested_by_step": self.requested_by_step,
            "resume_state": self.resume_state.value,
            "revision_round": self.revision_round,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ReviewRequest":
        return ReviewRequest(
            reason=raw["reason"],
            requested_by_step=raw["requested_by_step"],
            resume_state=TaskState(raw["resume_state"]),
            revision_round=int(raw.get("revision_round") or 0),
        )


@dataclass
class Failure:
    state: TaskState
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "error": self.error}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Failure":
        return Failure(state=TaskState(raw["state"]), error=raw["error"])


@dataclass
class TaskRunSnapshot:
    """Materialized view of a run, derived purely from its event log."""

    run_id: str
    project_id: str
    state: TaskState = TaskState.CREATED
    paused: bool = False
    cancel_requested: bool = False
    #: RUN_RETRIED 置位：下一次 advance 必须重跑当前状态（即使其最近一次步骤已
    #: SUCCEEDED，如审批拒绝后重做）。由事件日志确定性推导，故不参与 to_dict 序列化。
    force_rerun: bool = False
    #: 已开启的修订轮数（ADR-0013）：0 = 从未返工。跑完之后每接受一次「按这条
    #: 要求继续修改」就 +1，是按轮追加预算与「第 N 轮」展示的记账依据。
    revision_round: int = 0
    inputs: dict[str, Any] = field(default_factory=dict)
    #: Outputs accumulated per work state, keyed by state value.
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Approved review decisions keyed by the requesting state value; the
    #: value is the chosen option id (REVIEW_RESOLVED carries it as reason).
    #: Downstream nodes read e.g. the G2 data-gate choice from here instead
    #: of querying the control-plane approval rows (event log is the truth).
    review_decisions: dict[str, str] = field(default_factory=dict)
    steps: list[StepRun] = field(default_factory=list)
    review: ReviewRequest | None = None
    failure: Failure | None = None
    last_event_seq: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def attempts_for(self, state: TaskState) -> int:
        return sum(1 for step in self.steps if step.state is state)

    def find_step(self, step_id: str) -> StepRun | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    @property
    def is_terminal(self) -> bool:
        return self.state in (TaskState.COMPLETED, TaskState.FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "state": self.state.value,
            "paused": self.paused,
            "cancel_requested": self.cancel_requested,
            "revision_round": self.revision_round,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "review_decisions": self.review_decisions,
            "steps": [step.to_dict() for step in self.steps],
            "review": self.review.to_dict() if self.review else None,
            "failure": self.failure.to_dict() if self.failure else None,
            "last_event_seq": self.last_event_seq,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "TaskRunSnapshot":
        return TaskRunSnapshot(
            run_id=raw["run_id"],
            project_id=raw["project_id"],
            state=TaskState(raw["state"]),
            paused=bool(raw.get("paused", False)),
            cancel_requested=bool(raw.get("cancel_requested", False)),
            revision_round=int(raw.get("revision_round") or 0),
            inputs=dict(raw.get("inputs") or {}),
            outputs={key: dict(value) for key, value in (raw.get("outputs") or {}).items()},
            review_decisions={
                str(key): str(value)
                for key, value in (raw.get("review_decisions") or {}).items()
            },
            steps=[StepRun.from_dict(item) for item in raw.get("steps") or []],
            review=ReviewRequest.from_dict(raw["review"]) if raw.get("review") else None,
            failure=Failure.from_dict(raw["failure"]) if raw.get("failure") else None,
            last_event_seq=int(raw.get("last_event_seq", 0)),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
        )
