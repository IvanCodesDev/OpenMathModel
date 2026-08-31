"""自定义模型接口：协议映射、对话回复、流式输出、备用回退与中转站门控。

上游一律用 httpx.MockTransport 模拟（经 omm_api.llm._transport_factory 注入），
测试不出网；SSE 由 TestClient 缓冲后按行解析。唯一的例外是文末「系统代理绕过」
一节——代理挂载只在真实 transport 上才生效（注入 MockTransport 时 httpx 根本
不读环境代理），所以那几例用 127.0.0.1 上的临时 HTTP 服务器验证，仍不出网。
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from omm_api import llm as llm_module
from omm_api.llm import (
    LlmEndpoint,
    build_chat_request,
    parse_custom_headers,
    parse_chat_response,
    parse_stream_data,
)

MESSAGES = [{"role": "user", "content": "你好"}]


def _endpoint(**overrides) -> LlmEndpoint:
    base = dict(
        id="ep_test",
        name="测试网关",
        protocol="openai",
        base_url="https://gateway.test/v1",
        api_key="sk-test",
        model="gpt-test",
    )
    base.update(overrides)
    return LlmEndpoint(**base)


# ── 纯函数：请求构造与响应解析 ──────────────────────────────────────────────


def test_build_request_openai_headers_path_and_body():
    endpoint = _endpoint(organization="org-1", headers="X-API-Source: OMM; X-Trace: 1")
    url, headers, body = build_chat_request(endpoint, MESSAGES, stream=True)
    assert url == "https://gateway.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["OpenAI-Organization"] == "org-1"
    assert headers["X-API-Source"] == "OMM" and headers["X-Trace"] == "1"
    assert body == {"model": "gpt-test", "messages": MESSAGES, "stream": True}


def test_build_request_respects_path_prefix_and_model_override():
    endpoint = _endpoint(path_prefix="/api/v3/chat")
    url, _, body = build_chat_request(endpoint, MESSAGES, stream=False, model="gpt-override")
    assert url == "https://gateway.test/v1/api/v3/chat"
    assert body["model"] == "gpt-override"


def test_build_request_bare_domain_defaults_to_v1_path():
    """裸域名是中转站配置的最常见写法，必须自动补全 /v1/chat/completions。"""
    bare = _endpoint(base_url="https://gateway.test")
    url, _, _ = build_chat_request(bare, MESSAGES, stream=False)
    assert url == "https://gateway.test/v1/chat/completions"

    with_path = _endpoint(base_url="https://gateway.test/v1")
    url, _, _ = build_chat_request(with_path, MESSAGES, stream=False)
    assert url == "https://gateway.test/v1/chat/completions"

    custom_path = _endpoint(base_url="https://gateway.test/openai")
    url, _, _ = build_chat_request(custom_path, MESSAGES, stream=False)
    assert url == "https://gateway.test/openai/chat/completions"


def test_build_request_anthropic_shape():
    endpoint = _endpoint(protocol="anthropic", base_url="https://api.anthropic.com")
    messages = [{"role": "system", "content": "身份设定"}, *MESSAGES]
    url, headers, body = build_chat_request(endpoint, messages, stream=False)
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "身份设定"
    assert body["messages"] == MESSAGES, "system 消息不进 messages 数组"
    assert body["max_tokens"] > 0, "Anthropic 必须显式 max_tokens"


def test_build_request_gemini_url_key_and_contents():
    endpoint = _endpoint(protocol="gemini", base_url="https://generativelanguage.googleapis.com")
    messages = [{"role": "system", "content": "身份"}, {"role": "assistant", "content": "早"}, *MESSAGES]
    url, _, body = build_chat_request(endpoint, messages, stream=True)
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/gpt-test:streamGenerateContent"
        "?key=sk-test&alt=sse"
    )
    assert body["systemInstruction"] == {"parts": [{"text": "身份"}]}
    assert [c["role"] for c in body["contents"]] == ["model", "user"]


def test_build_request_missing_model_is_actionable_error():
    with pytest.raises(Exception) as excinfo:
        build_chat_request(_endpoint(model=""), MESSAGES, stream=False)
    assert "默认模型" in str(excinfo.value)


IMAGES = [{"media_type": "image/png", "data": "aGVsbG8=", "name": "题面.png"}]


def test_build_request_openai_attaches_images_to_last_user_message():
    messages = [
        {"role": "system", "content": "身份"},
        {"role": "user", "content": "看第一张"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "这张图里是什么"},
    ]
    snapshot = json.loads(json.dumps(messages))
    _, _, body = build_chat_request(_endpoint(), messages, stream=False, images=IMAGES)
    assert messages == snapshot, "输入 messages 不能被改写：回退链每个接口要重新组装"
    content = body["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "这张图里是什么"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
    }
    assert body["messages"][1]["content"] == "看第一张", "历史 user 消息保持纯文本"


def test_build_request_anthropic_puts_images_before_text():
    endpoint = _endpoint(protocol="anthropic", base_url="https://api.anthropic.com")
    messages = [{"role": "system", "content": "身份"}, *MESSAGES]
    _, _, body = build_chat_request(endpoint, messages, stream=False, images=IMAGES)
    content = body["messages"][-1]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}
    assert content[-1] == {"type": "text", "text": "你好"}


def test_build_request_gemini_inline_data_parts():
    endpoint = _endpoint(protocol="gemini", base_url="https://generativelanguage.googleapis.com")
    _, _, body = build_chat_request(endpoint, MESSAGES, stream=False, images=IMAGES)
    parts = body["contents"][-1]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}
    assert parts[-1] == {"text": "你好"}


def test_build_request_without_images_keeps_plain_content():
    _, _, body = build_chat_request(_endpoint(), MESSAGES, stream=False, images=None)
    assert body["messages"] == MESSAGES


def test_parse_custom_headers_tolerates_formats():
    assert parse_custom_headers("") == {}
    assert parse_custom_headers("A: 1\nB:2; C: 3") == {"A": "1", "B": "2", "C": "3"}
    assert parse_custom_headers("no-colon-line") == {}


def test_parse_chat_response_all_protocols():
    text, reasoning, usage, model = parse_chat_response(
        "openai",
        {
            "model": "gpt-x",
            "choices": [{"message": {"content": "回答", "reasoning_content": "先想一想"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )
    assert (text, reasoning, model) == ("回答", "先想一想", "gpt-x")
    assert usage == {"prompt_tokens": 3, "completion_tokens": 5}

    text, reasoning, usage, _ = parse_chat_response(
        "anthropic",
        {
            "content": [
                {"type": "thinking", "thinking": "推理链"},
                {"type": "text", "text": "回"},
                {"type": "text", "text": "答"},
            ],
            "usage": {"input_tokens": 2, "output_tokens": 4},
        },
    )
    assert (text, reasoning) == ("回答", "推理链")
    assert usage == {"prompt_tokens": 2, "completion_tokens": 4}

    text, reasoning, usage, _ = parse_chat_response(
        "gemini",
        {
            "candidates": [
                {"content": {"parts": [{"text": "思考摘要", "thought": True}, {"text": "回答"}]}}
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
        },
    )
    assert (text, reasoning) == ("回答", "思考摘要")
    assert usage == {"prompt_tokens": 1, "completion_tokens": 2}


def test_parse_stream_data_all_protocols():
    delta, reasoning, done, _ = parse_stream_data(
        "openai", json.dumps({"choices": [{"delta": {"content": "片"}}]})
    )
    assert (delta, reasoning, done) == ("片", "", False)
    delta, reasoning, done, _ = parse_stream_data(
        "openai", json.dumps({"choices": [{"delta": {"reasoning_content": "想"}}]})
    )
    assert (delta, reasoning, done) == ("", "想", False)
    assert parse_stream_data("openai", "[DONE]") == ("", "", True, {})

    delta, reasoning, done, _ = parse_stream_data(
        "anthropic", json.dumps({"type": "content_block_delta", "delta": {"text": "片"}})
    )
    assert (delta, reasoning, done) == ("片", "", False)
    delta, reasoning, done, _ = parse_stream_data(
        "anthropic",
        json.dumps({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想"}}),
    )
    assert (delta, reasoning, done) == ("", "想", False)
    assert parse_stream_data("anthropic", json.dumps({"type": "message_stop"}))[2] is True

    delta, reasoning, done, _ = parse_stream_data(
        "gemini",
        json.dumps({"candidates": [{"content": {"parts": [{"text": "想", "thought": True}, {"text": "片"}]}}]}),
    )
    assert (delta, reasoning, done) == ("片", "想", False)


def test_chat_stream_forwards_reasoning_events(client, monkeypatch):
    sse_body = (
        b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x83\xb3"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"\xe7\xad\x94"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"}),
    )
    _save_config(client, [_openai_endpoint_body()])

    events = _sse_events(client.post("/api/chat", json={"messages": MESSAGES}).text)
    kinds = [event["type"] for event in events]
    assert "reasoning" in kinds and "delta" in kinds
    assert kinds.index("reasoning") < kinds.index("delta"), "思考事件先于回答事件"
    reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
    assert reasoning == "想"


def test_chat_non_stream_returns_reasoning(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": "答", "reasoning_content": "推理过程"}}],
                "usage": {},
            },
        )

    _install_transport(monkeypatch, handler)
    _save_config(client, [_openai_endpoint_body()])
    payload = client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).json()
    assert payload["reply"] == "答"
    assert payload["reasoning"] == "推理过程"


# ── 接口行为（MockTransport 注入） ─────────────────────────────────────────


def _save_config(client, endpoints: list[dict], **flags) -> dict:
    body = {"endpoints": endpoints, **flags}
    response = client.put("/api/account/llm-config", json=body)
    assert response.status_code == 200, response.text
    return response.json()["config"]


def _openai_endpoint_body(**overrides) -> dict:
    base = {
        "name": "主接口",
        "protocol": "openai",
        "base_url": "https://gateway.test/v1",
        "api_key": "sk-main",
        "model": "gpt-test",
    }
    base.update(overrides)
    return base


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


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in text.splitlines()
        if line.startswith("data:")
    ]


def test_chat_without_config_is_actionable_400(client):
    response = client.post("/api/chat", json={"messages": MESSAGES})
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "LLM_NOT_CONFIGURED"
    assert "自定义 API" in payload["message"]


def test_chat_non_stream_reply_with_meta(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system", "服务端注入系统提示词"
        assert body["messages"][-1]["content"] == "你好"
        return _openai_reply("你好，我是建模 Agent")

    _install_transport(monkeypatch, handler)
    _save_config(client, [_openai_endpoint_body()])

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reply"] == "你好，我是建模 Agent"
    assert payload["endpoint"] == "主接口"
    assert payload["host"] == "gateway.test"
    assert payload["third_party"] is True
    assert payload["fallback_used"] is False
    assert payload["usage"] == {"prompt_tokens": 7, "completion_tokens": 9}


def test_chat_stream_emits_meta_delta_done(client, monkeypatch):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    _install_transport(monkeypatch, handler)
    _save_config(client, [_openai_endpoint_body()])

    response = client.post("/api/chat", json={"messages": MESSAGES})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert events[0]["type"] == "meta" and events[0]["host"] == "gateway.test"
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert deltas == "你好"
    assert events[-1]["type"] == "done"


def test_chat_stream_flag_follows_saved_setting(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _openai_reply("OK"))
    _save_config(client, [_openai_endpoint_body()], stream=False)

    response = client.post("/api/chat", json={"messages": MESSAGES})
    assert response.headers["content-type"].startswith("application/json"), "关闭流式后走 JSON"
    assert response.json()["reply"] == "OK"


def test_chat_forwards_images_to_provider(client, monkeypatch):
    """视觉直通（ADR-0010）：请求里的 images 以多模态格式出现在出网载荷里。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _openai_reply("图里是一张流程图")

    _install_transport(monkeypatch, handler)
    _save_config(client, [_openai_endpoint_body()])
    response = client.post(
        "/api/chat",
        json={
            "messages": MESSAGES,
            "stream": False,
            "images": [{"media_type": "image/jpeg", "data": "aGVsbG8=", "name": "photo.jpg"}],
        },
    )
    assert response.status_code == 200, response.text
    content = seen["body"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "你好"}
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,aGVsbG8="


