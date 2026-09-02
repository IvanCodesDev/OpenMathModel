"""发送前接待判定（POST /v1/task-intake）：对话优先门控。

不变量：
- 未配置自定义 API 的用户永远放行（演示/模拟链的「发送即建任务」不变）；
- 启发式短路（无摘录附件 / 长题面 / 赛题标识 / 极短输入）不出网；
- 带正文摘录的附件必须进模型判定，不再「带附件即放行」——判定提示词
  含文件名与摘录，意图与回应透传；
- 模型判定的意图与回应透传；判定失败/输出无法解析一律放行——门控挂了
  绝不能挡住真实用户；
- 本地信号层否决弱模型的 needs_info 误判（有对象有目标时「信息不足」
  不是合法拦人理由），但不否决 chat——垃圾项目的防线要留着；
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


def _intake(
    client, goal: str, has_attachments: bool = False, attachments: list[dict] | None = None
) -> dict:
    payload: dict = {"goal": goal, "has_attachments": has_attachments}
    if attachments is not None:
        payload["attachments"] = attachments
    response = client.post(API, json=payload)
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

    # 寒暄不是「想建模但没说清」：按 chat 友好回应，别对着一句「你好」索要赛题正文
    greeting = _intake(client, "你好")
    assert (greeting["intent"], greeting["source"]) == ("chat", "heuristic")
    assert "完整赛题" in greeting["reply"]

    # 极短但带赛题标识：确实想做题却没给题面，引导补题
    bare_marker = _intake(client, "A题")
    assert (bare_marker["intent"], bare_marker["source"]) == ("needs_info", "heuristic")
    assert "赛题正文" in bare_marker["reply"]

    # 带赛题标识 = 带着题面来的，不必再问判定模型
    contest = _intake(client, "2024 高教社杯 A 题，生产线排产优化")
    assert (contest["intent"], contest["source"]) == ("modeling_task", "heuristic")


def test_attachment_excerpts_are_judged_not_blindly_passed(client, monkeypatch):
    """带正文摘录的附件进模型判定：内容与建模无关时不建任务。"""
    calls: list[str] = []

    def judge(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        calls.append(prompt)
        assert "附件「随笔.txt」" in prompt
        assert "昨天在公园里喂了鸽子" in prompt
        assert "看看这个" in prompt
        return _judge_reply(
            {"intent": "chat", "reply": "这份文件像是生活随笔，和建模无关；把完整赛题发给我即可开始。"}
        )

    _configure_llm(client, monkeypatch, handler=judge)
    result = _intake(
        client,
        "看看这个",
        has_attachments=True,
        attachments=[
            {"name": "随笔.txt", "excerpt": "昨天在公园里喂了鸽子，天气很好。", "characters": 320}
        ],
    )

    assert (result["intent"], result["source"]) == ("chat", "judge")
    assert "和建模无关" in result["reply"]
    assert len(calls) == 1, "附件证据只消耗一次判定调用"


def test_attachments_without_excerpt_still_pass_through(client, monkeypatch):
    """解析不出文字的附件内容不可见：维持放行，由 viability 门兜底。"""

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("无摘录附件应启发式放行，不出网")

    _configure_llm(client, monkeypatch, handler=never)
    result = _intake(
        client,
        "题目见附件",
        has_attachments=True,
        attachments=[{"name": "扫描件.pdf", "excerpt": "", "characters": 0}],
    )
    assert (result["intent"], result["source"]) == ("modeling_task", "heuristic")


def test_long_goal_passes_without_judging_attachments(client, monkeypatch):
    """粘贴了长题面时正文本身就是实质证据：不再为附件烧判定调用。"""

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("长题面应启发式放行，不出网")

    _configure_llm(client, monkeypatch, handler=never)
    result = _intake(
        client,
        "某物流公司需要根据历史运量预测下季度运量并优化车辆配置。" * 10,
        has_attachments=True,
        attachments=[{"name": "随笔.txt", "excerpt": "昨天在公园里喂了鸽子。", "characters": 120}],
    )
    assert (result["intent"], result["source"]) == ("modeling_task", "heuristic")


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


def test_local_signal_vetoes_needs_info_misjudgement(client, monkeypatch):
    """有对象有目标却被判「信息不足」= 弱模型误判，本地信号否决它放行。

    17:38 复现出的真实误判样本：判定模型（池中最弱者）看到简短一句话就索要
    「完整题目正文」，把真实用户挡在建模流程外。
    """
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply(
            {"intent": "needs_info", "reply": "请提供共享单车调度问题的完整题目正文。"}
        ),
    )
    result = _intake(client, "帮我做一个共享单车调度优化模型")

    assert result["intent"] == "modeling_task", "有对象（共享单车调度）有目标（优化）不该被拦"
    assert result["source"] == "heuristic", "结论由本地信号做出，不是判定模型"
    assert result["reply"] == ""


def test_local_signal_does_not_veto_chat(client, monkeypatch):
    """否决只针对 needs_info：模型说「与建模无关」时仍听它的，否则垃圾项目防线就没了。"""
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply(
            {"intent": "chat", "reply": "这是在聊调度软件选型，不是建模需求。"}
        ),
    )
    result = _intake(client, "你们公司排产调度用的什么优化软件呀")

    assert (result["intent"], result["source"]) == ("chat", "judge")


def test_inquiry_phrasing_keeps_the_judge_verdict(client, monkeypatch):
    """咨询式问句不触发本地信号：「怎么做选址优化」是在问建模，不是派建模任务。"""
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply({"intent": "needs_info", "reply": "请给出具体题目。"}),
    )
    result = _intake(client, "怎么给共享单车调度做优化")

    assert (result["intent"], result["source"]) == ("needs_info", "judge")


def test_method_names_are_not_mistaken_for_inquiry_phrasing(client, monkeypatch):
    """「机器学习」里的「学习」、「推荐系统」里的「推荐」是方法名/对象名，不是在提问：
    这类任务型输入必须保住本地否决层，否则弱模型的 needs_info 误判又能拦人。"""
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply({"intent": "needs_info", "reply": "请给出完整题目。"}),
    )
    for goal in (
        "用机器学习方法预测二手房成交价格",
        "优化推荐系统的召回排序策略",
        "基于深度学习的电力负荷预测",
    ):
        result = _intake(client, goal)
        assert (result["intent"], result["source"]) == ("modeling_task", "heuristic"), goal

    # 真问句照旧交回判定模型，不因例外表而放行
    asking = _intake(client, "机器学习预测房价怎么入门")
    assert (asking["intent"], asking["source"]) == ("needs_info", "judge")


def test_contest_abbreviations_need_letter_boundaries():
    """MCMC（马尔可夫链蒙特卡洛）不是美赛 MCM：纯字母赛题缩写要求两侧不是字母，
    否则贝叶斯统计的咨询会被当成赛题直接放行、连判定都不做。"""
    from omm_api.intake import _modeling_signal

    assert _modeling_signal("用 MCMC 做贝叶斯参数估计时先验怎么选") == ""
    assert _modeling_signal("2024MCM 的 C 题，帮我做") == "problem"
    assert _modeling_signal("MCM/ICM 2025 problem B") == "problem"
    assert _modeling_signal("参加 mathorcup 的排产题") == "problem"
    # 中文标识仍按子串匹配，边界规则只管纯字母缩写
    assert _modeling_signal("国赛A题的排产优化") == "problem"


def test_intent_alias_is_normalized(client, monkeypatch):
    """弱模型省掉后缀写成 "modeling" 时按 modeling_task 采纳，不白烧这次调用。"""
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: _judge_reply({"intent": "modeling", "reply": ""}),
    )
    result = _intake(client, "帮我做个建模")

    assert (result["intent"], result["source"]) == ("modeling_task", "judge")


def test_judge_output_wrapped_in_prose_is_parsed(client, monkeypatch):
    """模型在 JSON 前后写解释、套 Markdown 围栏时照样解析出意图。

    解释文字里的花括号会让「取最外层 { … } 跨度」失效，逐个候选片段兜住它。
    """
    content = (
        "用户只说了要建模 {没有具体对象}，因此判为信息不足：\n"
        '```json\n{"intent": "needs_info", "reply": "请提供题目正文与数据附件。"}\n```'
    )
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            },
        ),
    )
    result = _intake(client, "帮我做个建模")

    assert (result["intent"], result["source"]) == ("needs_info", "judge")
    assert result["reply"] == "请提供题目正文与数据附件。"


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
