"""设置中心「智能路由」按任务类型定向接口（ADR-0015）。

四层各取一刀：配置解析与规范化（脏 id 丢弃、开关关闭）、llm-config 读写契约、
任务引擎端口按 prompt → 任务类型选链、对话携图时的 vision 定向。
出网全部 MockTransport / 打桩，不真实联网。
"""

from __future__ import annotations

import json

import httpx
import pytest
from omm_api import llm as llm_module
from omm_api.engine_glue import _PROMPT_NODE_IDS, _PROMPT_TASK_KINDS
from omm_api.llm import (
    TASK_ROUTE_KINDS,
    ChatOutcome,
    EngineLlmPort,
    LlmConfig,
    LlmEndpoint,
    normalize_task_routes,
    parse_llm_config,
)

MESSAGES = [{"role": "user", "content": "你好"}]


def _endpoint(id: str, host: str, model: str = "gpt-test") -> LlmEndpoint:
    return LlmEndpoint(
        id=id, name=f"接口 {id}", protocol="openai", base_url=f"https://{host}/v1", model=model
    )


MAIN = _endpoint("ep_main", "main.test")
CODER = _endpoint("ep_coder", "coder.test", model="coder-test")
WRITER = _endpoint("ep_writer", "writer.test", model="writer-test")
VISION = _endpoint("ep_vision", "vision.test", model="vision-test")


# ── 配置解析与规范化 ─────────────────────────────────────────────────────────


def test_normalize_task_routes_drops_unknown_kinds_and_stale_ids():
    routes = normalize_task_routes(
        {"coding": "ep_coder", "writing": "ep_gone", "vision": 42, "poetry": "ep_coder"},
        ["ep_main", "ep_coder"],
    )
    assert routes == {"coding": "ep_coder", "research": None, "writing": None, "vision": None}
    assert tuple(routes) == TASK_ROUTE_KINDS, "四个键永远齐全、顺序固定"
    assert normalize_task_routes("garbage", ["ep_main"]) == {kind: None for kind in TASK_ROUTE_KINDS}


def test_parse_llm_config_reads_routes_and_switch():
    raw = {
        "endpoints": [
            {"id": "ep_main", "name": "主", "base_url": "https://main.test/v1", "model": "m"},
            {"id": "ep_coder", "name": "码", "base_url": "https://coder.test/v1", "model": "c"},
        ],
        "active_endpoint_id": "ep_main",
        "task_routes": {"coding": "ep_coder", "writing": "ep_deleted"},
    }
    config = parse_llm_config(raw)
    assert config.smart_routing is True, "缺省开启"
    assert config.task_routes == (("coding", "ep_coder"),), "只保留指向现存接口的项"
    assert config.route_for("coding").id == "ep_coder"
    assert config.route_for("writing") is None
    assert config.route_for(None) is None

    off = parse_llm_config({**raw, "smart_routing": False})
    assert off.task_routes == (("coding", "ep_coder"),), "关闭开关不丢配置"
    assert off.route_for("coding") is None, "关闭后定向不生效"

    legacy = parse_llm_config({"endpoints": raw["endpoints"]})
    assert legacy.task_routes == () and legacy.smart_routing is True, "历史数据无需迁移"


def test_chain_for_pins_first_and_keeps_fallbacks():
    config = LlmConfig(
        endpoints=(MAIN, CODER, WRITER),
        active_endpoint_id=MAIN.id,
        task_routes=(("coding", CODER.id),),
    )
    assert [e.id for e in config.chain_for("coding")] == ["ep_coder", "ep_main", "ep_writer"]
    assert [e.id for e in config.chain_for("writing")] == ["ep_main", "ep_coder", "ep_writer"], "未定向走主接口链"
    assert [e.id for e in config.chain_for(None)] == ["ep_main", "ep_coder", "ep_writer"]

    no_fallback = LlmConfig(
        endpoints=(MAIN, CODER),
        active_endpoint_id=MAIN.id,
        fallback=False,
        task_routes=(("coding", CODER.id),),
    )
    assert [e.id for e in no_fallback.chain_for("coding")] == ["ep_coder"], "回退开关关闭时只打定向接口"