def test_chat_rejects_unsupported_or_malformed_images(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _openai_reply("不应被调用"))
    _save_config(client, [_openai_endpoint_body()])

    bad_type = client.post(
        "/api/chat",
        json={"messages": MESSAGES, "images": [{"media_type": "image/tiff", "data": "aGVsbG8="}]},
    )
    assert bad_type.status_code == 422

    data_url = client.post(
        "/api/chat",
        json={
            "messages": MESSAGES,
            "images": [{"media_type": "image/png", "data": "data:image/png;base64,aGVsbG8="}],
        },
    )
    assert data_url.status_code == 422, "data: URL 前缀必须被拒绝，只收纯 base64"


def test_chat_falls_back_on_429_and_reports_it(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return _openai_reply("备用接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(name="备用接口", base_url="https://backup.test/v1", api_key="sk-b"),
        ],
        fallback=True,
    )

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reply"] == "备用接口的回答"
    assert payload["endpoint"] == "备用接口"
    assert payload["fallback_used"] is True


def test_chat_402_falls_back_to_backup_endpoint(client, monkeypatch):
    """主接口余额不足（HTTP 402）→ 自动切到还有余额的备用接口。

    余额是接口各自独立的资产：主接口欠费不代表备用接口不可用。真实事故：
    DeepSeek 402 时链里的 GLM 余额充足，旧版却不回退、任务整个失败。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test":
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return _openai_reply("备用接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(name="备用接口", base_url="https://backup.test/v1", api_key="sk-b"),
        ],
        fallback=True,
    )

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reply"] == "备用接口的回答"
    assert payload["endpoint"] == "备用接口"
    assert payload["fallback_used"] is True


def test_chat_402_without_backup_surfaces_no_balance(client, monkeypatch):
    """只有一个接口时 402 如实上抛：独立错误码 + 余额不足人话文案。"""
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}}),
    )
    _save_config(client, [_openai_endpoint_body()])

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 402
    payload = response.json()
    assert payload["code"] == "LLM_NO_BALANCE"
    assert "余额不足" in payload["message"] and "402" in payload["message"]


def test_chat_fallback_disabled_surfaces_rate_limit(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: httpx.Response(429, json={}))
    _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(name="备用接口", base_url="https://backup.test/v1"),
        ],
        fallback=False,
    )

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 429
    assert response.json()["code"] == "LLM_RATE_LIMITED"


def test_chat_upstream_4xx_does_not_fall_back(client, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(401, json={"error": {"message": "bad api key"}})

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(name="备用接口", base_url="https://backup.test/v1"),
        ],
        fallback=True,
    )

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 502
    assert "bad api key" in response.json()["message"]
    assert calls == ["gateway.test"], "配置类错误不该换备用接口重试"


def test_chat_proxy_gate_blocks_third_party_host(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _openai_reply("不该到这"))
    _save_config(client, [_openai_endpoint_body()], allow_proxy=False)

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 403
    assert response.json()["code"] == "PROXY_DISABLED"


def test_chat_proxy_gate_allows_official_and_local_hosts(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _openai_reply("OK"))
    _save_config(
        client,
        [_openai_endpoint_body(base_url="http://127.0.0.1:11434/v1", protocol="ollama", api_key="")],
        allow_proxy=False,
    )
    assert client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).status_code == 200

    # 主流厂商官方域名（含带路径的接入点）同样不受第三方中转站开关影响
    for base_url in (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://api.x.ai/v1",
        "https://api.deepseek.com",
    ):
        response = client.post(
            "/api/llm/test",
            json={**_openai_endpoint_body(base_url=base_url), "allow_proxy": False},
        )
        assert response.status_code == 200, f"{base_url}: {response.text}"


def test_chat_requires_login(second_client):
    assert second_client.post("/api/chat", json={"messages": MESSAGES}).status_code == 401


# ── 测试连接 ────────────────────────────────────────────────────────────────


def test_llm_test_endpoint_reports_latency_and_reply(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _openai_reply("OK"))
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["reply"] == "OK"
    assert payload["host"] == "gateway.test"
    assert payload["third_party"] is True
    assert payload["latency_ms"] >= 0


def test_llm_test_endpoint_surfaces_upstream_error(client, monkeypatch):
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": {"message": "invalid key"}}),
    )
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 502
    assert "invalid key" in response.json()["message"]


# ── 网络层异常必须是可执行的错误信封，绝不允许 500 ─────────────────────────


def test_llm_test_timeout_maps_to_504_envelope(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_transport(monkeypatch, handler)
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 504, response.text
    payload = response.json()
    assert payload["code"] == "LLM_TIMEOUT"
    assert "未响应" in payload["message"]


def test_llm_test_connect_error_maps_to_502_envelope(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, handler)
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "LLM_UNREACHABLE"
    assert "gateway.test" in payload["message"]


def test_llm_test_redirect_is_actionable(client, monkeypatch):
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(301, headers={"Location": "https://www.gateway.test/v1"}),
    )
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "LLM_REDIRECTED"
    assert "Base URL" in payload["message"]


def test_llm_test_website_domain_suggests_api_base(client, monkeypatch):
    """把官网当 API 地址是最常见的配置错误，必须直接给出正确地址。"""
    _install_transport(monkeypatch, lambda request: _openai_reply("不该发出请求"))
    response = client.post(
        "/api/llm/test",
        json={**_openai_endpoint_body(base_url="https://www.deepseek.com"), "allow_proxy": True},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "LLM_WEBSITE_URL"
    assert "https://api.deepseek.com" in payload["message"]


def test_llm_test_non_json_body_is_actionable(client, monkeypatch):
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"<html>gateway landing page</html>"),
    )
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 502
    assert response.json()["code"] == "LLM_BAD_RESPONSE"


# ── 模型列表：让「默认模型 ID」跟上厂商上新 ─────────────────────────────────


def _models_reply(ids: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": model_id} for model_id in ids]})


def test_llm_models_lists_openai_ids_in_order(client, monkeypatch):
    """OpenAI 兼容家族走 /v1/models，返回顺序即补全顺序（通常新型号在前）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization", "")
        return _models_reply(["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.3"])

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/llm/models",
        json={
            **_openai_endpoint_body(base_url="https://open.bigmodel.cn/api/paas/v4"),
            "allow_proxy": True,
        },
    )
    assert response.status_code == 200, response.text
    assert seen["url"] == "https://open.bigmodel.cn/api/paas/v4/models"
    assert seen["auth"] == "Bearer sk-main"
    payload = response.json()
    assert payload["models"] == ["glm-5.3", "glm-5.3-flash", "glm-5.2"], "重复项要去掉且保持顺序"
    assert payload["host"] == "open.bigmodel.cn"
    assert payload["third_party"] is False


