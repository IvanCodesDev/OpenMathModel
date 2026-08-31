"""LLM port implementations for development and tests.

Real provider adapters (OpenAI-compatible, DeepSeek, Qwen, ...) arrive with
key management and infra in a later batch; the engine and skills must not
know the difference — that is the whole point of the port.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass
class LlmCall:
    prompt_id: str
    variables: dict[str, Any]


@dataclass
class ChatCall:
    """One ``chat_text`` invocation: label + the full wire message list."""

    label: str
    messages: list[dict[str, str]]


#: A chat script entry: literal reply text, or callable(messages) -> str for
#: scripts that need to react to the conversation (e.g. echo an observation).
ChatEntry = str | Callable[[list[dict[str, str]]], str]


def _play_entry(entry: ChatEntry, messages: list[dict[str, str]]) -> str:
    return entry(messages) if callable(entry) else entry


class StubLlmPort:
    """Deterministic responses keyed by prompt_id.

    A response may be a string or a callable(variables) -> str, so tests can
    react to inputs. Every call is recorded for assertions.

    Conversational calls (``chat_text``) are scripted per label with queue
    semantics (consume in order, repeat the last) — the same duck contract
    EngineLlmPort exposes, so sandbox-agent nodes run against this stub.
    """

    def __init__(
        self,
        responses: Mapping[str, str | Callable[[dict[str, Any]], str]],
        chat_scripts: Mapping[str, list[ChatEntry]] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._chat_scripts = {
            key: list(value) for key, value in (chat_scripts or {}).items()
        }
        self.calls: list[LlmCall] = []
        self.chat_calls: list[ChatCall] = []

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        self.calls.append(LlmCall(prompt_id=prompt_id, variables=dict(variables)))
        try:
            response = self._responses[prompt_id]
        except KeyError:
            raise KeyError(f"StubLlmPort has no response for prompt {prompt_id!r}")
        if callable(response):
            return response(variables)
        return response

    def chat_text(self, messages: list[dict[str, str]], *, label: str) -> str:
        self.chat_calls.append(
            ChatCall(label=label, messages=[dict(m) for m in messages])
        )
        queue = self._chat_scripts.get(label)
        if not queue:
            raise KeyError(f"StubLlmPort has no chat script for label {label!r}")
        entry = queue.pop(0) if len(queue) > 1 else queue[0]
        return _play_entry(entry, messages)


class ScriptedLlmPort:
    """Returns queued responses per prompt_id in order (repeats the last one).

    Useful for repair-path tests: first response malformed, second valid.
    ``chat_text`` follows the same queue discipline, keyed by label.
    """

    def __init__(
        self,
        scripts: Mapping[str, list[str]],
        chat_scripts: Mapping[str, list[ChatEntry]] | None = None,
    ) -> None:
        self._scripts = {key: list(value) for key, value in scripts.items()}
        self._chat_scripts = {
            key: list(value) for key, value in (chat_scripts or {}).items()
        }
        self.calls: list[LlmCall] = []
        self.chat_calls: list[ChatCall] = []

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        self.calls.append(LlmCall(prompt_id=prompt_id, variables=dict(variables)))
        queue = self._scripts.get(prompt_id)
        if not queue:
            raise KeyError(f"ScriptedLlmPort has no script for prompt {prompt_id!r}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def chat_text(self, messages: list[dict[str, str]], *, label: str) -> str:
        self.chat_calls.append(
            ChatCall(label=label, messages=[dict(m) for m in messages])
        )
        queue = self._chat_scripts.get(label)
        if not queue:
            raise KeyError(
                f"ScriptedLlmPort has no chat script for label {label!r}"
            )
        entry = queue.pop(0) if len(queue) > 1 else queue[0]
        return _play_entry(entry, messages)


def stub_response(payload: dict[str, Any], fenced: bool = False) -> str:
    """Build a stub LLM answer; optionally wrapped in a markdown fence."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if fenced:
        return f"```json\n{text}\n```"
    return text
