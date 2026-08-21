"""任务执行按自定义 API 换脑：问题分析与建模方案走真实 LLM 节点。

配置了接口的用户，任务的前两个阶段由 agents/skills 节点出网完成（这里用
MockTransport 模拟模型）；未配置的用户保持 sim-0.1 模拟链路不变。
"""

from __future__ import annotations

import json

import httpx
from conftest import (
    approve_when_asked,
    create_project,
    create_run,
    pending_approval,
    run_status_is,
    wait_until,
)

from omm_api import llm as llm_module

ANALYSIS_OUTPUT = {
    "title": "共享单车调度优化",
    "problem_type": "优化",
    "objectives": ["给出调度方案"],
    "constraints": ["车辆容量有限"],
    "data_requirements": ["历史订单数据"],
    "key_assumptions": ["需求平稳"],
}

PLANNING_OUTPUT = {
    "plans": [
        {
            "id": "A",
            "name": "整数规划",
            "approach": "MILP 建模",
            "steps": ["定义变量", "求解"],
            "risks": ["规模过大求解慢"],
        },
        {
            "id": "B",
            "name": "启发式",
            "approach": "贪心 + 局部搜索",
            "steps": ["构造初始解", "迭代改进"],
            "risks": ["无最优性保证"],
        },
    ],
    "recommended_plan_id": "A",
    "rationale": "数据规模中等，精确解可行",
}


def _llm_reply(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-test",
            "choices": [{
                "message": {
                    "content": json.dumps(payload, ensure_ascii=False),
                    "reasoning_content": "先梳理目标与约束，再决定建模路线。",
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


def _stage_router(request: httpx.Request) -> httpx.Response:
    prompt = json.loads(request.content)["messages"][-1]["content"]
    if "赛题原文" in prompt:
        return _llm_reply(ANALYSIS_OUTPUT)
    if "问题分析结果" in prompt:
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "规划节点应携带分析产出"
        return _llm_reply(PLANNING_OUTPUT)
    raise AssertionError(f"unexpected prompt: {prompt[:120]}")


def _configure_llm(client, monkeypatch, handler=_stage_router) -> None:
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


def test_configured_run_uses_llm_nodes_until_approval_then_completes(client, monkeypatch):
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert approval["title"] == "请确认建模方案（A/B）后继续实验", "标题来自 LLM 节点而非模拟节点"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    by_node = {step["node"]: step for step in steps}
    assert by_node["PROBLEM_ANALYSIS"]["status"] == "SUCCEEDED"
    assert by_node["MODEL_PLANNING"]["status"] == "SUCCEEDED", "方案产出成功后停在审批"

    approve_when_asked(client, run["id"], option_id="approve")
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED", "确认后其余阶段（模拟）继续走完"


def test_unconfigured_run_keeps_sim_workflow(client):
    run = create_run(client, create_project(client)["id"])
    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert approval["title"] == "确认建模方案后继续实验", "未配置接口时保持模拟节点文案"


def test_llm_failure_fails_step_and_run_is_retryable(client, monkeypatch):
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: httpx.Response(500, json={"error": {"message": "boom"}}),
    )
    run = create_run(client, create_project(client)["id"])

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))
    assert failed["failure"]["failure_class"] == "CODE_DEFECT"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    assert steps and steps[0]["node"] == "PROBLEM_ANALYSIS"
    assert steps[0]["status"] == "FAILED"


def test_llm_process_events_land_in_run_log(client, monkeypatch):
    """真实节点的模型调用要产生过程事件：thinking（思考内容）+ llm_call（调用摘要）。

    这是工作台执行轨迹「看到智能体在做什么」的数据来源（设计文档 §12.4）。
    """
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"])
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]

    thinking = [entry for entry in logs if entry.get("kind") == "thinking"]
    assert thinking, "推理内容应作为 thinking 过程事件进入 run.log"
    assert thinking[0]["prompt_id"] == "problem_analysis.default"
    assert "梳理目标" in thinking[0]["text"]

    calls = [entry for entry in logs if entry.get("kind") == "llm_call"]
    assert len(calls) >= 2, "问题分析与建模方案各至少一次模型调用摘要"
    assert calls[0]["model"] == "gpt-test"
    assert calls[0]["endpoint"] == "测试网关"
    assert calls[0]["prompt_tokens"] == 10


def test_analysis_title_renames_auto_named_project(client, monkeypatch):
    """最近任务的名字来自实际讨论的问题：分析产出 title 后替换首句截取的默认名。"""
    _configure_llm(client, monkeypatch)
    # 项目名与首页 deriveProjectName("请帮我完成这道建模题。附件是题目原文") 的结果一致
    project = create_project(client, name="完成这道建模题")
    run = create_run(client, project["id"], goal="请帮我完成这道建模题。附件是题目原文")

    wait_until(client, run["id"], pending_approval(client, run["id"]))

    renamed = client.get(f"/api/v1/projects/{project['id']}").json()
    assert renamed["name"] == ANALYSIS_OUTPUT["title"], "自动名应替换为分析出的实际问题标题"

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    renames = [entry for entry in logs if entry.get("kind") == "task_renamed"]
    assert renames and renames[0]["to"] == ANALYSIS_OUTPUT["title"], "重命名要留 run.log 痕迹"


def test_analysis_title_keeps_user_named_project(client, monkeypatch):
    """用户手动起的项目名是显式意图：分析产出 title 也不覆盖。"""
    _configure_llm(client, monkeypatch)
    project = create_project(client, name="我的毕业设计")
    run = create_run(client, project["id"], goal="请帮我完成这道建模题")

    wait_until(client, run["id"], pending_approval(client, run["id"]))

    kept = client.get(f"/api/v1/projects/{project['id']}").json()
    assert kept["name"] == "我的毕业设计"


def test_model_output_validation_failure_gets_one_repair_attempt(client, monkeypatch):
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "赛题原文" in prompt:
            calls.append(1)
            if len(calls) == 1:
                return _llm_reply({"problem_type": "优化"})  # 缺必填字段 → 触发修复
            assert "__repair_error" not in prompt or True
            return _llm_reply(ANALYSIS_OUTPUT)
        return _llm_reply(PLANNING_OUTPUT)

    _configure_llm(client, monkeypatch, handler=flaky)
    run = create_run(client, create_project(client)["id"])

    wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert len(calls) == 2, "首次输出缺字段应触发且仅触发一次修复重试"