def test_llm_models_bare_domain_defaults_to_v1_path(client, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _models_reply(["gpt-5.6-sol"])

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/llm/models",
        json={**_openai_endpoint_body(base_url="https://gateway.test"), "allow_proxy": True},
    )
    assert response.status_code == 200, response.text
    assert seen["url"] == "https://gateway.test/v1/models"


def test_llm_models_ignores_chat_path_prefix(client, monkeypatch):
    """「路径前缀」是给对话补全用的，套到模型列表上只会 404。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _models_reply(["gpt-5.6-sol"])

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/llm/models",
        json={**_openai_endpoint_body(path_prefix="/api/v3/chat"), "allow_proxy": True},
    )
    assert response.status_code == 200, response.text
    assert seen["url"] == "https://gateway.test/v1/models"


def test_llm_models_anthropic_uses_key_header(client, monkeypatch):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key", "")
        seen["version"] = request.headers.get("anthropic-version", "")
        return _models_reply(["claude-fable-5", "claude-opus-5"])

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/llm/models",
        json={
            **_openai_endpoint_body(protocol="anthropic", base_url="https://api.anthropic.com"),
            "allow_proxy": True,
        },
    )
    assert response.status_code == 200, response.text
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["key"] == "sk-main" and seen["version"] == "2023-06-01"
    assert response.json()["models"] == ["claude-fable-5", "claude-opus-5"]


def test_llm_models_gemini_strips_models_prefix(client, monkeypatch):
    """Gemini 条目名带 models/ 前缀，填进模型 ID 输入框必须去掉。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"models": [{"name": "models/gemini-3.6-flash"}, {"name": "models/gemini-3.5-flash-lite"}]},
        )

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/llm/models",
        json={
            **_openai_endpoint_body(
                protocol="gemini", base_url="https://generativelanguage.googleapis.com"
            ),
            "allow_proxy": True,
        },
    )
    assert response.status_code == 200, response.text
    assert "/v1beta/models?pageSize=200&key=sk-main" in seen["url"]
    assert response.json()["models"] == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_llm_models_missing_endpoint_tells_user_to_type_it(client, monkeypatch):
    """自建网关常常没实现模型列表；提示要指向「手填」，别让人以为接口坏了。"""
    _install_transport(monkeypatch, lambda request: httpx.Response(404, text="not found"))
    response = client.post(
        "/api/llm/models",
        json={**_openai_endpoint_body(), "allow_proxy": True},
    )
    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "LLM_MODELS_UNSUPPORTED"
    assert "手填模型 ID" in payload["message"]


