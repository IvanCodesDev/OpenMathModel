"""ModelGateway: the execution plane's single LLM egress (design §4.1, D1.1).

Scope of THIS layer, and nothing more:

- provider adaptation, OpenAI-compatible chat completions first (DeepSeek /
  Qwen / Kimi / vLLM / relay stations all speak this shape);
- model routing across fast/strong/code/vision tiers, by prompt_id prefix
  with per-call override;
- best-effort structured output via ``response_format`` — schema VALIDATION
  stays in the loop layer, the response_format hint is never trusted;
- network retries: 429/5xx/transport errors, exponential backoff, at most
  ``max_retries`` retries, then ``AgentError(E110)``. Structure repair (E120)
  belongs to the loop, stage retries to the graph — the three layers never
  stack (§4.1 retry discipline);
- record/replay cassettes for evals;
- usage metering pushed to the budget governor and trace hub.

Layering decision (§4.1 收编决策 v3.1): ``omm_api/llm.py`` remains the
control-plane adapter for user-configured chat; this module must never
import ``omm_api``. HTTP transport is injected as a plain callable so tests
exercise retry/parse logic without network or httpx mocking.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omm_agent_core.errors import AgentError, ErrorCode

__all__ = [
    "CallBudget",
    "GatewayConfig",
    "Message",
    "ModelGateway",
    "ModelRouting",
    "Reply",
    "ReplayCassette",
    "ToolCall",
    "TransportFailure",
    "Usage",
    "httpx_sender",
]


# ── D1.1 shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            wire["tool_call_id"] = self.tool_call_id
        return wire


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Reply:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    model: str


@dataclass(frozen=True)
class CallBudget:
    max_output_tokens: int = 4096
    timeout_s: float = 120.0


# ── configuration & routing ────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRouting:
    """Resolve which model serves a call.

    Precedence: explicit per-call tier override > longest matching
    prompt_id prefix in ``prefix_tiers`` > default model. Unknown tier names
    fail fast — a typo in assembly must not silently fall back (§4.9).
    """

    default: str
    fast: str | None = None
    strong: str | None = None
    code: str | None = None
    vision: str | None = None
    prefix_tiers: Mapping[str, str] = field(default_factory=dict)

    def model_for_tier(self, tier: str) -> str:
        if tier == "default":
            return self.default
        if tier in ("fast", "strong", "code", "vision"):
            model = getattr(self, tier)
            if model is None:
                # Tier not provisioned: honest fallback to default, the
                # assembler decided not to differentiate this tier.
                return self.default
            return str(model)
        raise ValueError(f"unknown model tier {tier!r}")

    def resolve(self, prompt_id: str | None = None, tier: str | None = None) -> str:
        if tier is not None:
            return self.model_for_tier(tier)
        if prompt_id:
            best: str | None = None
            for prefix in sorted(self.prefix_tiers, key=len, reverse=True):
                if prompt_id.startswith(prefix):
                    best = self.prefix_tiers[prefix]
                    break
            if best is not None:
                return self.model_for_tier(best)
        return self.default


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str
    routing: ModelRouting
    max_retries: int = 2  # network-level retries AFTER the first attempt
    backoff_base_s: float = 0.5
    use_response_format: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GatewayConfig":
        """Assembly-time validation: missing base facts abort startup (§4.9)."""
        base_url = env.get("OMM_LLM_BASE_URL", "").strip()
        model = env.get("OMM_LLM_MODEL", "").strip()
        if not base_url or not model:
            missing = [
                name
                for name, value in (
                    ("OMM_LLM_BASE_URL", base_url),
                    ("OMM_LLM_MODEL", model),
                )
                if not value
            ]
            raise ValueError(f"gateway config missing: {', '.join(missing)}")
        return cls(
            base_url=base_url,
            api_key=env.get("OMM_LLM_API_KEY", "").strip(),
            routing=ModelRouting(
                default=model,
                fast=env.get("OMM_LLM_MODEL_FAST", "").strip() or None,
                strong=env.get("OMM_LLM_MODEL_STRONG", "").strip() or None,
                code=env.get("OMM_LLM_MODEL_CODE", "").strip() or None,
                vision=env.get("OMM_LLM_MODEL_VISION", "").strip() or None,
            ),
        )


# ── transport ───────────────────────────────────────────────────────────────

#: (url, headers, json_body, timeout_s) -> (status_code, parsed_json_body).
#: Implementations raise TransportFailure for network-level problems.
HttpSender = Callable[[str, Mapping[str, str], Mapping[str, Any], float], tuple[int, dict[str, Any]]]


class TransportFailure(Exception):
    """Network-level failure (DNS/conn/timeout); retried, then E110."""


def httpx_sender(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout_s: float) -> tuple[int, dict[str, Any]]:
    """Default transport; the only place httpx is touched."""
    import httpx

    try:
        response = httpx.post(url, headers=dict(headers), json=dict(body), timeout=timeout_s)
    except httpx.HTTPError as exc:  # timeouts, connect errors, protocol errors
        raise TransportFailure(f"{type(exc).__name__}: {exc}") from exc
    try:
        parsed = response.json()
    except ValueError:
        parsed = {"raw": response.text[:2000]}
    return response.status_code, parsed


# ── record / replay ─────────────────────────────────────────────────────────


def request_fingerprint(
    model: str,
    messages: Sequence[Message],
    tools: Sequence[Mapping[str, Any]] | None,
    output_schema: Mapping[str, Any] | None,
) -> str:
    canonical = json.dumps(
        {
            "model": model,
            "messages": [m.to_wire() for m in messages],
            "tools": list(tools) if tools else None,
            "output_schema": dict(output_schema) if output_schema else None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReplayCassette:
    """JSONL cassette of recorded replies keyed by request fingerprint.

    Record mode appends after each live call; replay mode looks up by key
    and a miss is a hard error — evals must never silently go to network.
    """

    def __init__(self, entries: dict[str, Reply] | None = None) -> None:
        self._entries: dict[str, Reply] = dict(entries or {})

    @classmethod
    def load(cls, path: str | Path) -> "ReplayCassette":
        entries: dict[str, Reply] = {}
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            reply = record["reply"]
            entries[record["key"]] = Reply(
                content=reply.get("content"),
                tool_calls=tuple(
                    ToolCall(id=c["id"], name=c["name"], arguments=dict(c["arguments"]))
                    for c in reply.get("tool_calls", [])
                ),
                usage=Usage(**reply["usage"]),
                model=reply["model"],
            )
        return cls(entries)

    def lookup(self, key: str) -> Reply | None:
        return self._entries.get(key)

    @staticmethod
    def append(path: str | Path, key: str, reply: Reply, *, prompt_id: str | None) -> None:
        record = {
            "key": key,
            "prompt_id": prompt_id,
            "reply": {
                "content": reply.content,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in reply.tool_calls
                ],
                "usage": {
                    "prompt_tokens": reply.usage.prompt_tokens,
                    "completion_tokens": reply.usage.completion_tokens,
                    "duration_ms": reply.usage.duration_ms,
                },
                "model": reply.model,
            },
        }
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── gateway ─────────────────────────────────────────────────────────────────

#: Called after every successful reply with structured call facts.
UsageListener = Callable[[dict[str, Any]], None]

#: Renders (prompt_id, variables) into chat messages; lives in skills, is
#: injected here so complete() can satisfy the core LlmPort protocol.
PromptRenderer = Callable[[str, dict[str, Any]], list[Message]]

_QUOTA_MARKERS = ("insufficient_quota", "billing", "quota_exceeded")


class ModelGateway:
    """Implements and extends the core ``LlmPort`` (D1.1)."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        sender: HttpSender = httpx_sender,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        prompt_renderer: PromptRenderer | None = None,
        usage_listeners: Sequence[UsageListener] = (),
        record_path: str | Path | None = None,
        replay: ReplayCassette | None = None,
    ) -> None:
        if record_path is not None and replay is not None:
            raise ValueError("record and replay modes are mutually exclusive")
        self._config = config
        self._sender = sender
        self._sleeper = sleeper
        self._clock = clock
        self._prompt_renderer = prompt_renderer
        self._usage_listeners = list(usage_listeners)
        self._record_path = Path(record_path) if record_path is not None else None
        self._replay = replay

    # -- LlmPort ------------------------------------------------------------

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        if self._prompt_renderer is None:
            raise RuntimeError(
                "ModelGateway.complete requires a prompt_renderer; "
                "assembly must inject one (prompt resolution lives in skills)"
            )
        messages = self._prompt_renderer(prompt_id, variables)
        reply = self.chat(messages, prompt_id=prompt_id)
        return reply.content or ""

    # -- chat (D1.1) ----------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        budget: CallBudget | None = None,
        prompt_id: str | None = None,
        tier: str | None = None,
    ) -> Reply:
        budget = budget or CallBudget()
        model = self._config.routing.resolve(prompt_id=prompt_id, tier=tier)
        key = request_fingerprint(model, messages, tools, output_schema)

        if self._replay is not None:
            recorded = self._replay.lookup(key)
            if recorded is None:
                raise RuntimeError(
                    f"replay cassette has no entry for fingerprint {key[:12]}… "
                    f"(prompt_id={prompt_id!r}); refusing to hit the network"
                )
            self._notify(recorded, prompt_id=prompt_id, prompt_hash=key, replayed=True)
            return recorded

        body = self._build_body(model, messages, tools, output_schema, budget)
        started = self._clock()
        status, payload = self._send_with_retries(body, budget.timeout_s, prompt_id)
        duration_ms = int((self._clock() - started) * 1000)
        reply = self._parse_reply(status, payload, model, duration_ms, prompt_id)

        if self._record_path is not None:
            ReplayCassette.append(self._record_path, key, reply, prompt_id=prompt_id)
        self._notify(reply, prompt_id=prompt_id, prompt_hash=key, replayed=False)
        return reply

    # -- internals ------------------------------------------------------------

    def _build_body(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None,
        output_schema: Mapping[str, Any] | None,
        budget: CallBudget,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_wire() for m in messages],
            "max_tokens": budget.max_output_tokens,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": dict(tool)} for tool in tools
            ]
        if output_schema is not None and self._config.use_response_format:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": dict(output_schema)},
            }
        return body

    def _send_with_retries(
        self, body: Mapping[str, Any], timeout_s: float, prompt_id: str | None
    ) -> tuple[int, dict[str, Any]]:
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        last_problem = ""
        attempts = 1 + self._config.max_retries
        for attempt in range(attempts):
            try:
                status, payload = self._sender(url, headers, body, timeout_s)
            except TransportFailure as exc:
                last_problem = str(exc)
            else:
                if status == 429 or status >= 500:
                    last_problem = f"HTTP {status}"
                else:
                    return status, payload
            if attempt < attempts - 1:
                self._sleeper(self._config.backoff_base_s * (2**attempt))
        raise AgentError(
            ErrorCode.LLM_NETWORK,
            f"{last_problem} after {attempts} attempts",
            context={"prompt_id": prompt_id, "attempts": attempts},
        )

    @staticmethod
    def _parse_reply(
        status: int,
        payload: Mapping[str, Any],
        model: str,
        duration_ms: int,
        prompt_id: str | None,
    ) -> Reply:
        if status != 200:
            detail = json.dumps(payload, ensure_ascii=False)[:500]
            lowered = detail.lower()
            if any(marker in lowered for marker in _QUOTA_MARKERS):
                raise AgentError(
                    ErrorCode.LLM_PROVIDER_QUOTA, detail, context={"prompt_id": prompt_id}
                )
            raise AgentError(
                ErrorCode.LLM_NETWORK,
                f"HTTP {status}: {detail}",
                context={"prompt_id": prompt_id},
            )

        choices = payload.get("choices") or []
        if not choices:
            raise AgentError(
                ErrorCode.LLM_NETWORK,
                "provider returned no choices",
                context={"prompt_id": prompt_id},
            )
        choice = choices[0]
        if choice.get("finish_reason") == "content_filter":
            raise AgentError(
                ErrorCode.LLM_CONTENT_REFUSAL, "", context={"prompt_id": prompt_id}
            )

        message = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                arguments = {"__unparsed": str(raw_args)[:2000]}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=arguments,
                )
            )

        usage_raw = payload.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            duration_ms=duration_ms,
        )
        return Reply(
            content=message.get("content"),
            tool_calls=tuple(tool_calls),
            usage=usage,
            model=str(payload.get("model", model)),
        )

    def _notify(
        self, reply: Reply, *, prompt_id: str | None, prompt_hash: str, replayed: bool
    ) -> None:
        record = {
            "tool": "llm.chat",
            "prompt_id": prompt_id,
            "model": reply.model,
            "prompt_hash": prompt_hash,
            "prompt_tokens": reply.usage.prompt_tokens,
            "completion_tokens": reply.usage.completion_tokens,
            "duration_ms": reply.usage.duration_ms,
            "replayed": replayed,
        }
        for listener in self._usage_listeners:
            listener(dict(record))
