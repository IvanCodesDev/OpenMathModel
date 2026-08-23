"""Error taxonomy for the agent runtime (design doc appendix D2.1).

One flat catalog of stable error codes shared by every layer: the gateway,
loop engine, tool bus, budget governor, graph scheduler and subagent
supervisor all raise ``AgentError`` with a code from this table instead of
inventing ad-hoc strings. Codes travel in ``STEP_FAILED.payload.error_code``
and subagent ``ResultEnvelope.error_code``; the UI renders human wording,
while evals aggregate on the code.

The catalog is data, not behavior: dispositions here describe the DEFAULT
handling documented in D2.1 so that handlers and tests can assert against a
single source of truth. This module is stdlib-only (core dependency rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Stable error codes; the string value is what gets persisted."""

    # E1xx — LLM (thrown by gateway / loop)
    LLM_NETWORK = "E110"  # network/timeout after gateway retries
    LLM_SCHEMA_VIOLATION = "E120"  # structure invalid after R1 repair
    LLM_CONTENT_REFUSAL = "E130"  # provider refused the content
    LLM_PROVIDER_QUOTA = "E140"  # provider-side quota/billing

    # E2xx — tools (thrown by tool bus)
    TOOL_BAD_ARGS = "E210"
    TOOL_TIMEOUT = "E220"
    TOOL_CRASH = "E230"
    TOOL_TIER_DENIED = "E240"  # assembly defect: caller tier too low
    TOOL_IDEMPOTENCY_CONFLICT = "E250"  # same call slot, different args

    # E3xx — budget (thrown by budget governor / loop)
    BUDGET_RUN = "E310"  # run-level budget → GB gate
    BUDGET_NODE = "E320"
    BUDGET_LOOP = "E330"  # max_turns / repair budget exhausted
    LOOP_NO_PROGRESS = "E331"  # K identical tool calls or final answers
    LOOP_TOOL_FAIL_STREAK = "E332"  # same tool failed M times in a row
    BUDGET_SUBAGENT = "E340"

    # E4xx — graph (thrown by scheduler)
    GRAPH_ILLEGAL_TRANSITION = "E410"
    GRAPH_READS_UNSATISFIED = "E420"
    GRAPH_ITERATION_LIMIT = "E430"  # forced G3 gate
    GRAPH_JOIN_FAILED = "E440"

    # E5xx — subagents (thrown by supervisor)
    SUBAGENT_SPAWN_INVALID = "E510"
    SUBAGENT_ENVELOPE_INVALID = "E520"
    SUBAGENT_TIMEOUT_REAPED = "E530"
    SUBAGENT_DEPTH_VIOLATION = "E540"


class Disposition(str, Enum):
    """Default handling per D2.1; consumers may escalate but not soften."""

    FAIL_STEP = "fail_step"  # step fails → run fails → UI offers retry
    BUDGET_GATE = "budget_gate"  # E310: raise the GB gate, human decides
    OBSERVATION = "observation"  # repairable: feed back into the inner loop
    DEFECT = "defect"  # assembly/config bug: fail fast, alert
    PARENT_POLICY = "parent_policy"  # parent decides: retry → degrade → gate
    FORCED_GATE = "forced_gate"  # E430: force a human gate


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    owner: str  # which component raises it
    disposition: Disposition
    summary: str  # short human wording (chinese, mirrors design doc)


