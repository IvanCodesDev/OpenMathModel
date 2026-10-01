"""omm-agent-harness: 模型外围自研运行时基座（设计文档 §4）。

H0 批次交付三个组件：ModelGateway（执行面唯一 LLM 出口）、BudgetGovernor
（四级预算硬停）、TraceHub（运行跟踪与报告）。H1 批次新增两个组件：
run_inner_loop（L-I 内环引擎，§5.2/§5.3）与 ContextAssembler（分节装配，
§4.2）。Subagent 组件随 H2 进入本包；E6 看板聚合器（metrics，§14.2）随控制面
接线从 evals 迁入。依赖方向：harness → core, tools；
禁止 import omm_api、禁止 import skills（parser/validator 由调用方注入）。
"""

from .budget import (
    SUBAGENT_MAX_FRACTION,
    UNLIMITED,
    BudgetGovernor,
    LoopBudget,
    NodeBudget,
    RunBudget,
    is_unlimited,
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
from .metrics import (
    aggregate_batch,
    aggregate_run,
    compare_reports,
    render_batch_markdown,
    render_markdown,
)
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
    "UNLIMITED",
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
    "aggregate_batch",
    "aggregate_run",
    "compare_reports",
    "httpx_sender",
    "is_unlimited",
    "render_batch_markdown",
    "render_markdown",
    "request_fingerprint",
    "run_inner_loop",
    "run_sandbox_task",
]
