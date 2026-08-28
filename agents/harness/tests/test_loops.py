"""E2 tests for the inner loop engine: the §5.3 exit table, row by row.

Every test scripts the model (canned replies) and asserts one exact exit:
status + exit_reason + error_code + call/turn accounting. No test asserts
model text content — that is the golden-trace discipline applied at loop
scope. The single-shot profile test is the behavioral anchor for the coming
LlmSkillNode migration (one normal try + one repair try, §2.1).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from omm_agent_harness import LoopBudget, LoopTask, Message, Reply, ToolCall, Usage, run_inner_loop
from omm_agent_core.models import ToolResult

# -- scripted collaborators ----------------------------------------------------


def text_reply(content: str) -> Reply:
    return Reply(content=content, tool_calls=(), usage=Usage(10, 5, 3), model="stub")


def tool_reply(*calls: ToolCall) -> Reply:
    return Reply(content=None, tool_calls=tuple(calls), usage=Usage(10, 5, 3), model="stub")


def call(name: str, call_id: str = "c1", **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ScriptedChat:
    """Feeds canned replies in order and records every messages snapshot."""

    def __init__(self, replies: Sequence[Reply]) -> None:
        self._replies = list(replies)
        self.seen_messages: list[list[Message]] = []

    def __call__(self, messages: Sequence[Message]) -> Reply:
        self.seen_messages.append(list(messages))
        if not self._replies:
            raise AssertionError("chat called more times than scripted")
        return self._replies.pop(0)


def require_keys(*keys: str):
    def validator(value) -> list[str]:
        if not isinstance(value, dict):
            return ["not an object"]
        return [f"missing required key: {key}" for key in keys if key not in value]

    return validator


def make_task(
    *,
    validator=None,
    budget: LoopBudget | None = None,
) -> LoopTask:
    return LoopTask(
        task_id="t1",
        messages=(
            Message(role="system", content="你是测试技能"),
            Message(role="user", content="给出结果 JSON"),
        ),
        validator=validator or require_keys("answer"),
        budget=budget or LoopBudget(),
    )


def executor_returning(*results: ToolResult):
    def execute(calls: Sequence[ToolCall]) -> Sequence[ToolResult]:
        return list(results)[: len(calls)]

    return execute


GOOD = json.dumps({"answer": 42})


# -- §5.3 row: done ------------------------------------------------------------


def test_done_first_turn() -> None:
    chat = ScriptedChat([text_reply(GOOD)])
    outcome = run_inner_loop(make_task(), chat=chat)
    assert outcome.ok
    assert (outcome.status, outcome.exit_reason) == ("done", "done")
    assert outcome.value == {"answer": 42}
    assert outcome.turns == 1 and outcome.llm_calls == 1 and outcome.repairs_used == 0
    assert outcome.usage.prompt_tokens == 10 and outcome.usage.completion_tokens == 5


def test_single_shot_profile_repair_success_matches_llmskillnode() -> None:
    """max_turns=1 + repairs=1 must allow one normal try plus one repair try —
    the exact LlmSkillNode discipline the migration will be verified against."""
    chat = ScriptedChat([text_reply(json.dumps({"wrong": 1})), text_reply(GOOD)])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=1, repairs=1)), chat=chat
    )
    assert outcome.ok
    assert outcome.turns == 1 and outcome.llm_calls == 2 and outcome.repairs_used == 1
    # The repair message carries the fixed D4 feedback format.
    repair_prompt = chat.seen_messages[1][-1]
    assert repair_prompt.role == "user"
    assert "__repair_error" in repair_prompt.content
    assert "__previous_output" in repair_prompt.content


def test_parse_error_repaired() -> None:
    chat = ScriptedChat([text_reply("这不是 JSON"), text_reply(GOOD)])
    outcome = run_inner_loop(make_task(), chat=chat)
    assert outcome.ok and outcome.repairs_used == 1


# -- §5.3 row: schema violation (E120) ----------------------------------------


def test_invalid_after_repair_budget_spent() -> None:
    chat = ScriptedChat(
        [text_reply(json.dumps({"wrong": 1})), text_reply(json.dumps({"still": 2}))]
    )
    outcome = run_inner_loop(make_task(), chat=chat)
    assert (outcome.status, outcome.exit_reason) == ("invalid", "schema_violation")
    assert outcome.error_code == "E120"
    assert "missing required key" in (outcome.last_error or "")
    assert outcome.llm_calls == 2 and outcome.repairs_used == 1


def test_non_object_top_level_is_violation() -> None:
    chat = ScriptedChat([text_reply(json.dumps([1, 2])), text_reply(GOOD)])
    outcome = run_inner_loop(make_task(validator=lambda value: []), chat=chat)
    # First answer parses and passes the (permissive) validator but is not an
    # object → repaired; second is fine.
    assert outcome.ok and outcome.repairs_used == 1


# -- tool rounds ---------------------------------------------------------------


def test_tool_round_then_done_feeds_observation() -> None:
    chat = ScriptedChat([tool_reply(call("ws_read", path="a.txt")), text_reply(GOOD)])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=3)),
        chat=chat,
        execute_tools=executor_returning(
            ToolResult(status="succeeded", output={"text": "file body"})
        ),
    )
    assert outcome.ok and outcome.turns == 2 and outcome.llm_calls == 2
    second_turn_messages = chat.seen_messages[1]
    observation = second_turn_messages[-1]
    assert observation.role == "tool" and observation.tool_call_id == "c1"
    assert "file body" in observation.content


def test_oversized_observation_is_truncated() -> None:
    huge = "x" * 10_000
    chat = ScriptedChat([tool_reply(call("ws_read")), text_reply(GOOD)])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=3)),
        chat=chat,
        execute_tools=executor_returning(
            ToolResult(status="succeeded", output={"text": huge})
        ),
    )
    assert outcome.ok
    observation = chat.seen_messages[1][-1]
    assert len(observation.content) < 10_000
    assert "已截断" in observation.content


def test_tool_calls_without_executor_get_refusal_observation() -> None:
    chat = ScriptedChat([tool_reply(call("python_run")), text_reply(GOOD)])
    outcome = run_inner_loop(make_task(budget=LoopBudget(max_turns=3)), chat=chat)
    assert outcome.ok
    refusal = chat.seen_messages[1][-1]
    assert refusal.role == "tool" and "没有可用工具" in refusal.content


# -- §5.3 row: max_turns (E330) ------------------------------------------------


def test_max_turns_exhausted() -> None:
    chat = ScriptedChat(
        [
            tool_reply(call("ws_read", path="a.txt", call_id_marker=1)),
            tool_reply(call("ws_read", path="b.txt", call_id_marker=2)),
        ]
    )
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=2, no_progress_k=5)),
        chat=chat,
        execute_tools=executor_returning(
            ToolResult(status="succeeded", output={"ok": True})
        ),
    )
    assert (outcome.status, outcome.exit_reason) == ("exhausted", "max_turns")
    assert outcome.error_code == "E330"
    assert outcome.turns == 2


# -- §5.3 row: no progress (E331) ----------------------------------------------


def test_no_progress_identical_tool_signatures() -> None:
    same = lambda: tool_reply(call("ws_read", path="same.txt"))  # noqa: E731
    chat = ScriptedChat([same(), same(), same()])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=8, no_progress_k=3)),
        chat=chat,
        execute_tools=executor_returning(
            ToolResult(status="succeeded", output={"ok": True})
        ),
    )
    assert (outcome.status, outcome.exit_reason) == ("exhausted", "no_progress")
    assert outcome.error_code == "E331"
    assert outcome.turns == 3  # stopped well before max_turns=8: money saved


def test_no_progress_identical_invalid_answers() -> None:
    bad = json.dumps({"wrong": 1})
    chat = ScriptedChat([text_reply(bad), text_reply(bad), text_reply(bad)])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=8, repairs=5, no_progress_k=3)),
        chat=chat,
    )
    assert (outcome.status, outcome.exit_reason) == ("exhausted", "no_progress")
    assert outcome.error_code == "E331"


# -- §5.3 row: tool fail streak (E332) ------------------------------------------


def test_tool_fail_streak_same_tool() -> None:
    failing = lambda marker: tool_reply(call("python_run", attempt=marker))  # noqa: E731
    chat = ScriptedChat([failing(1), failing(2), failing(3)])
    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=8, tool_fail_m=3, no_progress_k=99)),
        chat=chat,
        execute_tools=executor_returning(
            ToolResult(status="failed", error="SyntaxError: invalid syntax")
        ),
    )
    assert (outcome.status, outcome.exit_reason) == ("exhausted", "tool_fail_streak")
    assert outcome.error_code == "E332"
    assert "SyntaxError" in (outcome.last_error or "")


def test_tool_success_resets_fail_streak() -> None:
    chat = ScriptedChat(
        [
            tool_reply(call("python_run", attempt=1)),
            tool_reply(call("python_run", attempt=2)),
            tool_reply(call("python_run", attempt=3)),
            text_reply(GOOD),
        ]
    )
    results = iter(
        [
            ToolResult(status="failed", error="boom"),
            ToolResult(status="succeeded", output={"ok": True}),
            ToolResult(status="failed", error="boom"),
        ]
    )

    def execute(calls):
        return [next(results)]

    outcome = run_inner_loop(
        make_task(budget=LoopBudget(max_turns=8, tool_fail_m=2, no_progress_k=99)),
        chat=chat,
        execute_tools=execute,
    )
    assert outcome.ok  # streak never reached 2 in a row


# -- §5.3 row: cancelled ---------------------------------------------------------


def test_external_cancellation() -> None:
    chat = ScriptedChat([text_reply(GOOD)])
    outcome = run_inner_loop(make_task(), chat=chat, cancelled=lambda: True)
    assert (outcome.status, outcome.exit_reason) == ("cancelled", "cancelled")
    assert outcome.error_code is None
    assert outcome.llm_calls == 0  # cancelled before spending anything


# -- usage accounting -----------------------------------------------------------


def test_usage_accumulates_across_calls() -> None:
    chat = ScriptedChat([text_reply("bad"), text_reply(GOOD)])
    outcome = run_inner_loop(make_task(), chat=chat)
    assert outcome.usage.prompt_tokens == 20
    assert outcome.usage.completion_tokens == 10
    assert outcome.usage.duration_ms == 6


def test_loop_never_mutates_task_messages() -> None:
    task = make_task()
    before = tuple(task.messages)
    chat = ScriptedChat([text_reply("bad"), text_reply(GOOD)])
    run_inner_loop(task, chat=chat)
    assert task.messages == before
