"""ModelGateway: retries→E110, quota→E140, refusal→E130, routing, record/replay."""

from __future__ import annotations

import json
from typing import Any

import pytest
from omm_agent_core.errors import AgentError, ErrorCode
from omm_agent_harness.gateway import (
    CallBudget,
    GatewayConfig,
    Message,
    ModelGateway,
    ModelRouting,
    ReplayCassette,
    TransportFailure,
)

MESSAGES = [Message(role="user", content="你好")]


def ok_payload(content: str = "答复", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "m-default",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    payload.update(overrides)
    return payload


class FakeSender:
    """Scripted (status, payload) or TransportFailure per call; records requests."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: Any, timeout_s: float) -> tuple[int, dict[str, Any]]:
        self.requests.append({"url": url, "headers": dict(headers), "body": dict(body)})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def make_gateway(sender: FakeSender, **kwargs: Any) -> tuple[ModelGateway, list[float], list[dict[str, Any]]]:
    sleeps: list[float] = []
    records: list[dict[str, Any]] = []
    config = kwargs.pop(
        "config",
        GatewayConfig(
            base_url="https://llm.example/v1",
            api_key="sk-test",
            routing=ModelRouting(
                default="m-default",
                fast="m-fast",
                code="m-code",
                prefix_tiers={"problem_analysis": "fast"},
            ),
        ),
    )
    gateway = ModelGateway(
        config,
        sender=sender,
        sleeper=sleeps.append,
        usage_listeners=[records.append],
        **kwargs,
    )
    return gateway, sleeps, records


def test_success_parses_content_usage_and_audits():
    sender = FakeSender([(200, ok_payload())])
    gateway, sleeps, records = make_gateway(sender)

    reply = gateway.chat(MESSAGES, prompt_id="paper_writing.section")

    assert reply.content == "答复"
    assert reply.usage.prompt_tokens == 11
    assert reply.usage.completion_tokens == 7
    assert not sleeps
    assert len(records) == 1
    assert records[0]["tool"] == "llm.chat"
    assert records[0]["prompt_id"] == "paper_writing.section"
    assert records[0]["prompt_hash"]
    # auth header attached, never logged in the audit record
    assert sender.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert "Authorization" not in json.dumps(records[0])


def test_tool_calls_parsed_from_json_arguments():
    payload = ok_payload()
    payload["choices"][0]["message"] = {
        "content": None,
        "tool_calls": [
            {"id": "c1", "function": {"name": "ws_read", "arguments": '{"path": "a.py"}'}}
        ],
    }
    sender = FakeSender([(200, payload)])
    gateway, _, _ = make_gateway(sender)

    reply = gateway.chat(MESSAGES)

    assert reply.content is None
    assert reply.tool_calls[0].name == "ws_read"
    assert reply.tool_calls[0].arguments == {"path": "a.py"}


def test_retry_on_429_and_5xx_then_success():
    sender = FakeSender([(429, {}), (503, {}), (200, ok_payload())])
    gateway, sleeps, _ = make_gateway(sender)

    reply = gateway.chat(MESSAGES)

    assert reply.content == "答复"
    assert sleeps == [0.5, 1.0]  # exponential backoff, base 0.5


def test_retries_exhausted_raises_e110_with_context():
    sender = FakeSender([(500, {}), (500, {}), (500, {})])
    gateway, sleeps, _ = make_gateway(sender)

    with pytest.raises(AgentError) as excinfo:
        gateway.chat(MESSAGES, prompt_id="experimenting.run")
    assert excinfo.value.code is ErrorCode.LLM_NETWORK
    assert excinfo.value.context["attempts"] == 3
    assert len(sleeps) == 2  # no sleep after the final attempt


def test_transport_failures_are_retried():
    sender = FakeSender([TransportFailure("boom"), (200, ok_payload())])
    gateway, _, _ = make_gateway(sender)

    assert gateway.chat(MESSAGES).content == "答复"


def test_quota_body_raises_e140():
    sender = FakeSender([(403, {"error": {"code": "insufficient_quota"}})])
    gateway, _, _ = make_gateway(sender)

    with pytest.raises(AgentError) as excinfo:
        gateway.chat(MESSAGES)
    assert excinfo.value.code is ErrorCode.LLM_PROVIDER_QUOTA


def test_content_filter_raises_e130():
    payload = ok_payload()
    payload["choices"][0]["finish_reason"] = "content_filter"
    sender = FakeSender([(200, payload)])
    gateway, _, _ = make_gateway(sender)

    with pytest.raises(AgentError) as excinfo:
        gateway.chat(MESSAGES)
    assert excinfo.value.code is ErrorCode.LLM_CONTENT_REFUSAL


def test_routing_prefix_match_and_explicit_tier_override():
    sender = FakeSender([(200, ok_payload()), (200, ok_payload()), (200, ok_payload())])
    gateway, _, _ = make_gateway(sender)

    gateway.chat(MESSAGES, prompt_id="problem_analysis.default")  # prefix → fast
    gateway.chat(MESSAGES, prompt_id="problem_analysis.default", tier="code")  # override
    gateway.chat(MESSAGES, prompt_id="unmapped.skill")  # default

    models = [request["body"]["model"] for request in sender.requests]
    assert models == ["m-fast", "m-code", "m-default"]


def test_unknown_tier_fails_fast():
    with pytest.raises(ValueError, match="unknown model tier"):
        ModelRouting(default="m").resolve(tier="turbo")


def test_structured_output_request_shape_and_opt_out():
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    sender = FakeSender([(200, ok_payload())])
    gateway, _, _ = make_gateway(sender)
    gateway.chat(MESSAGES, output_schema=schema, budget=CallBudget(max_output_tokens=99))
    body = sender.requests[0]["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["max_tokens"] == 99

    sender2 = FakeSender([(200, ok_payload())])
    config = GatewayConfig(
        base_url="https://llm.example/v1",
        api_key="",
        routing=ModelRouting(default="m-default"),
        use_response_format=False,
    )
    gateway2, _, _ = make_gateway(sender2, config=config)
    gateway2.chat(MESSAGES, output_schema=schema)
    assert "response_format" not in sender2.requests[0]["body"]


def test_record_then_replay_roundtrip(tmp_path):
    cassette_path = tmp_path / "golden.jsonl"
    sender = FakeSender([(200, ok_payload("录制的答案"))])
    recording, _, _ = make_gateway(sender, record_path=cassette_path)
    recorded = recording.chat(MESSAGES, prompt_id="problem_analysis.default")

    replaying, _, records = make_gateway(
        FakeSender([]), replay=ReplayCassette.load(cassette_path)
    )
    replayed = replaying.chat(MESSAGES, prompt_id="problem_analysis.default")

    assert replayed == recorded
    assert records[0]["replayed"] is True

    with pytest.raises(RuntimeError, match="replay cassette has no entry"):
        replaying.chat([Message(role="user", content="另一个问题")])


def test_record_and_replay_modes_are_exclusive(tmp_path):
    with pytest.raises(ValueError, match="mutually exclusive"):
        ModelGateway(
            GatewayConfig(
                base_url="https://llm.example/v1",
                api_key="",
                routing=ModelRouting(default="m"),
            ),
            record_path=tmp_path / "a.jsonl",
            replay=ReplayCassette(),
        )


def test_complete_requires_renderer_and_uses_it():
    sender = FakeSender([(200, ok_payload("完成文本"))])
    gateway, _, _ = make_gateway(sender)
    with pytest.raises(RuntimeError, match="prompt_renderer"):
        gateway.complete("problem_analysis.default", {"q": 1})

    def renderer(prompt_id: str, variables: dict[str, Any]) -> list[Message]:
        return [Message(role="user", content=f"{prompt_id}:{variables['q']}")]

    sender2 = FakeSender([(200, ok_payload("完成文本"))])
    gateway2, _, _ = make_gateway(sender2, prompt_renderer=renderer)
    assert gateway2.complete("problem_analysis.default", {"q": 1}) == "完成文本"
    assert sender2.requests[0]["body"]["model"] == "m-fast"  # routed by prompt_id


def test_from_env_validates_required_settings():
    with pytest.raises(ValueError, match="OMM_LLM_BASE_URL, OMM_LLM_MODEL"):
        GatewayConfig.from_env({})

    config = GatewayConfig.from_env(
        {
            "OMM_LLM_BASE_URL": "https://llm.example/v1",
            "OMM_LLM_API_KEY": "sk",
            "OMM_LLM_MODEL": "m-default",
            "OMM_LLM_MODEL_FAST": "m-fast",
        }
    )
    assert config.routing.default == "m-default"
    assert config.routing.fast == "m-fast"
    assert config.routing.strong is None