def test_llm_models_respects_proxy_gate(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _models_reply(["不该发出请求"]))
    response = client.post(
        "/api/llm/models",
        json={**_openai_endpoint_body(), "allow_proxy": False},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PROXY_DISABLED"


def test_llm_models_requires_login(second_client):
    assert second_client.post("/api/llm/models", json={}).status_code == 401


# ── Auto 模式：难度判定 + 权重路由 ──────────────────────────────────────────


def _judge_reply(difficulty: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "judge-mini",
            "choices": [
                {
                    "message": {"content": f'{{"difficulty": {difficulty}, "reason": "测试判定"}}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        },
    )


def _is_judge_request(request: httpx.Request) -> bool:
    body = json.loads(request.content)
    return "模型路由器" in body["messages"][-1]["content"]


def _weighted_pool() -> list[dict]:
    """两条接口：轻量（权重 2，主接口）+ 旗舰（权重 9）。"""
    return [
        _openai_endpoint_body(name="轻量接口", base_url="https://light.test/v1", model="mini-test", weight=2),
        _openai_endpoint_body(name="旗舰接口", base_url="https://strong.test/v1", model="max-test", weight=9),
    ]


def test_auto_route_hard_question_goes_to_strong_endpoint(client, monkeypatch):
    """判定为高难度时路由到权重最高的接口；判定请求由最轻量的接口承担。"""
    judge_hosts: list[str] = []
    judge_max_tokens: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            judge_hosts.append(request.url.host)
            judge_max_tokens.append(json.loads(request.content).get("max_tokens"))
            return _judge_reply(5)
        assert request.url.host == "strong.test", "高难度问题应路由到旗舰接口"
        return _openai_reply("旗舰模型的回答")

    _install_transport(monkeypatch, handler)
    _save_config(client, _weighted_pool())

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "请建立共享单车调度优化模型"}], "stream": False, "route": "auto"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reply"] == "旗舰模型的回答"
    assert payload["endpoint"] == "旗舰接口"
    assert payload["route"]["mode"] == "auto"
    assert payload["route"]["difficulty"] == 5
    assert payload["route"]["judge_model"] == "judge-mini"
    assert payload["route"]["judged"] is True
    assert judge_hosts == ["light.test"], "难度判定应使用最轻量的接口"
    assert judge_max_tokens == [512], "判定调用必须设 max_tokens，防推理型裁判烧 token"


def test_auto_route_short_message_skips_judge_entirely(client, monkeypatch):
    """极短且无建模关键词的消息不花判定调用：直接按难度 2 走轻量接口。"""
    judge_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            judge_calls.append(request.url.host)
            return _judge_reply(1)
        assert request.url.host == "light.test", "简单问题应留在轻量接口"
        return _openai_reply("轻量模型的回答")

    _install_transport(monkeypatch, handler)
    _save_config(client, _weighted_pool())

    payload = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "你好"}], "stream": False, "route": "auto"},
    ).json()
    assert payload["endpoint"] == "轻量接口"
    assert payload["route"]["difficulty"] == 2
    assert payload["route"]["judged"] is False
    assert judge_calls == [], "短消息不该花一次判定调用"


