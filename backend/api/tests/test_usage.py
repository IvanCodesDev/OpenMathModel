"""设置中心「用量监控」：调用记录、月度汇总、预算设置与硬限制闸门。

上游一律用 httpx.MockTransport 模拟（经 omm_api.llm._transport_factory 注入），
与 test_llm_chat 的注入方式一致；用量行为全部经公开 API 断言。
"""

from __future__ import annotations

import json

import httpx

from omm_api import llm as llm_module

MESSAGES = [{"role": "user", "content": "你好"}]


def _install_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm_module, "_transport_factory", lambda: transport)


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


def _reply(model: str = "gpt-test", prompt_tokens: int = 7, completion_tokens: int = 9) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def _summary(client, month: str | None = None) -> dict:
    url = "/api/usage/summary" + (f"?month={month}" if month else "")
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


# ── 汇总空态与鉴权 ──────────────────────────────────────────────────────────


def test_usage_summary_empty_state(client):
    payload = _summary(client)
    assert payload["totals"] == {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_cny": 0.0,
    }
    assert len(payload["daily"]) == 14
    assert payload["models"] == []
    assert payload["agent_runs"] == {"total": 0, "llm": 0}
    budget = payload["budget"]
    assert budget["monthly_budget_cny"] is None
    assert budget["hard_limit"] is False
    assert budget["alert"] is False


def test_usage_requires_login(second_client):
    assert second_client.get("/api/usage/summary").status_code == 401
    assert second_client.get("/api/usage/export").status_code == 401
    assert second_client.get("/api/usage/settings").status_code == 401


def test_usage_month_param_validation(client):
    response = client.get("/api/usage/summary?month=2026-13")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# ── 调用记录：对话（非流式/流式）与测试连接 ────────────────────────────────


def test_non_stream_chat_records_usage(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _reply())
    _save_config(client, [_openai_endpoint_body()])

    assert client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).status_code == 200

    payload = _summary(client)
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["prompt_tokens"] == 7
    assert payload["totals"]["completion_tokens"] == 9
    assert payload["totals"]["total_tokens"] == 16
    assert payload["models"][0]["model"] == "gpt-test"
    assert payload["models"][0]["requests"] == 1
    # 14 天序列的最后一天就是今天，本次调用落在这一格
    assert payload["daily"][-1]["total_tokens"] == 16


def test_stream_chat_records_usage(client, monkeypatch):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
        b'data: {"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"}),
    )
    _save_config(client, [_openai_endpoint_body()])

    response = client.post("/api/chat", json={"messages": MESSAGES})
    assert response.status_code == 200
    assert "done" in response.text

    payload = _summary(client)
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["total_tokens"] == 8


def test_llm_test_endpoint_records_usage(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _reply())
    response = client.post("/api/llm/test", json={**_openai_endpoint_body(), "allow_proxy": True})
    assert response.status_code == 200

    payload = _summary(client)
    assert payload["totals"]["requests"] == 1

    export = client.get("/api/usage/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "openmathmodel-usage-" in export.headers["content-disposition"]
    assert "连接测试" in export.text
    assert "gpt-test" in export.text


def test_auto_route_judge_call_is_recorded(client, monkeypatch):
    """Auto 模式的难度判定也是一次真实调用，必须计入用量。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "模型路由器" in body["messages"][-1]["content"]:
            return httpx.Response(
                200,
                json={
                    "model": "judge-mini",
                    "choices": [{"message": {"content": '{"difficulty": 5, "reason": "测试"}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 2},
                },
            )
        return _reply(model="max-test")

    _install_transport(monkeypatch, handler)
    _save_config(
        client,
        [
            _openai_endpoint_body(name="轻量", base_url="https://light.test/v1", model="mini-test", weight=2),
            _openai_endpoint_body(name="旗舰", base_url="https://strong.test/v1", model="max-test", weight=9),
        ],
    )

    response = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "请建立优化模型"}], "stream": False, "route": "auto"},
    )
    assert response.status_code == 200, response.text

    payload = _summary(client)
    assert payload["totals"]["requests"] == 2, "难度判定 + 正式回答各记一条"
    models = {row["model"] for row in payload["models"]}
    assert {"judge-mini", "max-test"} <= models


# ── 预算设置与硬限制闸门 ────────────────────────────────────────────────────


def test_usage_settings_roundtrip(client):
    defaults = client.get("/api/usage/settings").json()["settings"]
    assert defaults == {
        "monthly_budget_cny": None,
        "budget_threshold_percent": 80,
        "hard_limit": False,
    }

    updated = client.put(
        "/api/usage/settings",
        json={"monthly_budget_cny": 200, "budget_threshold_percent": 60, "hard_limit": True},
    )
    assert updated.status_code == 200
    assert updated.json()["settings"] == {
        "monthly_budget_cny": 200.0,
        "budget_threshold_percent": 60,
        "hard_limit": True,
    }
    assert client.get("/api/usage/settings").json()["settings"]["hard_limit"] is True


def test_budget_threshold_alert_in_summary(client, monkeypatch):
    # 一次 500 万输入 token 的 gpt-4o 调用 ≈ 90 元
    _install_transport(monkeypatch, lambda request: _reply(model="gpt-4o", prompt_tokens=5_000_000, completion_tokens=0))
    _save_config(client, [_openai_endpoint_body()])
    client.put(
        "/api/usage/settings",
        json={"monthly_budget_cny": 100, "budget_threshold_percent": 80, "hard_limit": False},
    )
    assert client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).status_code == 200

    budget = _summary(client)["budget"]
    assert budget["used_cny"] >= 80
    assert budget["used_percent"] >= 80
    assert budget["alert"] is True
    assert budget["remaining_cny"] is not None


def test_budget_hard_limit_blocks_paid_endpoint(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _reply(model="gpt-4o", prompt_tokens=5_000_000, completion_tokens=0))
    _save_config(client, [_openai_endpoint_body()])
    client.put(
        "/api/usage/settings",
        json={"monthly_budget_cny": 50, "budget_threshold_percent": 80, "hard_limit": True},
    )
    # 第一次调用发生在预算内（0 < 50），花掉 ≈90 元
    assert client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).status_code == 200

    blocked = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "BUDGET_EXCEEDED"


def test_budget_hard_limit_keeps_local_endpoint_usable(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gateway.test":
            return _reply(model="gpt-4o", prompt_tokens=5_000_000, completion_tokens=0)
        return _reply(model="llama3.2", prompt_tokens=4, completion_tokens=6)

    _install_transport(monkeypatch, handler)
    config = _save_config(
        client,
        [
            _openai_endpoint_body(),
            _openai_endpoint_body(
                name="本地 Ollama",
                protocol="ollama",
                base_url="http://127.0.0.1:11434/v1",
                api_key="",
                model="llama3.2",
            ),
        ],
    )
    paid_id = config["endpoints"][0]["id"]
    client.put(
        "/api/usage/settings",
        json={"monthly_budget_cny": 50, "budget_threshold_percent": 80, "hard_limit": True},
    )
    assert client.post("/api/chat", json={"messages": MESSAGES, "stream": False}).status_code == 200

    # 超预算后：默认链回落到本地接口，仍可对话
    fallback = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["host"] == "127.0.0.1"

    # 点名付费接口则明确拒绝，并说明原因
    blocked = client.post(
        "/api/chat",
        json={"messages": MESSAGES, "stream": False, "endpoint_id": paid_id},
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "BUDGET_EXCEEDED"
    assert "用量监控" in blocked.json()["message"]
