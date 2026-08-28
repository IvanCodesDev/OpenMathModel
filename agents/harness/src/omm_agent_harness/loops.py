"""Inner loop engine (L-I, design §5.2): the generic tool-use turn loop.

One loop shape for every skill that needs multi-turn reasoning: chat → (tool
calls → observations → next turn | final answer → parse → validate → done or
single-repair retry). Every exit maps to exactly one row of the §5.3 table —
"stuck" is not a state this engine can produce.

Layering discipline (§4.1/§5.4): network retries live in the gateway below,
stage retries in the graph above; THIS layer owns structural repair (R1) and
the four loop budgets (max_turns / repairs / no-progress K / tool-fail M,
numbers from §4.7 via ``LoopBudget``). Run/node token ledgers are the budget
governor's job at the chat call site, not re-checked here.

Dependency shape: ``chat`` and ``execute_tools`` are injected callables, and
``LoopTask.parser``/``validator`` come from the caller (skills own their JSON
extraction and schema validation; harness must not import skills). This keeps
the engine testable with scripted replies — the E2 tests feed canned outputs
and assert the §5.3 mapping row by row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from omm_agent_core.errors import ErrorCode
from omm_agent_core.models import ToolResult

from .budget import LoopBudget
from .gateway import Message, Reply, ToolCall, Usage

__all__ = [
    "ChatFn",
    "LoopOutcome",
    "LoopTask",
    "ToolExecutor",
    "run_inner_loop",
]

#: One model turn: bound by the caller to a gateway + call budget so the loop
#: never touches provider config. ``tools`` is the only per-call variation.
ChatFn = Callable[[Sequence[Message]], Reply]

#: Executes one turn's tool calls (order-preserving, results align by index).
#: Recording/audit/tier enforcement live inside the executor (ToolBus).
ToolExecutor = Callable[[Sequence[ToolCall]], Sequence[ToolResult]]

#: Parses the final-answer text into a candidate value. Raise ``ValueError``
#: (or json.JSONDecodeError, a subclass) to signal "not parseable" — treated
#: exactly like a schema violation and fed to the repair ladder.
Parser = Callable[[str], Any]

#: Returns validation problems (empty list = valid). The single defense line:
#: response_format hints upstream are never trusted (§4.1).
Validator = Callable[[Any], list[str]]

#: Observation payload cap. Oversized tool output is truncated with an honest
#: marker; offloading to artifacts is the caller's concern (it owns the store).
_OBSERVATION_LIMIT = 4000

#: Repair feedback caps, mirroring the fixed format discipline of D4
#: (``__repair_error`` + truncated ``__previous_output``).
_REPAIR_PREVIOUS_LIMIT = 2000


def _default_parser(raw: str) -> Any:
    return json.loads(raw)


@dataclass(frozen=True)
class LoopTask:
    """What one inner loop is asked to produce (D1.2 shape)."""

    task_id: str
    messages: tuple[Message, ...]  # assembled prompt (ContextAssembler output)
    validator: Validator
    parser: Parser = _default_parser
    budget: LoopBudget = field(default_factory=LoopBudget)


@dataclass(frozen=True)
class LoopOutcome:
    """Terminal report of one inner loop (D1.2 shape, §5.3 row encoded).

    ``status`` is the coarse disposition family; ``exit_reason`` names the
    §5.3 row; ``error_code`` is the stable code evals aggregate on.
    """

    status: str  # "done" | "invalid" | "exhausted" | "cancelled"
    exit_reason: str  # done|schema_violation|max_turns|no_progress|tool_fail_streak|cancelled
    value: dict[str, Any] | None
    error_code: str | None
    turns: int
    llm_calls: int
    repairs_used: int
    usage: Usage
    last_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


class _UsageTally:
    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.duration = 0

    def add(self, usage: Usage) -> None:
        self.prompt += usage.prompt_tokens
        self.completion += usage.completion_tokens
        self.duration += usage.duration_ms

    def total(self) -> Usage:
        return Usage(
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            duration_ms=self.duration,
        )


def _tool_signature(calls: Sequence[ToolCall]) -> str:
    """Signature of one tool turn: (name, args_hash) tuples, order-sensitive."""
    canonical = json.dumps(
        [
            [call.name, hashlib.sha256(
                json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=repr).encode("utf-8")
            ).hexdigest()]
            for call in calls
        ],
        ensure_ascii=False,
    )
    return "tools:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _answer_signature(content: str) -> str:
    return "answer:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断 {len(text) - limit} 字符）"


def _observation_content(result: ToolResult) -> str:
    """ToolResult → what the model sees next turn. Honest and bounded."""
    if result.ok:
        body = json.dumps(result.output, ensure_ascii=False, default=repr)
        return _truncate(body, _OBSERVATION_LIMIT)
    detail = result.error or result.status
    return _truncate(f"工具执行失败（{result.status}）：{detail}", _OBSERVATION_LIMIT)


def _repair_message(problems: str, previous: str) -> Message:
    """Fixed repair-feedback format (D4): error list + truncated prior output."""
    return Message(
        role="user",
        content=(
            "你上一次的输出未通过结构校验，必须修复后重新输出完整结果。\n"
            f"__repair_error: {problems}\n"
            f"__previous_output: {_truncate(previous, _REPAIR_PREVIOUS_LIMIT)}\n"
            "只输出符合要求结构的 JSON，不要输出多余文本。"
        ),
    )


class _NoProgressTracker:
    """E331: K consecutive identical signatures (tool turn or final answer)."""

    def __init__(self, k: int) -> None:
        self._k = max(int(k), 1)
        self._last: str | None = None
        self._streak = 0

    def observe(self, signature: str) -> bool:
        if signature == self._last:
            self._streak += 1
        else:
            self._last = signature
            self._streak = 1
        return self._streak >= self._k


class _ToolFailStreakTracker:
    """E332: the SAME tool failing M times in a row (across turns)."""

    def __init__(self, m: int) -> None:
        self._m = max(int(m), 1)
        self._tool: str | None = None
        self._streak = 0
        self.last_error: str | None = None

    def observe(self, results: Sequence[ToolCall], outcomes: Sequence[ToolResult]) -> bool:
        for call, outcome in zip(results, outcomes):
            if outcome.ok:
                self._tool = None
                self._streak = 0
                continue
            if call.name == self._tool:
                self._streak += 1
            else:
                self._tool = call.name
                self._streak = 1
            self.last_error = f"{call.name}: {outcome.error or outcome.status}"
            if self._streak >= self._m:
                return True
        return False


def run_inner_loop(
    task: LoopTask,
    *,
    chat: ChatFn,
    execute_tools: ToolExecutor | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> LoopOutcome:
    """Drive one inner loop to a §5.3 exit. Never raises for model behavior.

    Turn semantics (§4.7 loop row): a TURN is one forward attempt — a tool
    round or a fresh final answer. The single R1 repair retries WITHIN the
    same turn (budget.repairs is its own allowance), so the "single-shot"
    profile ``max_turns=1, repairs=1`` reproduces today's LlmSkillNode
    behavior exactly: one normal try plus one repair try.
    """
    budget = task.budget
    messages: list[Message] = list(task.messages)
    tally = _UsageTally()
    no_progress = _NoProgressTracker(budget.no_progress_k)
    fail_streak = _ToolFailStreakTracker(budget.tool_fail_m)

    llm_calls = 0
    repairs_used = 0
    turns = 0

    def outcome(
        status: str,
        exit_reason: str,
        *,
        value: dict[str, Any] | None = None,
        error_code: ErrorCode | None = None,
        last_error: str | None = None,
    ) -> LoopOutcome:
        return LoopOutcome(
            status=status,
            exit_reason=exit_reason,
            value=value,
            error_code=error_code.value if error_code else None,
            turns=turns,
            llm_calls=llm_calls,
            repairs_used=repairs_used,
            usage=tally.total(),
            last_error=last_error,
        )

    for _ in range(max(budget.max_turns, 1)):
        turns += 1
        # Repair retries stay inside this while: same turn, extra chat calls.
        while True:
            if cancelled is not None and cancelled():
                return outcome("cancelled", "cancelled")

            reply = chat(messages)
            llm_calls += 1
            tally.add(reply.usage)

            if reply.tool_calls:
                signature = _tool_signature(reply.tool_calls)
                messages.append(
                    Message(role="assistant", content=reply.content or "")
                )
                if execute_tools is None:
                    # Model requested tools in a tool-less task: feed the
                    # refusal back as observations; budgets bound the damage.
                    results: Sequence[ToolResult] = tuple(
                        ToolResult(status="failed", error="本任务没有可用工具，请直接给出最终 JSON 输出")
                        for _ in reply.tool_calls
                    )
                else:
                    results = execute_tools(reply.tool_calls)
                for call, result in zip(reply.tool_calls, results):
                    messages.append(
                        Message(
                            role="tool",
                            content=_observation_content(result),
                            tool_call_id=call.id,
                        )
                    )
                if execute_tools is not None and fail_streak.observe(reply.tool_calls, results):
                    return outcome(
                        "exhausted",
                        "tool_fail_streak",
                        error_code=ErrorCode.LOOP_TOOL_FAIL_STREAK,
                        last_error=fail_streak.last_error,
                    )
                if no_progress.observe(signature):
                    return outcome(
                        "exhausted",
                        "no_progress",
                        error_code=ErrorCode.LOOP_NO_PROGRESS,
                        last_error="连续多轮重复相同的工具调用，判定无进展",
                    )
                break  # tool round complete → next turn

            content = reply.content or ""
            problems: str | None = None
            value: Any = None
            try:
                value = task.parser(content)
            except ValueError as exc:  # includes json.JSONDecodeError
                problems = f"not valid JSON: {exc}"
            if problems is None:
                issues = task.validator(value)
                if not issues:
                    if not isinstance(value, dict):
                        problems = "top-level output must be a JSON object"
                    else:
                        return outcome("done", "done", value=value)
                else:
                    problems = "; ".join(issues)

            if no_progress.observe(_answer_signature(content)):
                return outcome(
                    "exhausted",
                    "no_progress",
                    error_code=ErrorCode.LOOP_NO_PROGRESS,
                    last_error="连续多轮输出完全相同且未通过校验，判定无进展",
                )
            if repairs_used < budget.repairs:
                repairs_used += 1
                messages.append(Message(role="assistant", content=content))
                messages.append(_repair_message(problems, content))
                continue  # same turn, repair retry
            return outcome(
                "invalid",
                "schema_violation",
                error_code=ErrorCode.LLM_SCHEMA_VIOLATION,
                last_error=problems,
            )

    return outcome(
        "exhausted",
        "max_turns",
        error_code=ErrorCode.BUDGET_LOOP,
        last_error=f"内环达到 max_turns={budget.max_turns} 仍未产出合法终答",
    )