def test_auto_route_medium_question_picks_middle_tier(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            return _judge_reply(3)
        assert request.url.host == "mid.test", "中等难度应路由到中档接口"
        return _openai_reply("中档模型的回答")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            *_weighted_pool(),
            _openai_endpoint_body(name="中档接口", base_url="https://mid.test/v1", model="plus-test", weight=5),
        ],
    )

    payload = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "帮我求解一个线性规划问题并解释思路"}], "stream": False, "route": "auto"},
    ).json()
    assert payload["endpoint"] == "中档接口"


def test_auto_route_judge_failure_falls_back_to_heuristic(client, monkeypatch):
    """判定接口挂掉不能阻塞对话：退回规则估计（建模关键词 → 中档以上）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            return httpx.Response(500, json={"error": {"message": "judge down"}})
        return _openai_reply("仍然拿到了回答")

    _install_transport(monkeypatch, handler)
    _save_config(client, _weighted_pool())

    payload = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "请给出优化建模思路"}], "stream": False, "route": "auto"},
    ).json()
    assert payload["reply"] == "仍然拿到了回答"
    assert payload["route"]["judge_model"] == "", "规则估计时 judge_model 为空"
    assert payload["route"]["difficulty"] >= 3


def test_auto_route_stream_meta_carries_route_info(client, monkeypatch):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            return _judge_reply(5)
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    _install_transport(monkeypatch, handler)
    _save_config(client, _weighted_pool())

    events = _sse_events(
        client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "请为这个优化问题建模"}], "route": "auto"},
        ).text
    )
    meta = events[0]
    assert meta["type"] == "meta"
    assert meta["route"]["difficulty"] == 5
    assert meta["endpoint"] == "旗舰接口"


def test_auto_route_single_endpoint_skips_judge(client, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        return _openai_reply("唯一接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(client, [_openai_endpoint_body()])

    payload = client.post(
        "/api/chat", json={"messages": MESSAGES, "stream": False, "route": "auto"}
    ).json()
    assert payload["reply"] == "唯一接口的回答"
    assert payload["route"]["reason"] == "仅一个接口可用"
    assert len(calls) == 1, "只有一个接口时不该多花一次判定调用"


def test_auto_route_respects_proxy_gate(client, monkeypatch):
    """中转站开关关闭时，Auto 候选池只剩官方域名接口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.deepseek.com"
        return _openai_reply("官方接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(name="中转站", weight=9),
            _openai_endpoint_body(name="官方接口", base_url="https://api.deepseek.com", weight=2),
        ],
        allow_proxy=False,
    )

    payload = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "请建立复杂的优化模型"}], "stream": False, "route": "auto"},
    ).json()
    assert payload["endpoint"] == "官方接口"


