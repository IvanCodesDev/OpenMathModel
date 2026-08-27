"""跨语言协议的 Python 绑定。

协议源文件（JSON Schema，schemas/v1/）是事实来源；本包承载：
- ``v1/``：scripts/generate_python.py 从 Schema 生成的 Pydantic v2 模型（禁止手改）；
- ``enums``：生成枚举的稳定别名层；
- ``inputs``：API 请求载荷模型（写方向，extra=ignore）。

任何服务端或 Agent 模块都从这里取协议类型，不各自重定义。
"""

from .enums import (
    TERMINAL_TASK_RUN_STATUSES,
    AgentEventType,
    ApprovalDecisionType,
    ApprovalStatus,
    ArtifactKind,
    ArtifactStatus,
    FailureClass,
    PaperExportFormat,
    PaperExportStatus,
    ProjectMode,
    StepRunStatus,
    TaskRunStatus,
)
from .inputs import (
    BudgetInput,
    CreatePaperExportInput,
    CreateProjectInput,
    CreateTaskRunInput,
    InputModel,
    TaskRunAction,
    TaskRunActionInput,
)
from .v1 import (
    AgentEvent,
    ApprovalRequest,
    Artifact,
    DatasetProfile,
    DeliveryManifest,
    DocumentDraft,
    ErrorEnvelope,
    ExperimentSummary,
    ModelingWorkspaceView,
    PaperExport,
    PlanProposal,
    Project,
    StepRun,
    TaskRun,
)

# 历史别名：wave-2 代码以 ErrorBody 引用错误信封
ErrorBody = ErrorEnvelope

__all__ = [
    "TERMINAL_TASK_RUN_STATUSES",
    "AgentEventType",
    "ApprovalDecisionType",
    "ApprovalStatus",
    "ArtifactKind",
    "ArtifactStatus",
    "FailureClass",
    "PaperExportFormat",
    "PaperExportStatus",
    "ProjectMode",
    "StepRunStatus",
    "TaskRunStatus",
    "BudgetInput",
    "CreatePaperExportInput",
    "CreateProjectInput",
    "CreateTaskRunInput",
    "InputModel",
    "TaskRunAction",
    "TaskRunActionInput",
    "AgentEvent",
    "ApprovalRequest",
    "Artifact",
    "DatasetProfile",
    "DeliveryManifest",
    "DocumentDraft",
    "ErrorBody",
    "ErrorEnvelope",
    "ExperimentSummary",
    "ModelingWorkspaceView",
    "PaperExport",
    "PlanProposal",
    "Project",
    "StepRun",
    "TaskRun",
]

__version__ = "0.1.0"
