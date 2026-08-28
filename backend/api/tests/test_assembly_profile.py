"""装配档位开关（§4.9）：OMM_AGENT_NODES=sim|real|mixed。

三个档位的行为边界：sim 无视用户配置强制整链模拟；real 禁止静默回落模拟
（真实链路不可用 = 干净的运行失败，带可行动信息）；mixed（含缺省与非法值）
= 现状的按运行归属自动装配。mixed 的完整行为由既有生命周期/e2e 套件覆盖，
这里只锚定开关本身的三条边界。
"""

from __future__ import annotations

from conftest import API, create_project, create_run, get_run, run_status_is, wait_until


def _save_llm_config(client) -> None:
    """配置一个形状合法的自定义 API（sim 档位下绝不会被调用，不出网）。"""
    response = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": [
                {
                    "name": "测试网关",
                    "protocol": "openai",
                    "base_url": "https://gateway.test/v1",
                    "api_key": "sk-test",
                    "model": "gpt-test",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


def _first_step_outputs(client, run_id: str) -> dict:
    events = client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]
    for event in events:
        if event["type"] == "step.succeeded":
            return event["payload"].get("outputs") or {}
    raise AssertionError("no step.succeeded event found")


def test_sim_forces_simulated_nodes_despite_llm_config(client, monkeypatch) -> None:
    monkeypatch.setenv("OMM_AGENT_NODES", "sim")
    _save_llm_config(client)  # 配置在场也必须被忽略
    project = create_project(client)
    run = create_run(client, project["id"])

    client.app.state.advancer.advance(run["id"])
    client.app.state.advancer.advance(run["id"])

    outputs = _first_step_outputs(client, run["id"])
    # SimStageNode 的输出只有 label（模拟标注）；真实节点会产出 title 等契约键。
    assert "label" in outputs and "title" not in outputs
    events = client.get(f"{API}/task-runs/{run['id']}/events/history").json()["items"]
    assert not [e for e in events if e["payload"].get("kind") == "llm_call_started"]


def test_real_without_usable_config_fails_cleanly(client, monkeypatch) -> None:
    monkeypatch.setenv("OMM_AGENT_NODES", "real")
    # 用户未配置任何自定义 API → real 档位必须失败而非静默模拟
    project = create_project(client)
    run = create_run(client, project["id"])

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))
    assert "OMM_AGENT_NODES=real" in (failed["failure"]["message"] or "")

    # 失败走标准路径：步骤失败可见、retry 动作可用（干净失败而非卡死）
    steps = client.get(f"{API}/task-runs/{run['id']}/steps").json()["items"]
    assert steps and steps[0]["status"] == "FAILED"


def test_invalid_mode_value_behaves_as_mixed(client, monkeypatch) -> None:
    monkeypatch.setenv("OMM_AGENT_NODES", "bogus")
    # 无用户配置的 mixed = 模拟链正常推进（与缺省行为一致，不失败）
    project = create_project(client)
    run = create_run(client, project["id"])

    client.app.state.advancer.advance(run["id"])
    client.app.state.advancer.advance(run["id"])

    payload = get_run(client, run["id"])
    assert payload["status"] == "RUNNING"
    outputs = _first_step_outputs(client, run["id"])
    assert "label" in outputs