def test_auto_route_followup_inherits_difficulty(client, monkeypatch):
    """短追问继承上一轮难度：不再花判定调用，且难度维持高档。"""
    judge_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            judge_calls.append(request.url.host)
            return _judge_reply(1)
        assert request.url.host == "strong.test", "继承难度 5 应继续用旗舰接口"
        return _openai_reply("继续深入的回答")

    _install_transport(monkeypatch, handler)
    config = _save_config(client, _weighted_pool())
    strong_id = config["endpoints"][1]["id"]

    payload = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "那继续第二问呢"}],
            "stream": False,
            "route": "auto",
            "route_state": {"difficulty": 5, "endpoint_id": strong_id, "turns": 1},
        },
    ).json()
    assert judge_calls == [], "短追问不该重新判定"
    assert payload["route"]["difficulty"] == 5
    assert payload["route"]["judged"] is False
    assert payload["endpoint"] == "旗舰接口"


def test_auto_route_sticky_keeps_last_endpoint_within_one_level(client, monkeypatch):
    """难度只差一档时沿用上一轮接口：换接口会让供应商 prompt cache 失效。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            return _judge_reply(3)
        assert request.url.host == "strong.test", "difficulty 4→3 应粘在上一轮的旗舰接口"
        return _openai_reply("粘性接口的回答")

    _install_transport(monkeypatch, handler)
    config = _save_config(client, _weighted_pool())
    strong_id = config["endpoints"][1]["id"]

    payload = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "再对这个规划模型的求解做一次灵敏度分析"}],
            "stream": False,
            "route": "auto",
            "route_state": {"difficulty": 4, "endpoint_id": strong_id, "turns": 2},
        },
    ).json()
    assert payload["route"]["difficulty"] == 3
    assert payload["route"]["judged"] is True
    assert payload["route"]["sticky"] is True
    assert payload["endpoint"] == "旗舰接口"


def test_auto_route_rejudges_after_turn_budget(client, monkeypatch):
    """继承轮数耗尽后必须重判一次（带上下文），防止话题漂移后难度失真。"""
    judge_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            judge_calls.append(json.loads(request.content)["messages"][-1]["content"])
            return _judge_reply(5)
        return _openai_reply("重判后的回答")

    _install_transport(monkeypatch, handler)
    config = _save_config(client, _weighted_pool())
    strong_id = config["endpoints"][1]["id"]

    payload = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "继续"}],
            "stream": False,
            "route": "auto",
            "route_state": {"difficulty": 5, "endpoint_id": strong_id, "turns": 5},
            "route_context": "上一轮正在推导多目标优化模型的帕累托前沿",
        },
    ).json()
    assert len(judge_calls) == 1, "轮数预算耗尽应强制重判"
    assert "对话背景" in judge_calls[0], "重判提示词应带上微上下文"
    assert payload["route"]["judged"] is True
    assert payload["route"]["difficulty"] == 5


def test_auto_route_prefers_route_question_over_message_blocks(client, monkeypatch):
    """判定输入优先用 route_question：消息里的注入块不该参与难度判定。"""
    judge_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_judge_request(request):
            judge_calls.append(request.url.host)
            return _judge_reply(5)
        assert request.url.host == "light.test"
        return _openai_reply("轻量接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(client, _weighted_pool())

    content = "【当前建模任务】请建立复杂的优化仿真模型\n\n【回答方式】深度研究模式\n\n你好"
    payload = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "route": "auto",
            "route_question": "你好",
        },
    ).json()
    assert judge_calls == [], "route_question 是短消息时不该因注入块触发判定"
    assert payload["route"]["difficulty"] == 2
    assert payload["endpoint"] == "轻量接口"


def test_chat_with_endpoint_id_uses_that_endpoint(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "strong.test"
        return _openai_reply("指定接口的回答")

    _install_transport(monkeypatch, handler)
    config = _save_config(client, _weighted_pool())
    strong_id = config["endpoints"][1]["id"]

    payload = client.post(
        "/api/chat",
        json={"messages": MESSAGES, "stream": False, "endpoint_id": strong_id},
    ).json()
    assert payload["endpoint"] == "旗舰接口"

    missing = client.post(
        "/api/chat",
        json={"messages": MESSAGES, "stream": False, "endpoint_id": "ep_missing"},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "LLM_ENDPOINT_NOT_FOUND"


def test_chat_falls_back_on_timeout(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test":
            raise httpx.ReadTimeout("slow upstream", request=request)
        return _openai_reply("备用接口的回答")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(name="备用接口", base_url="https://backup.test/v1"),
        ],
        fallback=True,
    )

    response = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert response.status_code == 200, response.text
    assert response.json()["endpoint"] == "备用接口"


# ── Auto 路由纯函数：规则估计、判定解析与强度映射 ───────────────────────────


def test_heuristic_difficulty_recognizes_english_keywords():
    """COMAP 等英文题面必须能命中难度信号，不再被单一字符阈值低估。"""
    assert llm_module.heuristic_difficulty("hello there") == 2
    assert llm_module.heuristic_difficulty(
        "Build an optimization model to forecast demand under constraints"
    ) == 5, "三个及以上英文关键词应判到最高档"
    assert llm_module.heuristic_difficulty("Please prove this inequality") == 3
    long_english = "word " * 300
    assert llm_module.heuristic_difficulty(long_english) == 5, "长英文文本按词数分档"


def test_difficulty_parse_only_accepts_standalone_digits():
    parse = llm_module._difficulty_from_text
    assert parse('{"difficulty": 4, "reason": "多步推理"}') == 4
    assert parse("难度是 3") == 3
    assert parse("答案是 3.14，无难度数字") is None, "小数不算难度"
    assert parse("编号 12345 没有独立数字") is None
    assert parse("在 1-5 的量表上打分") is None, "裁判回显量表不算难度"
    assert parse("完全没有数字") is None


def test_pick_by_difficulty_uses_absolute_strength_targets():
    """映射与池构成解耦：全强池的简单题取相对最弱，弱多池的中档题不落到最弱。"""
    light = _endpoint(id="ep_l", weight=2)
    mid = _endpoint(id="ep_m", weight=5)
    strong = _endpoint(id="ep_s", weight=9)
    pick = llm_module.pick_by_difficulty
    assert pick([light, mid, strong], 1) is light
    assert pick([light, mid, strong], 3) is mid
    assert pick([light, mid, strong], 5) is strong
    # 池里只有两个强模型：难度 1 也只能取相对较弱的那个
    strong_a = _endpoint(id="ep_a", weight=8)
    strong_b = _endpoint(id="ep_b", weight=10)
    assert pick([strong_b, strong_a], 1) is strong_a
    # 同距时中难度偏强（答不好引发的重问比差价更贵）
    four = _endpoint(id="ep_4", weight=4)
    six = _endpoint(id="ep_6", weight=6)
    assert pick([four, six], 3) is six


# ── 系统代理绕过：本机接口必须直连 ─────────────────────────────────────────


class _LocalUpstream(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # 保持测试输出干净
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        body = json.dumps({"choices": [{"message": {"content": "本机接口应答"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def local_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()


def _point_system_proxy_at_a_dead_port(monkeypatch) -> None:
    """把系统代理指向一个没人监听的端口：请求只要真走了代理就必然连不上，
    「有没有绕过代理」因此成为可断言的事实，而不是靠读 httpx 私有属性猜。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_bypasses_http_proxy_covers_loopback_private_and_link_local():
    bypass = llm_module.bypasses_http_proxy
    for host in (
        "localhost", "ollama.localhost", "nas.local",
        "127.0.0.1", "127.1.2.3", "::1",
        "10.0.0.5", "172.16.0.9", "172.31.255.254", "192.168.2.70",
        "169.254.1.1", "fe80::1", "fd00::1",
    ):
        assert bypass(host), f"{host} 是本机/私网地址，必须直连"
    for host in ("", "api.openai.com", "api.deepseek.com", "gateway.test", "8.8.8.8", "172.32.0.1"):
        assert not bypass(host), f"{host} 不是本机地址，不该被摘出代理"