def test_prompt_task_kinds_cover_every_engine_prompt():
    """每个会打模型的提示词都要归类；漏归类的按主接口链处理是安全方向，但
    产品口径是「六个阶段都能定向」，所以这里守住全覆盖。"""
    assert set(_PROMPT_TASK_KINDS) == set(_PROMPT_NODE_IDS)
    assert set(_PROMPT_TASK_KINDS.values()) <= set(TASK_ROUTE_KINDS)
    # 数据准备阶段拆开：方案是研究，沙盒写码是编程
    assert _PROMPT_TASK_KINDS["data_preparation.default"] == "research"
    assert _PROMPT_TASK_KINDS["data_cleaning.sandbox"] == "coding"
    assert _PROMPT_TASK_KINDS["paper_section.default"] == "writing"
    assert _PROMPT_TASK_KINDS["experiment_code.sandbox"] == "coding"


# ── llm-config 契约 ──────────────────────────────────────────────────────────


def _body(**overrides) -> dict:
    base = {
        "name": "主接口",
        "protocol": "openai",
        "base_url": "https://main.test/v1",
        "api_key": "sk-main",
        "model": "gpt-test",
    }
    base.update(overrides)
    return base


def test_llm_config_task_routes_roundtrip_and_stale_cleanup(client):
    saved = client.put(
        "/api/account/llm-config",
        json={"endpoints": [_body(), _body(name="编程接口", base_url="https://coder.test/v1")]},
    )
    assert saved.status_code == 200, saved.text
    config = saved.json()["config"]
    assert config["smart_routing"] is True
    assert config["task_routes"] == {kind: None for kind in TASK_ROUTE_KINDS}, "缺省全自动、四键齐全"
    main_id, coder_id = (e["id"] for e in config["endpoints"])

    routed = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": config["endpoints"],
            "active_endpoint_id": main_id,
            "smart_routing": False,
            "task_routes": {"coding": coder_id, "writing": "ep_missing"},
        },
    ).json()["config"]
    assert routed["smart_routing"] is False
    assert routed["task_routes"] == {"coding": coder_id, "research": None, "writing": None, "vision": None}
    assert client.get("/api/account/llm-config").json()["config"] == routed

    # 删掉被定向的接口：定向自动回落 None，前端不需要再补一次保存
    dropped = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": [e for e in config["endpoints"] if e["id"] == main_id],
            "active_endpoint_id": main_id,
            "task_routes": {"coding": coder_id},
        },
    ).json()["config"]
    assert dropped["task_routes"]["coding"] is None


# ── 任务引擎端口：按 prompt → 任务类型选链 ────────────────────────────────────


class _Template:
    def render(self, variables: dict) -> str:
        return "渲染后的提示词"


class _Registry:
    def get(self, prompt_id: str) -> _Template:
        return _Template()


def _outcome(endpoint: LlmEndpoint) -> ChatOutcome:
    return ChatOutcome(
        text='{"ok": true}',
        model=endpoint.model,
        endpoint=endpoint,
        usage={"prompt_tokens": 10, "completion_tokens": 20},
        elapsed_ms=50,
    )


def _capture_chains(monkeypatch) -> list[list[str]]:
    chains: list[list[str]] = []

    def fake_stream(config, messages, chain=None, on_delta=None, **kwargs):
        chains.append([e.id for e in (chain if chain is not None else config.chain())])
        return _outcome(chain[0] if chain else config.active())

    monkeypatch.setattr(llm_module, "stream_complete_with_fallback", fake_stream)
    return chains


def test_engine_port_routes_prompts_by_task_kind(monkeypatch):
    config = LlmConfig(
        endpoints=(MAIN, CODER, WRITER),
        active_endpoint_id=MAIN.id,
        stream=True,
        task_routes=(("coding", CODER.id), ("writing", WRITER.id)),
    )
    chains = _capture_chains(monkeypatch)
    events: list[dict] = []
    port = EngineLlmPort(
        config, _Registry(), on_event=events.append, task_kind_for_prompt=_PROMPT_TASK_KINDS
    )

    port.complete("experiment_code.default", {})
    port.complete("paper_section.default", {})
    port.complete("problem_analysis.default", {})
    port.chat_text([{"role": "user", "content": "跑一下"}], label="data_cleaning.sandbox")
    port.complete("unknown.prompt", {})

    assert chains == [
        ["ep_coder", "ep_main", "ep_writer"],  # 实验代码 → 编程接口在前
        ["ep_writer", "ep_main", "ep_coder"],  # 论文章节 → 写作接口在前
        ["ep_main", "ep_coder", "ep_writer"],  # 题意解析未定向 → 主接口链
        ["ep_coder", "ep_main", "ep_writer"],  # 沙盒写码会话同样按编程定向
        ["ep_main", "ep_coder", "ep_writer"],  # 表外提示词 → 主接口链
    ]
    calls = [e for e in events if e["kind"] == "llm_call"]
    assert [(c.get("task_kind"), c.get("pinned")) for c in calls] == [
        ("coding", True),
        ("writing", True),
        ("research", False),
        ("coding", True),
        (None, None),
    ], "过程事件如实标注任务类型与是否命中定向"


