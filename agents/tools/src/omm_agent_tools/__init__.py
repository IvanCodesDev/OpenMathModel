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
from .python_runner import PythonSandbox, probe_sandbox_gpu
from .registry import (
    TIERS,
    ToolCallContext,
    ToolHandler,
    ToolNotAllowed,
    ToolRegistry,
    ToolSpec,
    tier_rank,
)
from .sandbox_tools import (
    ENV_PROBE_PACKAGE_CANDIDATES,
    WS_READ_MAX_CHARS,
    env_fingerprint,
    sandbox_workspace_specs,
)
from .table_profile import PROFILE_MAX_ROWS, profile_csv_text, table_profile_spec
from .workspace import TaskWorkspace, WorkspaceArtifactStore, WorkspaceViolation

__all__ = [
    "ENV_PROBE_PACKAGE_CANDIDATES",
    "EventRecorder",
    "PROFILE_MAX_ROWS",
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
    "WS_READ_MAX_CHARS",
    "WorkspaceArtifactStore",
    "WorkspaceViolation",
    "args_fingerprint",
    "env_fingerprint",
    "execute_parallel",
    "probe_sandbox_gpu",
    "profile_csv_text",
    "sandbox_workspace_specs",
    "summarize",
    "table_profile_spec",
    "tier_rank",
]
