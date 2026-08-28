"""omm-agent-harness: 模型外围自研运行时基座（设计文档 §4）。

H0 批次交付三个组件：ModelGateway（执行面唯一 LLM 出口）、BudgetGovernor
（四级预算硬停）、TraceHub（运行跟踪与报告）。H1 批次新增两个组件：
run_inner_loop（L-I 内环引擎，§5.2/§5.3）与 ContextAssembler（分节装配，
§4.2）。Subagent 组件随 H2 进入本包。依赖方向：harness → core, tools；
禁止 import omm_api、禁止 import skills（parser/validator 由调用方注入）。
"""

from .budget import (
    SUBAGENT_MAX_FRACTION,
    BudgetGovernor,
    LoopBudget,
    NodeBudget,
    RunBudget,
)
from .context import (
    STANDARD_SECTION_ORDER,
    AssembledPrompt,
    AssemblyError,
    ContextAssembler,
    Section,
)
from .gateway import (
    CallBudget,
    GatewayConfig,
    Message,
    ModelGateway,
    ModelRouting,
    Reply,
    ReplayCassette,
    ToolCall,
    TransportFailure,
    Usage,
    httpx_sender,
    request_fingerprint,
)
from .loops import LoopOutcome, LoopTask, run_inner_loop
from .sandbox_agent import (
    SandboxAssertion,
    SandboxEvidence,
    SandboxTask,
    run_sandbox_task,
)
from .subagents import (
    CONTEXT_SLICE_MAX_CHARS,
    MAX_SUBAGENT_CONCURRENCY,
    ResultEnvelope,
    SpawnSpec,
    SubagentSupervisor,
)
from .trace import Span, TraceHub

__all__ = [
    "CONTEXT_SLICE_MAX_CHARS",
    "MAX_SUBAGENT_CONCURRENCY",
    "SUBAGENT_MAX_FRACTION",
    "STANDARD_SECTION_ORDER",
    "AssembledPrompt",
    "AssemblyError",
    "BudgetGovernor",
    "CallBudget",
    "ContextAssembler",
    "GatewayConfig",
    "LoopBudget",
    "LoopOutcome",
    "LoopTask",
    "Message",
    "ModelGateway",
    "ModelRouting",
    "NodeBudget",
    "Reply",
    "ReplayCassette",
    "ResultEnvelope",
    "RunBudget",
    "SandboxAssertion",
    "SandboxEvidence",
    "SandboxTask",
    "Section",
    "Span",
    "SpawnSpec",
    "SubagentSupervisor",
    "ToolCall",
    "TraceHub",
    "TransportFailure",
    "Usage",
    "httpx_sender",
    "request_fingerprint",
    "run_inner_loop",
    "run_sandbox_task",
]
