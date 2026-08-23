"""omm-agent-harness: 模型外围自研运行时基座（设计文档 §4）。

H0 批次交付三个组件：ModelGateway（执行面唯一 LLM 出口）、BudgetGovernor
（四级预算硬停）、TraceHub（运行跟踪与报告）。Loop/Subagent 等组件随
H1/H2 批次进入本包。依赖方向：harness → core, tools；禁止 import omm_api。
"""

from .budget import (
    SUBAGENT_MAX_FRACTION,
    BudgetGovernor,
    LoopBudget,
    NodeBudget,
    RunBudget,
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
from .trace import Span, TraceHub

__all__ = [
    "SUBAGENT_MAX_FRACTION",
    "BudgetGovernor",
    "CallBudget",
    "GatewayConfig",
    "LoopBudget",
    "Message",
    "ModelGateway",
    "ModelRouting",
    "NodeBudget",
    "Reply",
    "ReplayCassette",
    "RunBudget",
    "Span",
    "ToolCall",
    "TraceHub",
    "TransportFailure",
    "Usage",
    "httpx_sender",
    "request_fingerprint",
]
