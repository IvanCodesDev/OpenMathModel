"""网关自身基础设施故障 → 瞬态（回退 + 重试），而不是一次判死。

真实事故（2026-09-05，中转站上的 GLM，本地库 run_e761cc88 / run_e7687f6d）：
- HTTP 401「鉴权服务请求失败: Post …/internal/auth/verify: context deadline exceeded
  (Client.Timeout exceeded while awaiting headers)」——中转站鉴权微服务超时，密钥没错；
- HTTP 500「… failed to connect to `user=postgres …`: FATAL: sorry, too many clients
  already」——中转站自家数据库连接池打满。
旧逻辑把二者与「密钥无效」「boom」一视同仁归 LLM_UPSTREAM_ERROR：不切备用接口、
引擎整次调用不重试，题意解析一次失败整个任务 FAILED，用户点「重试当前阶段」再撞一次。

边界必须守住：「invalid key」式 401 与「boom」式 500 仍是确定性失败（既有测试语义）。
"""

from __future__ import annotations

import httpx
import pytest

from omm_api import llm as llm_module
from omm_api.engine_glue import _API_ERROR_GUIDANCE, _classify_failure
from omm_api.errors import ApiError
from omm_api.llm import (
    ChatOutcome,
    EngineLlmPort,
    LlmConfig,
    LlmEndpoint,
    _upstream_error,
    upstream_failure_is_transient,
)

AUTH_TIMEOUT_BODY = (
    '鉴权服务请求失败: Post "http://ainft-chat-service.apenft-market-production.svc.cluster.local:3210'
    '/v1/internal/auth/verify": context deadline exceeded (Client.Timeout exceeded while awaiting '
    "headers) (request id: 20260905140328310846457c955d568psJBKpAY)"
)
DB_EXHAUSTED_BODY = (
    "Failed to get available channel for model glm-5.3-flash under group default (distributor): "
    "model existence lookup: failed to connect to `user=postgres database=db_ainft_api_prod`:\n\t"
    "172.31.19.59:5432 (postgres-apenft-prod.example.rds.amazonaws.com): server error: "
    "FATAL: sorry, too many clients already"
)

ENDPOINT = LlmEndpoint(
    id="ep_glm",
    name="GLM",
    protocol="openai",
    base_url="https://relay.test/v1",
    api_key="sk-main",
    model="glm-5.3-flash",
)
BACKUP = LlmEndpoint(
    id="ep_backup",
    name="备用接口",
    protocol="openai",
    base_url="https://backup.test/v1",
    api_key="sk-backup",
    model="gpt-test",
)
MESSAGES = [{"role": "user", "content": "你好"}]


def _response(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


def _openai_reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-test",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        },
    )


def _install_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm_module, "_transport_factory", lambda: transport)


# ── 分类：什么算网关瞬态 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, AUTH_TIMEOUT_BODY),
        (500, DB_EXHAUSTED_BODY),
        (503, "The model is temporarily unavailable because today's cost limit has been reached."),
        (502, "Bad Gateway"),
        (504, "upstream request timeout"),
        (500, "系统繁忙，请稍后再试"),
        (503, "Service Unavailable"),
    ],
)
def test_gateway_infrastructure_failures_are_transient(status, message) -> None:
    assert upstream_failure_is_transient(status, message)
    error = _upstream_error(ENDPOINT, _response(status, message))
    assert error.code == "LLM_UPSTREAM_UNAVAILABLE"
    assert "暂时不可用" in error.message and f"HTTP {status}" in error.message
    assert llm_module._should_fall_back(error), "换备用接口大概率能过"
    assert llm_module._is_transient(error), "稍后重来大概率能过"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "invalid key"),
        (401, "bad api key"),
        (403, "account disabled"),
        (404, "model not found"),
        (400, "max_tokens is too large"),
        (500, "boom"),
    ],
)
def test_configuration_and_content_errors_stay_deterministic(status, message) -> None:
    assert not upstream_failure_is_transient(status, message)
    error = _upstream_error(ENDPOINT, _response(status, message))
    assert error.code == "LLM_UPSTREAM_ERROR"
    assert not llm_module._should_fall_back(error)
    assert not llm_module._is_transient(error)


def test_402_still_has_its_own_code() -> None:
    error = _upstream_error(ENDPOINT, _response(402, "Insufficient Balance"))
    assert error.code == "LLM_NO_BALANCE"


