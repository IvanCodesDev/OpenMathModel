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


class StubLlmPort:
    """Deterministic responses keyed by prompt_id.

    A response may be a string or a callable(variables) -> str, so tests can
    react to inputs. Every call is recorded for assertions.
    """

    def __init__(
        self, responses: Mapping[str, str | Callable[[dict[str, Any]], str]]
    ) -> None:
        self._responses = dict(responses)
        self.calls: list[LlmCall] = []

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        self.calls.append(LlmCall(prompt_id=prompt_id, variables=dict(variables)))
        try:
            response = self._responses[prompt_id]
        except KeyError:
            raise KeyError(f"StubLlmPort has no response for prompt {prompt_id!r}")
        if callable(response):
            return response(variables)
        return response


class ScriptedLlmPort:
    """Returns queued responses per prompt_id in order (repeats the last one).

    Useful for repair-path tests: first response malformed, second valid.
    """

    def __init__(self, scripts: Mapping[str, list[str]]) -> None:
        self._scripts = {key: list(value) for key, value in scripts.items()}
        self.calls: list[LlmCall] = []

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        self.calls.append(LlmCall(prompt_id=prompt_id, variables=dict(variables)))
        queue = self._scripts.get(prompt_id)
        if not queue:
            raise KeyError(f"ScriptedLlmPort has no script for prompt {prompt_id!r}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


def stub_response(payload: dict[str, Any], fenced: bool = False) -> str:
    """Build a stub LLM answer; optionally wrapped in a markdown fence."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if fenced:
        return f"```json\n{text}\n```"
    return text
