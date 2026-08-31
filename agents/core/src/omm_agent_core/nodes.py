"""Node contract: one node executes one work state.

Nodes receive structured context and return structured results (architecture
constraint 5.1). They never mutate the snapshot and never emit events — the
engine owns ordering and durability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import ArtifactRef, TaskRunSnapshot
from .ports import NodeServices
from .states import TaskState


@dataclass(frozen=True)
class NodeContext:
    run_id: str
    project_id: str
    state: TaskState
    step_id: str
    attempt: int
    #: Run-level inputs (problem statement, dataset refs, user options...).
    inputs: Mapping[str, Any]
    #: Outputs of previously completed states, keyed by state value.
    prior_outputs: Mapping[str, Mapping[str, Any]]
    #: Approved gate decisions (option ids) keyed by the requesting state
    #: value — e.g. the G2 data-gate choice ("adopt_cleaned"/"use_raw") made
    #: after DATA_PREPARATION succeeded. Empty for runs without resolved gates.
    review_decisions: Mapping[str, str] = field(default_factory=dict)

    @staticmethod
    def for_step(
        snapshot: TaskRunSnapshot, state: TaskState, step_id: str, attempt: int
    ) -> "NodeContext":
        return NodeContext(
            run_id=snapshot.run_id,
            project_id=snapshot.project_id,
            state=state,
            step_id=step_id,
            attempt=attempt,
            inputs=dict(snapshot.inputs),
            prior_outputs={key: dict(value) for key, value in snapshot.outputs.items()},
            review_decisions=dict(snapshot.review_decisions),
        )


@dataclass(frozen=True)
class NodeResult:
    status: str  # "succeeded" | "failed" | "needs_review"
    outputs: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    error: str | None = None
    review_reason: str | None = None
    #: Optional gate metadata for NEEDS_REVIEW results (gate id, options,
    #: evidence...). The engine copies it into the REVIEW_REQUESTED payload
    #: under "gate" — absent when None, so legacy flows keep their payload
    #: shape byte-identical (golden traces unaffected).
    review_meta: dict[str, Any] | None = None

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"

    @staticmethod
    def succeeded(
        outputs: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> "NodeResult":
        return NodeResult(
            status=NodeResult.SUCCEEDED,
            outputs=outputs or {},
            metrics=metrics or {},
            artifacts=artifacts,
        )

    @staticmethod
    def failed(error: str, metrics: dict[str, Any] | None = None) -> "NodeResult":
        return NodeResult(status=NodeResult.FAILED, error=error, metrics=metrics or {})

    @staticmethod
    def needs_review(
        reason: str,
        outputs: dict[str, Any] | None = None,
        review_meta: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> "NodeResult":
        return NodeResult(
            status=NodeResult.NEEDS_REVIEW,
            review_reason=reason,
            outputs=outputs or {},
            metrics=metrics or {},
            artifacts=artifacts,
            review_meta=review_meta,
        )


@runtime_checkable
class StepNode(Protocol):
    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult: ...


NodeRegistry = Mapping[TaskState, StepNode]