def test_failure_class_and_guidance_for_gateway_transient() -> None:
    """步骤失败按 TRANSIENT 归类；指引说清与余额、密钥无关，不再引导去「检查配置」。"""
    error = _upstream_error(ENDPOINT, _response(401, AUTH_TIMEOUT_BODY))
    assert _classify_failure(f"模型接口调用失败：{error.message}") == "TRANSIENT"
    assert _classify_failure(error.message) == "TRANSIENT", "未经包装直达时按文案兜底"
    guidance = _API_ERROR_GUIDANCE["LLM_UPSTREAM_UNAVAILABLE"]
    assert "与余额、密钥无关" in guidance
    assert "检查该接口的配置" not in guidance


# ── 链内回退：主接口网关故障 → 备用接口顶上 ─────────────────────────────────


def test_complete_with_fallback_switches_on_gateway_transient(monkeypatch) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "relay.test":
            return _response(401, AUTH_TIMEOUT_BODY)
        return _openai_reply("备用接口回复")

    _install_transport(monkeypatch, handler)
    config = LlmConfig(endpoints=(ENDPOINT, BACKUP), active_endpoint_id=ENDPOINT.id)

    outcome = llm_module.complete_with_fallback(config, MESSAGES)

    assert outcome.text == "备用接口回复"
    assert outcome.fallback_used is True
    assert hosts == ["relay.test", "backup.test"]


def test_complete_with_fallback_keeps_invalid_key_deterministic(monkeypatch) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return _response(401, "invalid key")

    _install_transport(monkeypatch, handler)
    config = LlmConfig(endpoints=(ENDPOINT, BACKUP), active_endpoint_id=ENDPOINT.id)

    with pytest.raises(ApiError) as excinfo:
        llm_module.complete_with_fallback(config, MESSAGES)

    assert excinfo.value.code == "LLM_UPSTREAM_ERROR"
    assert hosts == ["relay.test"], "密钥错换接口也是错，不烧备用接口"


def test_stream_events_switch_on_gateway_transient(monkeypatch) -> None:
    """对话流式路径同一判定：主接口 500「too many clients」→ 备用接口出正文。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "relay.test":
            return _response(500, DB_EXHAUSTED_BODY)
        body = (
            'data: {"choices":[{"delta":{"content":"备用"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=body.encode()
        )

    _install_transport(monkeypatch, handler)
    config = LlmConfig(endpoints=(ENDPOINT, BACKUP), active_endpoint_id=ENDPOINT.id)

    events = list(llm_module.stream_events(config, MESSAGES))

    meta = next(event for event in events if event["type"] == "meta")
    assert meta["endpoint"] == "备用接口" and meta["fallback_used"] is True
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "备用"
    assert events[-1]["type"] == "done"


# ── 引擎整次调用重试：单接口配置下网关抖一下不判死 ───────────────────────────


class _Template:
    def render(self, variables: dict) -> str:
        return "渲染后的提示词"


class _Registry:
    def get(self, prompt_id: str) -> _Template:
        return _Template()


def test_engine_port_retries_gateway_transient_and_recovers(monkeypatch) -> None:
    """只配了一个接口（用户现场）：第 1 次 401 鉴权超时 → 退避后第 2 次成功，
    事件流有 started/failed 收尾对 + 成功摘要；旧逻辑这里直接上抛、阶段判死。"""
    events: list[dict] = []
    attempts = 0

    def fake_stream(config, messages, on_delta=None, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _upstream_error(ENDPOINT, _response(401, AUTH_TIMEOUT_BODY))
        return ChatOutcome(
            text='{"ok": true}',
            model="glm-5.3-flash",
            endpoint=ENDPOINT,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            elapsed_ms=120,
        )

    sleeps: list[float] = []
    monkeypatch.setattr(llm_module, "stream_complete_with_fallback", fake_stream)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    port = EngineLlmPort(
        LlmConfig(endpoints=(ENDPOINT,), active_endpoint_id=ENDPOINT.id, stream=True),
        _Registry(),
        on_event=events.append,
    )

    text = port.complete("problem_analysis.default", {})

    assert text == '{"ok": true}'
    kinds = [event["kind"] for event in events]
    assert kinds.count("llm_call_started") == 2
    assert kinds.count("llm_call_failed") == 1
    assert kinds[-1] == "llm_call"
    assert sleeps == [2.0]
    failed = next(event for event in events if event["kind"] == "llm_call_failed")
    assert "暂时不可用（HTTP 401）" in failed["error"]
