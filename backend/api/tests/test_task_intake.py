"""发送前接待判定（POST /v1/task-intake）：对话优先门控。

不变量：
- 未配置自定义 API 的用户永远放行（演示/模拟链的「发送即建任务」不变）；
- 启发式短路（带附件 / 长题面 / 极短输入）不出网；
- 模型判定的意图与回应透传；判定失败/输出无法解析一律放行——门控挂了
  绝不能挡住真实用户；
- 端点要求登录。
"""

from __future__ import annotations

import json

import httpx

from omm_api import llm as llm_module

API = "/api/v1/task-intake"


def _configure_llm(client, monkeypatch, handler) -> None:
    monkeypatch.setattr(llm_module, "_transport_factory", lambda: httpx.MockTransport(handler))
    saved = client.put(
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
    assert saved.status_code == 200, saved.text


def _judge_reply(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-test",
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        },
    )


def _intake(client, goal: str, has_attachments: bool = False) -> dict:
    response = client.post(API, json={"goal": goal, "has_attachments": has_attachments})
    assert response.status_code == 200, response.text
    return response.json()


def test_unconfigured_user_always_passes_through(client):
    result = _intake(client, "你好")
    assert result["intent"] == "modeling_task"
    assert result["source"] == "fallback"


def test_heuristics_skip_the_judge_call(client, monkeypatch):
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("启发式短路不应出网")

    _configure_llm(client, monkeypatch, handler=never)

    with_attachments = _intake(client, "题目见附件", has_attachments=True)
    assert (with_attachments["intent"], with_attachments["source"]) == ("modeling_task", "heuristic")

    long_goal = _intake(client, "某物流公司需要根据历史运量预测下季度运量并优化车辆配置。" * 10)
    assert (long_goal["intent"], long_goal["source"]) == ("modeling_task", "heuristic")

    trivial = _intake(client, "你好")
    assert (trivial["intent"], trivial["source"]) == ("needs_info", "heuristic")
    assert "赛题正文" in trivial["reply"]


def test_judge_intent_and_reply_pass_through(client, monkeypatch):
    def judge(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        assert "接待员" in prompt
        assert "今天天气怎么样啊朋友" in prompt
        return _judge_reply(
            {"intent": "chat", "reply": "今天不错！把完整赛题发给我就可以开始建模。"}
        )

    _configure_llm(client, monkeypatch, handler=judge)
    result = _intake(client, "今天天气怎么样啊朋友")

    assert result["intent"] == "chat"
    assert result["source"] == "judge"
    assert "完整赛题" in result["reply"]


def test_needs_info_without_reply_gets_default_guidance(client, monkeypatch):
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply({"intent": "needs_info", "reply": ""}),
    )
    result = _intake(client, "帮我做一个数学建模的作业")

    assert result["intent"] == "needs_info"
    assert "赛题正文" in result["reply"], "模型没给 reply 时使用默认引导文案"


def test_judge_failure_lets_the_task_through(client, monkeypatch):
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: httpx.Response(500, json={"error": {"message": "boom"}}),
    )
    result = _intake(client, "帮我做一个数学建模的作业")
    assert (result["intent"], result["source"]) == ("modeling_task", "fallback")


def test_unparseable_judge_output_lets_the_task_through(client, monkeypatch):
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply({"intent": "自由发挥", "reply": "?"}),
    )
    result = _intake(client, "帮我做一个数学建模的作业")
    assert (result["intent"], result["source"]) == ("modeling_task", "fallback")


def test_intake_requires_login(second_client):
    response = second_client.post(API, json={"goal": "你好", "has_attachments": False})
    assert response.status_code == 401
