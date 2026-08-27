"""任务执行按自定义 API 换脑：六个建模阶段全部走真实 LLM 节点。

配置了接口的用户，整条链由 agents/skills 节点出网完成（这里用 MockTransport
模拟模型），实验阶段的代码经 agents/tools 的 python 沙箱真实执行；
未配置的用户保持 sim-0.1 模拟链路不变。
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
    "viability": "ok",
    "missing_info": [],
    "title": "共享单车调度优化",
    "problem_type": "优化",
    "objectives": ["给出调度方案"],
    "constraints": ["车辆容量有限"],
    "data_requirements": ["历史订单数据"],
    "key_assumptions": ["需求平稳"],
    "plan_outline": [
        {"stage": "PROBLEM_ANALYSIS", "text": "解析单车调度的子问题与容量约束"},
        {"stage": "DATA_PREPARATION", "text": "构造历史订单数据并画像高峰需求"},
        {"stage": "MODEL_PLANNING", "text": "比较整数规划与启发式并请求确认"},
        {"stage": "EXPERIMENTING", "text": "实现选定调度模型并对比基线"},
        {"stage": "VALIDATING", "text": "检验调度结果的稳健性与参数敏感度"},
        {"stage": "PAPER_WRITING", "text": "撰写含调度对比与检验结论的论文"},
    ],
}

PREPARATION_OUTPUT = {
    "profile_summary": "合成订单数据规模适中，质量良好，可直接建模",
    "datasets": [
        {
            "name": "历史订单",
            "source": "需构造",
            "fields": ["hour 时段", "demand 需求量（次）"],
            "quality_risks": ["高峰时段方差大"],
        }
    ],
    "preparation_steps": ["构造合成需求数据", "按时段聚合"],
    "missing_value_strategy": "前向填充",
    "outlier_strategy": "IQR 截断",
    "derived_features": ["高峰标志位"],
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

#: 实验阶段 stub 模型给出的脚本——由 python 沙箱真实执行：写产物文件并打印指标行。
#: newline='' 禁止平台换行转换，产物字节在 Windows 与 POSIX 上一致。
EXPERIMENT_CODE = (
    "import json\n"
    "with open('results.csv', 'w', encoding='utf-8', newline='') as fh:\n"
    "    fh.write('metric,value\\nrmse,0.5\\n')\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'rmse': 0.5}))\n"
)

EXPERIMENT_OUTPUT = {
    "approach_summary": "构造合成需求数据，贪心近似求解并与随机基线对比",
    "code": EXPERIMENT_CODE,
}

VALIDATION_OUTPUT = {
    "verdict": "concerns",
    "checks": [
        {"name": "结果合理性", "result": "pass", "note": "指标数量级正常"},
        {"name": "稳健性", "result": "warn", "note": "对需求率参数敏感"},
    ],
    "risks": ["合成数据外推风险"],
    "validation_summary": "结果整体可信，但对需求率参数敏感",
}

PAPER_OUTPUT = {
    "title": "基于整数规划的共享单车调度优化",
    "abstract": "本文建立整数规划模型求解调度问题……",
    "keywords": ["整数规划", "调度"],
    "sections": [
        {"heading": "问题重述", "content": "题目要求……"},
        {"heading": "模型检验", "content": "结果对需求率参数敏感，结论需谨慎外推。"},
    ],
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
    """按各阶段提示词的角色锚点路由 stub 输出（锚点来自 agents/prompts 模板正文）。"""
    prompt = json.loads(request.content)["messages"][-1]["content"]
    if "赛题原文" in prompt:
        return _llm_reply(ANALYSIS_OUTPUT)
    if "数据工程师" in prompt:
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "数据准备应携带分析产出"
        return _llm_reply(PREPARATION_OUTPUT)
    if "两套可执行的建模方案" in prompt:
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "规划节点应携带分析产出"
        assert PREPARATION_OUTPUT["profile_summary"] in prompt, "规划节点应携带数据画像摘要"
        return _llm_reply(PLANNING_OUTPUT)
    if "实验工程师" in prompt:
        assert '"id": "A"' in prompt, "实验节点应携带选中的方案 A"
        return _llm_reply(EXPERIMENT_OUTPUT)
    if "评审专家" in prompt:
        assert '"rmse": 0.5' in prompt, "检验节点应携带实验指标"
        return _llm_reply(VALIDATION_OUTPUT)
    if "论文写手" in prompt:
        assert VALIDATION_OUTPUT["validation_summary"] in prompt, "论文节点应携带检验结论"
        return _llm_reply(PAPER_OUTPUT)
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


def test_configured_run_uses_llm_nodes_end_to_end(client, monkeypatch):
    """全链真实节点：审批前三个阶段 + 审批后实验（沙箱真跑代码）/检验/论文。"""
    project = create_project(client)
    _configure_llm(client, monkeypatch)
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert approval["title"] == "请确认建模方案（A/B）后继续实验", "标题来自 LLM 节点而非模拟节点"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    by_node = {step["node"]: step for step in steps}
    assert by_node["PROBLEM_ANALYSIS"]["status"] == "SUCCEEDED"
    assert by_node["DATA_PREPARATION"]["status"] == "SUCCEEDED", "数据准备走真实节点"
    assert by_node["MODEL_PLANNING"]["status"] == "SUCCEEDED", "方案产出成功后停在审批"

    approve_when_asked(client, run["id"], option_id="approve")
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED", "确认后实验/检验/论文全部真实走完"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    statuses = {step["node"]: step["status"] for step in steps}
    for node in ("EXPERIMENTING", "VALIDATING", "PAPER_WRITING"):
        assert statuses[node] == "SUCCEEDED", f"{node} 应由真实节点完成"

    # 实验代码在沙箱真实执行：产物含代码写出的 results.csv 与论文草稿
    # （v1 契约不含 name 字段，按内容寻址 URI 的尾部文件名定位）
    artifacts = client.get(f"/api/v1/projects/{project['id']}/artifacts").json()["items"]
    by_file = {a["uri"].rstrip("/").rsplit("/", 1)[-1]: a for a in artifacts}
    assert "results.csv" in by_file, "实验代码创建的文件应进入产物列表"
    assert by_file["results.csv"]["kind"] == "table", "csv 产物按契约词汇归类"
    assert "experiment.py" in by_file, "生成的实验脚本应发布为可复现的 code 产物"
    assert by_file["experiment.py"]["kind"] == "code"
    assert "paper-draft.md" in by_file, "论文草稿应发布为产物"
    assert by_file["paper-draft.md"]["kind"] == "paper"

    csv_download = client.get(f"/api/v1/artifacts/{by_file['results.csv']['id']}/download")
    assert csv_download.status_code == 200
    assert csv_download.content == b"metric,value\nrmse,0.5\n"

    paper_download = client.get(f"/api/v1/artifacts/{by_file['paper-draft.md']['id']}/download")
    assert paper_download.status_code == 200
    paper_text = paper_download.content.decode("utf-8")
    assert "# 基于整数规划的共享单车调度优化" in paper_text
    assert "## 模型检验" in paper_text

    # 工具调用留痕：python_run 的 TOOL_CALLED 事件投影到 run.log
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    tool_calls = [entry for entry in logs if entry.get("tool") == "python_run"]
    assert tool_calls, "沙箱执行应产生 TOOL_CALLED 过程事件"
    assert tool_calls[0]["status"] == "succeeded"


def test_unconfigured_run_keeps_sim_workflow(client):
    run = create_run(client, create_project(client)["id"])
    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert approval["title"] == "确认建模方案后继续实验", "未配置接口时保持模拟节点文案"

    # 模拟链没有 plan_outline：执行计划回退固定阶段名（plan_text 全空）
    workspace = client.get(f"/api/v1/task-runs/{run['id']}/workspace").json()
    assert all(page["plan_text"] is None for page in workspace["pages"])


def test_plan_outline_personalizes_execution_plan(client, monkeypatch, validate_contract):
    """执行计划面板的渐进细化：揭示即本题化文案，方案产出后实验条目细化为选中方案。"""
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    wait_until(client, run["id"], pending_approval(client, run["id"]))

    workspace = client.get(f"/api/v1/task-runs/{run['id']}/workspace").json()
    validate_contract("modeling-workspace-view.schema.json", workspace)
    plan_by_key = {page["key"]: page["plan_text"] for page in workspace["pages"]}

    assert plan_by_key["running"] == "解析单车调度的子问题与容量约束", "计划文案来自问题分析的 plan_outline"
    assert plan_by_key["data"] == "构造历史订单数据并画像高峰需求"
    assert plan_by_key["model"] == "比较整数规划与启发式并请求确认"
    # 方案已产出（等待确认中）：实验条目细化为选中方案的名称与步骤
    assert "按方案「整数规划」实施" in plan_by_key["experiments"]
    assert "定义变量" in plan_by_key["experiments"]
    assert plan_by_key["editor"] == "撰写含调度对比与检验结论的论文"
    # 最终成果页不在 plan_outline 六阶段内：保持固定文案
    assert plan_by_key["complete"] is None


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
            # 修复重试必须把校验错误拼进提示词，而不是原样盲重发
            assert "上次输出未通过校验" in prompt
            assert "missing required property" in prompt
            return _llm_reply(ANALYSIS_OUTPUT)
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=flaky)
    run = create_run(client, create_project(client)["id"])

    wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert len(calls) == 2, "首次输出缺字段应触发且仅触发一次修复重试"


def test_reference_metadata_reaches_problem_analysis_prompt(client, monkeypatch):
    """@ 引用的赛题正文（reference_metadata）必须进入问题分析的附件摘要段：
    「这道题」+ 引用赛题的发送方式，题面全在引用里。"""
    seen: list[str] = []

    def router(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "赛题原文" in prompt:
            seen.append(prompt)
            return _llm_reply(ANALYSIS_OUTPUT)
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(
        client,
        create_project(client)["id"],
        goal="这道题",
        params={
            "reference_metadata": [
                {
                    "kind": "problem",
                    "title": "2024 APMCM C 共享单车调度",
                    "excerpt": "某城市共享单车系统需要在容量约束下优化调度……",
                }
            ]
        },
    )

    wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert seen, "问题分析应已执行"
    assert "【引用赛题】" in seen[0]
    assert "2024 APMCM C 共享单车调度" in seen[0]
    assert "容量约束下优化调度" in seen[0], "引用正文摘要应进入提示词"


def test_insufficient_input_stops_at_first_stage_with_guidance(client, monkeypatch):
    """准入门（诚实止损）：随便说一句话不再走完六阶段，第一阶段判定不足即停。"""
    calls: list[str] = []

    def gatekeeper(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        calls.append(prompt[:40])
        if "赛题原文" in prompt:
            return _llm_reply(
                {
                    "viability": "insufficient",
                    "missing_info": ["题目正文", "数据文件或数据说明", "求解目标"],
                    "title": "赛题信息缺失",
                    "problem_type": "未知",
                    "objectives": [],
                    "constraints": [],
                    "data_requirements": [],
                    "key_assumptions": [],
                    "plan_outline": [],
                }
            )
        raise AssertionError(f"判定不足后不应再有任何阶段调用模型: {prompt[:80]}")

    _configure_llm(client, monkeypatch, handler=gatekeeper)
    run = create_run(client, create_project(client)["id"], goal="你好啊")

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))
    assert "题目信息不足" in failed["failure"]["message"]
    assert "数据文件或数据说明" in failed["failure"]["message"]
    assert "重新发起" in failed["failure"]["message"], "失败消息应给出可执行的下一步引导"
    assert len(calls) == 1, "整条链只消耗一次模型调用"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    assert [step["node"] for step in steps] == ["PROBLEM_ANALYSIS"], "后续阶段一步未跑"

    # 项目名不被「赛题信息缺失」这类占位标题改写（失败路径不触发自动改名）
    project = client.get(f"/api/v1/projects/{run['project_id']}").json()
    assert project["name"] != "赛题信息缺失"


def test_experiment_runtime_failure_regenerates_with_feedback(client, monkeypatch):
    """实验代码在沙箱里报错时，第二轮生成必须携带运行时错误反馈并成功收尾。"""
    experiment_calls: list[str] = []

    def router(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "实验工程师" in prompt:
            experiment_calls.append(prompt)
            if len(experiment_calls) == 1:
                return _llm_reply(
                    {"approach_summary": "首版实现", "code": "raise RuntimeError('bad seed')"}
                )
            assert "bad seed" in prompt, "第二轮生成必须携带第一轮的运行时错误"
            assert "raise RuntimeError" in prompt, "第二轮生成必须携带上一轮代码"
            return _llm_reply(EXPERIMENT_OUTPUT)
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, create_project(client)["id"])

    approve_when_asked(client, run["id"], option_id="approve")
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    assert final["status"] == "COMPLETED", "一轮代码修复后任务应完整走完"
    assert len(experiment_calls) == 2
