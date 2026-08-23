"""数学建模 Agent 领域内核。

只定义领域语义与端口，不依赖 FastAPI、Tauri、具体 UI、沙盒实现或 Workflow 供应商
SDK。工具、技能与执行环境通过端口注入，因此本包位于依赖图最底层。
"""

from .engine import AdvanceOutcome, TaskRunEngine
from .errors import CATALOG, AgentError, Disposition, ErrorCode, ErrorInfo
from .models import (
    AgentEvent,
    ArtifactRef,
    EventType,
    Failure,
    ReviewRequest,
    StepRun,
    StepStatus,
    TaskRunSnapshot,
    ToolResult,
)
from .nodes import NodeContext, NodeRegistry, NodeResult, StepNode
from .ports import (
    ArtifactStore,
    Clock,
    EventSink,
    FixedClock,
    IdGenerator,
    InMemoryArtifactStore,
    InMemoryEventSink,
    LlmPort,
    NodeServices,
    SequentialIdGenerator,
    SystemClock,
    ToolInvoker,
    UuidIdGenerator,
)
from .reducer import ReduceError, SequenceError, apply_event, replay_events
from .states import (
    TERMINAL_STATES,
    WORK_SEQUENCE,
    WORK_STATES,
    TaskState,
    TransitionError,
    assert_transition,
    can_transition,
    next_work_state,
)

__all__ = [
    "AdvanceOutcome",
    "AgentError",
    "AgentEvent",
    "ArtifactRef",
    "ArtifactStore",
    "CATALOG",
    "Clock",
    "Disposition",
    "ErrorCode",
    "ErrorInfo",
    "EventSink",
    "EventType",
    "Failure",
    "FixedClock",
    "IdGenerator",
    "InMemoryArtifactStore",
    "InMemoryEventSink",
    "LlmPort",
    "NodeContext",
    "NodeRegistry",
    "NodeResult",
    "NodeServices",
    "ReduceError",
    "ReviewRequest",
    "SequenceError",
    "SequentialIdGenerator",
    "StepNode",
    "StepRun",
    "StepStatus",
    "SystemClock",
    "TERMINAL_STATES",
    "TaskRunEngine",
    "TaskRunSnapshot",
    "TaskState",
    "ToolInvoker",
    "ToolResult",
    "TransitionError",
    "UuidIdGenerator",
    "WORK_SEQUENCE",
    "WORK_STATES",
    "apply_event",
    "assert_transition",
    "can_transition",
    "next_work_state",
    "replay_events",
]
