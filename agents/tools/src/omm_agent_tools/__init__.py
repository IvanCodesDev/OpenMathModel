"""工具适配器。

实现内核声明的端口。高风险或高成本能力（代码执行、外部检索、文件写入）统一走执行
接口，以便记录输入输出摘要、耗时、成本与产物。
"""

from .code_run import CODE_RUN_TOOL_NAME, DEFAULT_LANGUAGE, CodeRunSandbox
from .invoker import (
    MAX_TURN_PARALLELISM,
    EventRecorder,
    IdempotencyCache,
    RecordingInvoker,
    args_fingerprint,
    execute_parallel,
    failure_detail,
    summarize,
)
from .knowledge import (
    KNOWLEDGE_LIBRARY_ENV,
    KNOWLEDGE_READ_TOOL,
    KNOWLEDGE_SEARCH_TOOL,
    KnowledgeCard,
    KnowledgeLibrary,
    knowledge_tool_specs,
    load_knowledge_library,
    resolve_library_path,
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
from .runners import (
    LANGUAGE_ALIASES,
    LANGUAGE_SPECS,
    PLANNED_LANGUAGES,
    LanguageProbe,
    LanguageSpec,
    SubprocessRunner,
    normalize_language,
    probe_language,
    resolve_executable,
)
from .sandbox_tools import (
    ENV_PROBE_PACKAGE_CANDIDATES,
    WS_READ_MAX_CHARS,
    env_fingerprint,
    language_fingerprint,
    sandbox_workspace_specs,
)
from .table_profile import PROFILE_MAX_ROWS, profile_csv_text, table_profile_spec
from .workspace import TaskWorkspace, WorkspaceArtifactStore, WorkspaceViolation

__all__ = [
    "CODE_RUN_TOOL_NAME",
    "DEFAULT_LANGUAGE",
    "ENV_PROBE_PACKAGE_CANDIDATES",
    "EventRecorder",
    "PROFILE_MAX_ROWS",
    "CodeRunSandbox",
    "IdempotencyCache",
    "KNOWLEDGE_LIBRARY_ENV",
    "KNOWLEDGE_READ_TOOL",
    "KNOWLEDGE_SEARCH_TOOL",
    "KnowledgeCard",
    "KnowledgeLibrary",
    "LANGUAGE_ALIASES",
    "LANGUAGE_SPECS",
    "LanguageProbe",
    "LanguageSpec",
    "MAX_TURN_PARALLELISM",
    "PLANNED_LANGUAGES",
    "PythonSandbox",
    "RecordingInvoker",
    "SubprocessRunner",
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
    "failure_detail",
    "knowledge_tool_specs",
    "language_fingerprint",
    "load_knowledge_library",
    "normalize_language",
    "probe_language",
    "probe_sandbox_gpu",
    "profile_csv_text",
    "resolve_executable",
    "resolve_library_path",
    "sandbox_workspace_specs",
    "summarize",
    "table_profile_spec",
    "tier_rank",
]
