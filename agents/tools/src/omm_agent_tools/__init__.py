"""工具适配器。

实现内核声明的端口。高风险或高成本能力（代码执行、外部检索、文件写入）统一走执行
接口，以便记录输入输出摘要、耗时、成本与产物。
"""

from .invoker import (
    MAX_TURN_PARALLELISM,
    EventRecorder,
    IdempotencyCache,
    RecordingInvoker,
    args_fingerprint,
    execute_parallel,
    summarize,
)
from .python_runner import PythonSandbox
from .registry import (
    TIERS,
    ToolCallContext,
    ToolHandler,
    ToolNotAllowed,
    ToolRegistry,
    ToolSpec,
    tier_rank,
)
from .workspace import TaskWorkspace, WorkspaceArtifactStore, WorkspaceViolation

__all__ = [
    "EventRecorder",
    "IdempotencyCache",
    "MAX_TURN_PARALLELISM",
    "PythonSandbox",
    "RecordingInvoker",
    "TIERS",
    "TaskWorkspace",
    "ToolCallContext",
    "ToolHandler",
    "ToolNotAllowed",
    "ToolRegistry",
    "ToolSpec",
    "WorkspaceArtifactStore",
    "WorkspaceViolation",
    "args_fingerprint",
    "execute_parallel",
    "summarize",
    "tier_rank",
]
