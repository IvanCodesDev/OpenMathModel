"""工具适配器。

实现内核声明的端口。高风险或高成本能力（代码执行、外部检索、文件写入）统一走执行
接口，以便记录输入输出摘要、耗时、成本与产物。
"""

from .invoker import EventRecorder, RecordingInvoker, summarize
from .python_runner import PythonSandbox
from .registry import (
    ToolCallContext,
    ToolHandler,
    ToolNotAllowed,
    ToolRegistry,
    ToolSpec,
)
from .workspace import TaskWorkspace, WorkspaceArtifactStore, WorkspaceViolation

__all__ = [
    "EventRecorder",
    "PythonSandbox",
    "RecordingInvoker",
    "TaskWorkspace",
    "ToolCallContext",
    "ToolHandler",
    "ToolNotAllowed",
    "ToolRegistry",
    "ToolSpec",
    "WorkspaceArtifactStore",
    "WorkspaceViolation",
    "summarize",
]
