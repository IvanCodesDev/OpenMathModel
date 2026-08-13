"""设置中心「自定义 API」：接口配置的读写、校验与归属。"""

from __future__ import annotations


def _endpoint(**overrides) -> dict:
    base = {
        "name": "测试网关",
        "protocol": "openai",
        "base_url": "https://gateway.test/v1",
        "api_key": "sk-test",
        "model": "gpt-test",
    }
    base.update(overrides)
    return base


def _config(client) -> dict:
    response = client.get("/api/account/llm-config")
    assert response.status_code == 200, response.text
    return response.json()["config"]


def test_llm_config_defaults_when_never_saved(client):
    config = _config(client)
    assert config["endpoints"] == []
    assert config["active_endpoint_id"] is None
    assert config["allow_proxy"] is True
    assert config["stream"] is True
    assert config["fallback"] is True


def test_llm_config_roundtrip_assigns_ids_and_active(client):
    saved = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": [_endpoint(), _endpoint(name="备用接口", base_url="https://backup.test/v1")],
            "allow_proxy": False,
            "stream": False,
            "fallback": False,
        },
    )
    assert saved.status_code == 200, saved.text
    config = saved.json()["config"]

    ids = [endpoint["id"] for endpoint in config["endpoints"]]
    assert all(ids), "缺 id 的接口应自动生成"
    assert config["active_endpoint_id"] == ids[0], "未指定主接口时默认第一个"
    assert config["allow_proxy"] is False
    assert config["stream"] is False
    assert config["fallback"] is False
    # 密钥保存后本人可读（本机后端即“本机保存”）
    assert config["endpoints"][0]["api_key"] == "sk-test"
    assert _config(client) == config

    # 指定另一个为主接口
    updated = client.put(
        "/api/account/llm-config",
        json={"endpoints": config["endpoints"], "active_endpoint_id": ids[1]},
    )
    assert updated.json()["config"]["active_endpoint_id"] == ids[1]

    # 未知 active id 回落到第一个
    fallback = client.put(
        "/api/account/llm-config",
        json={"endpoints": config["endpoints"], "active_endpoint_id": "ep_missing"},
    )
    assert fallback.json()["config"]["active_endpoint_id"] == ids[0]


def test_llm_config_weight_roundtrip_and_default(client):
    """模型能力权重随接口保存；未设置时回读为 0（= 按模型名自动推断）。"""
    saved = client.put(
        "/api/account/llm-config",
        json={"endpoints": [_endpoint(weight=8), _endpoint(name="轻量接口", base_url="https://light.test/v1")]},
    )
    assert saved.status_code == 200, saved.text
    endpoints = saved.json()["config"]["endpoints"]
    assert endpoints[0]["weight"] == 8
    assert endpoints[1]["weight"] == 0
    assert _config(client)["endpoints"][0]["weight"] == 8

    out_of_range = client.put(
        "/api/account/llm-config", json={"endpoints": [_endpoint(weight=11)]}
    )
    assert out_of_range.status_code == 422


def test_llm_config_validation_rejects_bad_values(client):
    bad_protocol = client.put(
        "/api/account/llm-config", json={"endpoints": [_endpoint(protocol="soap")]}
    )
    assert bad_protocol.status_code == 422

    bad_url = client.put(
        "/api/account/llm-config", json={"endpoints": [_endpoint(base_url="gateway.test")]}
    )
    assert bad_url.status_code == 422

    bad_prefix = client.put(
        "/api/account/llm-config", json={"endpoints": [_endpoint(path_prefix="chat")]}
    )
    assert bad_prefix.status_code == 422

    # 非法值不落库
    assert _config(client)["endpoints"] == []


def test_llm_config_requires_login(second_client):
    assert second_client.get("/api/account/llm-config").status_code == 401
    assert (
        second_client.put("/api/account/llm-config", json={"endpoints": []}).status_code == 401
    )