def test_engine_port_ignores_routes_when_switch_off(monkeypatch):
    config = LlmConfig(
        endpoints=(MAIN, CODER),
        active_endpoint_id=MAIN.id,
        stream=True,
        smart_routing=False,
        task_routes=(("coding", CODER.id),),
    )
    chains = _capture_chains(monkeypatch)
    EngineLlmPort(config, _Registry(), task_kind_for_prompt=_PROMPT_TASK_KINDS).complete(
        "experiment_code.default", {}
    )
    assert chains == [["ep_main", "ep_coder"]]


# ── 对话：携图时的 vision 定向 ────────────────────────────────────────────────


def _install_transport(monkeypatch, handler) -> None:
    monkeypatch.setattr(llm_module, "_transport_factory", lambda: httpx.MockTransport(handler))


def _reply(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": "看到了"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        },
    )


@pytest.fixture
def vision_config(client) -> dict:
    saved = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": [
                _body(),
                _body(name="视觉接口", base_url="https://vision.test/v1", model="vision-test"),
            ]
        },
    ).json()["config"]
    main_id, vision_id = (e["id"] for e in saved["endpoints"])
    routed = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": saved["endpoints"],
            "active_endpoint_id": main_id,
            "task_routes": {"vision": vision_id},
        },
    )
    assert routed.status_code == 200, routed.text
    return {"main": main_id, "vision": vision_id}


IMAGE = {"media_type": "image/jpeg", "data": "aGVsbG8=", "name": "photo.jpg"}


def test_chat_with_images_uses_vision_route_in_auto_mode(client, monkeypatch, vision_config):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return _reply("vision-test")

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/chat",
        json={"messages": MESSAGES, "stream": False, "route": "auto", "images": [IMAGE]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert hosts == ["vision.test"], "携图 + Auto → 直接打视觉接口，不经难度判定"
    assert payload["host"] == "vision.test"
    assert payload["route"] == {"mode": "task", "kind": "vision", "endpoint_id": vision_config["vision"]}

    # 旧客户端不带任何路由参数：携图同样走视觉定向
    hosts.clear()
    legacy = client.post("/api/chat", json={"messages": MESSAGES, "stream": False, "images": [IMAGE]})
    assert legacy.status_code == 200 and hosts == ["vision.test"]

    # 不携图：定向不介入，走主接口
    hosts.clear()
    plain = client.post("/api/chat", json={"messages": MESSAGES, "stream": False})
    assert plain.status_code == 200 and hosts == ["main.test"]


def test_chat_with_images_respects_explicit_endpoint(client, monkeypatch, vision_config):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return _reply("gpt-test")

    _install_transport(monkeypatch, handler)
    response = client.post(
        "/api/chat",
        json={
            "messages": MESSAGES,
            "stream": False,
            "endpoint_id": vision_config["main"],
            "images": [IMAGE],
        },
    )
    assert response.status_code == 200, response.text
    assert hosts == ["main.test"], "用户显式钉住的接口优先于视觉定向"
    assert response.json()["route"] is None


def test_chat_stream_meta_carries_task_route(client, monkeypatch, vision_config):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"\xe5\x9b\xbe"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "vision.test"
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    _install_transport(monkeypatch, handler)
    response = client.post("/api/chat", json={"messages": MESSAGES, "route": "auto", "images": [IMAGE]})
    assert response.status_code == 200, response.text
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert events[0]["type"] == "meta"
    assert events[0]["route"]["mode"] == "task" and events[0]["route"]["kind"] == "vision"
    assert events[-1]["type"] == "done"
