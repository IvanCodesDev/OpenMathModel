"""契约枚举的稳定别名层。

事实来源是 schemas/v1/*.schema.json；本模块只把 scripts/generate_python.py
生成的枚举以稳定名称re-export，消费方不感知生成器的命名细节。
演进规则：枚举只增不改；消费者必须安全处理未知枚举值。
"""

from __future__ import annotations

from .v1.agent_event import Type as AgentEventType
from .v1.approval_request import DecisionType as ApprovalDecisionType
from .v1.approval_request import Status as ApprovalStatus
from .v1.artifact import Kind as ArtifactKind
from .v1.artifact import Status as ArtifactStatus
from .v1.project import Mode as ProjectMode
from .v1.step_run import Status as StepRunStatus
from .v1.task_run import FailureClass
from .v1.task_run import Status as TaskRunStatus

TERMINAL_TASK_RUN_STATUSES = frozenset(
    {TaskRunStatus.COMPLETED, TaskRunStatus.FAILED, TaskRunStatus.CANCELLED}
)

__all__ = [
    "AgentEventType",
    "ApprovalDecisionType",
    "ApprovalStatus",
    "ArtifactKind",
    "ArtifactStatus",
    "FailureClass",
    "ProjectMode",
    "StepRunStatus",
    "TaskRunStatus",
    "TERMINAL_TASK_RUN_STATUSES",
]