CATALOG: dict[ErrorCode, ErrorInfo] = {
    info.code: info
    for info in (
        ErrorInfo(ErrorCode.LLM_NETWORK, "gateway", Disposition.FAIL_STEP,
                  "LLM 网络/超时（重试后仍失败）"),
        ErrorInfo(ErrorCode.LLM_SCHEMA_VIOLATION, "loop", Disposition.FAIL_STEP,
                  "LLM 输出结构违约（修复后仍失败）"),
        ErrorInfo(ErrorCode.LLM_CONTENT_REFUSAL, "gateway", Disposition.FAIL_STEP,
                  "LLM 内容拒绝"),
        ErrorInfo(ErrorCode.LLM_PROVIDER_QUOTA, "gateway", Disposition.FAIL_STEP,
                  "供应商配额受限（附冷却提示）"),
        ErrorInfo(ErrorCode.TOOL_BAD_ARGS, "toolbus", Disposition.OBSERVATION,
                  "工具参数校验失败"),
        ErrorInfo(ErrorCode.TOOL_TIMEOUT, "toolbus", Disposition.OBSERVATION,
                  "工具执行超时"),
        ErrorInfo(ErrorCode.TOOL_CRASH, "toolbus", Disposition.OBSERVATION,
                  "工具崩溃"),
        ErrorInfo(ErrorCode.TOOL_TIER_DENIED, "toolbus", Disposition.DEFECT,
                  "工具权限档位越权（装配缺陷）"),
        ErrorInfo(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT, "toolbus", Disposition.DEFECT,
                  "幂等键冲突：同一调用槽位参数不一致"),
        ErrorInfo(ErrorCode.BUDGET_RUN, "budget", Disposition.BUDGET_GATE,
                  "运行级预算耗尽（进 GB 闸门）"),
        ErrorInfo(ErrorCode.BUDGET_NODE, "budget", Disposition.FAIL_STEP,
                  "节点级预算耗尽"),
        ErrorInfo(ErrorCode.BUDGET_LOOP, "loop", Disposition.FAIL_STEP,
                  "内环预算耗尽（max_turns/修复次数）"),
        ErrorInfo(ErrorCode.LOOP_NO_PROGRESS, "loop", Disposition.FAIL_STEP,
                  "内环无进展提前终止"),
        ErrorInfo(ErrorCode.LOOP_TOOL_FAIL_STREAK, "loop", Disposition.FAIL_STEP,
                  "同一工具连续失败达到上限"),
        ErrorInfo(ErrorCode.BUDGET_SUBAGENT, "budget", Disposition.PARENT_POLICY,
                  "子代理预算切片耗尽（收割为 failed envelope）"),
        ErrorInfo(ErrorCode.GRAPH_ILLEGAL_TRANSITION, "scheduler", Disposition.DEFECT,
                  "图非法转移"),
        ErrorInfo(ErrorCode.GRAPH_READS_UNSATISFIED, "scheduler", Disposition.DEFECT,
                  "图 reads 依赖不可满足"),
        ErrorInfo(ErrorCode.GRAPH_ITERATION_LIMIT, "scheduler", Disposition.FORCED_GATE,
                  "迭代边超限（强制 G3 闸门）"),
        ErrorInfo(ErrorCode.GRAPH_JOIN_FAILED, "scheduler", Disposition.FAIL_STEP,
                  "并行 lane join 失败"),
        ErrorInfo(ErrorCode.SUBAGENT_SPAWN_INVALID, "supervisor", Disposition.DEFECT,
                  "SpawnSpec 违约"),
        ErrorInfo(ErrorCode.SUBAGENT_ENVELOPE_INVALID, "supervisor", Disposition.PARENT_POLICY,
                  "ResultEnvelope 违约"),
        ErrorInfo(ErrorCode.SUBAGENT_TIMEOUT_REAPED, "supervisor", Disposition.PARENT_POLICY,
                  "子代理超时被收割"),
        ErrorInfo(ErrorCode.SUBAGENT_DEPTH_VIOLATION, "supervisor", Disposition.DEFECT,
                  "子代理深度违规（深度=1 强制）"),
    )
}


class AgentError(Exception):
    """Runtime error carrying a stable code from the catalog.

    ``context`` holds structured facts for the event payload (e.g. used
    tokens at a budget stop) — never secrets, never raw model output.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str = "",
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        info = CATALOG[code]
        message = f"[{code.value}] {info.summary}" + (f": {detail}" if detail else "")
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.context: dict[str, Any] = dict(context or {})

    @property
    def info(self) -> ErrorInfo:
        return CATALOG[self.code]

    def to_payload(self) -> dict[str, Any]:
        """Shape used in STEP_FAILED payloads and result envelopes."""
        payload: dict[str, Any] = {
            "error_code": self.code.value,
            "error": str(self),
        }
        if self.context:
            payload["error_context"] = dict(self.context)
        return payload
