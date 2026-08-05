"""Explicit task state machine.

State diagram (docs/PROJECT_STRUCTURE.md):

    CREATED -> PROBLEM_ANALYSIS -> DATA_PREPARATION -> MODEL_PLANNING
            -> EXPERIMENTING -> VALIDATING -> PAPER_WRITING -> COMPLETED
                                          \\-> NEEDS_REVIEW
                                          \\-> FAILED

Design decisions (kept deliberately small for the MVP loop):

- NEEDS_REVIEW is a first-class state, not a UI flag: any work state may
  suspend into it, and resolving the review resumes an explicit target state.
- FAILED is terminal for the engine loop but retryable through an explicit
  ``retry`` action, which re-enters the state that failed (attempt + 1).
- Pause/cancel are control flags on the run, not states: they gate scheduling
  without exploding the transition matrix.
"""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    CREATED = "CREATED"
    PROBLEM_ANALYSIS = "PROBLEM_ANALYSIS"
    DATA_PREPARATION = "DATA_PREPARATION"
    MODEL_PLANNING = "MODEL_PLANNING"
    EXPERIMENTING = "EXPERIMENTING"
    VALIDATING = "VALIDATING"
    PAPER_WRITING = "PAPER_WRITING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


#: Forward execution order of the work states.
WORK_SEQUENCE: tuple[TaskState, ...] = (
    TaskState.PROBLEM_ANALYSIS,
    TaskState.DATA_PREPARATION,
    TaskState.MODEL_PLANNING,
    TaskState.EXPERIMENTING,
    TaskState.VALIDATING,
    TaskState.PAPER_WRITING,
)

WORK_STATES: frozenset[TaskState] = frozenset(WORK_SEQUENCE)

#: Terminal for the scheduling loop. FAILED can still be left via `retry`.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED}
)


class TransitionError(Exception):
    """Raised when a state change violates the transition matrix."""

    def __init__(self, source: TaskState, target: TaskState) -> None:
        super().__init__(f"illegal transition: {source.value} -> {target.value}")
        self.source = source
        self.target = target


def next_work_state(state: TaskState) -> TaskState | None:
    """Return the state that follows ``state`` on the happy path."""
    if state is TaskState.CREATED:
        return WORK_SEQUENCE[0]
    if state in WORK_STATES:
        index = WORK_SEQUENCE.index(state)
        if index + 1 < len(WORK_SEQUENCE):
            return WORK_SEQUENCE[index + 1]
        return TaskState.COMPLETED
    return None


def can_transition(source: TaskState, target: TaskState) -> bool:
    if source == target:
        return False
    if source is TaskState.CREATED:
        return target is WORK_SEQUENCE[0] or target is TaskState.FAILED
    if source in WORK_STATES:
        if target is TaskState.NEEDS_REVIEW or target is TaskState.FAILED:
            return True
        return target is next_work_state(source)
    if source is TaskState.NEEDS_REVIEW:
        # Approve resumes any work state (reviewer may send the run backwards);
        # reject fails the run.
        return target in WORK_STATES or target is TaskState.FAILED
    if source is TaskState.FAILED:
        # Explicit retry re-enters the work state that failed.
        return target in WORK_STATES
    return False  # COMPLETED is fully terminal.


def assert_transition(source: TaskState, target: TaskState) -> None:
    if not can_transition(source, target):
        raise TransitionError(source, target)