def test_local_endpoint_bypasses_system_proxy(local_upstream, monkeypatch):
    """本机接口（Ollama / vLLM / 自建网关）必须直连，不能被塞进系统代理。

    httpx 的 trust_env 只认 NO_PROXY 环境变量，不读 Windows 注册表的
    ProxyOverride：即便系统自己的 proxy_bypass('127.0.0.1') 返回 True，
    httpx 照样走代理，用户拿到的是代理返回的「接口 X 返回 HTTP 502」——
    与接口本身毫无关系，排查会一路查错方向（真实事故）。
    """
    _point_system_proxy_at_a_dead_port(monkeypatch)

    with llm_module._client(5.0, local_upstream) as client:
        response = client.post(local_upstream, json={"model": "m"})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "本机接口应答"


def test_public_endpoint_still_goes_through_system_proxy(monkeypatch):
    """反向对照：官方厂商域名照旧走系统代理——境内访问多半依赖它，
    绕过规则必须只摘本机地址，不能顺手把所有出网都改成直连。"""
    _point_system_proxy_at_a_dead_port(monkeypatch)
    url = "https://api.deepseek.com/v1/chat/completions"

    with llm_module._client(5.0, url) as client, pytest.raises(httpx.TransportError):
        client.post(url, json={"model": "m"})
