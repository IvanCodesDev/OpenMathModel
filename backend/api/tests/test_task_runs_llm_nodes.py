"""任务执行按自定义 API 换脑：六个建模阶段全部走真实 LLM 节点。

配置了接口的用户，整条链由 agents/skills 节点出网完成（这里用 MockTransport
模拟模型），实验阶段的代码经 agents/tools 的 python 沙箱真实执行；
未配置的用户保持 sim-0.1 模拟链路不变。
"""

from __future__ import annotations

import json
import re

import httpx
from conftest import (
    SERVICE_ROOT,
    approve_when_asked,
    confirm_delivery,
    create_project,
    create_run,
    pending_approval,
    run_status_is,
    wait_until,
)

from omm_api import llm as llm_module
from omm_api.orm import ApprovalRequestRow, LlmUsageRow
from sqlalchemy import select

ANALYSIS_OUTPUT = {
    "viability": "ok",
    "missing_info": [],
    "title": "共享单车调度优化",
    "problem_type": "优化",
    "objectives": ["给出调度方案"],
    "constraints": ["车辆容量有限"],
    "data_requirements": ["历史订单数据"],
    "key_assumptions": ["需求平稳"],
    "subquestions": [{"id": "q1", "text": "给出调度方案", "depends_on": []}],
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

#: 方案阶段（H3）：三视角提议人并行各回一案；归约桩把它们收成 PLANNING_OUTPUT 的
#: A/B 两案（多出归约字段 role / source_views，投影只取契约五键）。
PROPOSAL_OUTPUT_BY_VIEW = {
    "机理建模": {
        "name": "排队论",
        "approach": "把调度点当排队系统",
        "steps": ["估计到达率", "求稳态指标", "比较布局"],
        "risks": ["到达非泊松"],
        "fit": "机理可解释",
    },
    "数据驱动": {
        "name": "需求预测",
        "approach": "回归预测各点需求",
        "steps": ["整理特征", "交叉验证", "预测"],
        "risks": ["数据不足"],
        "fit": "依赖历史数据",
    },
    "运筹优化": {
        "name": "整数规划",
        "approach": "MILP 建模",
        "steps": ["定义变量", "求解"],
        "risks": ["规模过大求解慢"],
        "fit": "离散调度天然是整数规划",
    },
}

REDUCE_OUTPUT = {
    **PLANNING_OUTPUT,
    "plans": [
        {
            **PLANNING_OUTPUT["plans"][0],
            "role": "primary",
            "source_views": ["operations_research"],
        },
        {
            **PLANNING_OUTPUT["plans"][1],
            "role": "baseline",
            "source_views": ["mechanism", "data_driven"],
        },
    ],
    "dropped": [],
    "progress_note": "三路提议归约为两案，推荐整数规划。",
}

#: 归约之后的规范化桩（H3 切片 2）：假设表 + 符号表。故意混入模型常犯的毛病
#: （$ 定界、枚举别名、写错的方案 id），由节点归一化、投影再清洗——两表进
#: plan-proposal 契约的 assumptions / symbols。
FORMALIZE_OUTPUT = {
    "assumptions": [
        {"id": "G1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面", "impact": "medium", "status": "confirmed"},
        {"id": "G2", "text": "调度点之间需求独立", "scope": "GLOBAL", "basis": "简化需要", "impact": "low", "status": "to_verify"},
        {"id": "A1", "text": "预算约束为硬约束", "scope": "方案 A", "basis": "题面", "impact": "High", "status": "critical"},
        {"id": "B1", "text": "局部搜索邻域可覆盖可行域", "scope": "B", "basis": "领域常识", "impact": "medium", "status": "to_verify"},
    ],
    "symbols": [
        {"symbol": "i \\in \\mathcal{I}", "kind": "set", "definition": "调度点索引", "unit": None, "range": "1…N", "plan_id": None},
        {"symbol": "$d_i$", "kind": "parameter", "definition": "调度点 i 的需求量", "unit": "辆", "range": "≥ 0", "plan_id": None},
        {"symbol": "x_i", "kind": "decision variable", "definition": "调度点 i 是否设站", "unit": "无", "range": "{0,1}", "plan_id": "A"},
        {"symbol": "z", "kind": "objective", "definition": "总调度成本", "unit": "元", "range": "最小化", "plan_id": "A"},
        {"symbol": "\\mathcal{N}(s)", "kind": "set", "definition": "解 s 的邻域", "unit": None, "range": None, "plan_id": "Plan B"},
    ],
}

#: 实验沙盒会话（H3）里 stub 模型发出的脚本——由 python 沙箱真实执行：
#: 写产物文件并打印指标行。newline='' 禁止平台换行转换，产物字节在
#: Windows 与 POSIX 上一致。
EXPERIMENT_CODE = (
    "import json\n"
    "with open('results.csv', 'w', encoding='utf-8', newline='') as fh:\n"
    "    fh.write('metric,value\\nrmse,0.5\\n')\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'rmse': 0.5}))\n"
)

#: 实验沙盒会话的终答（summary + 节点声明的两个叙事键）。approach_summary
#: 沿老字段名进入 stage-outputs 投影，下游断言不漂移。
EXPERIMENT_OUTPUT = {
    "summary": "贪心近似跑通，rmse 0.5 达标并写出结果表",
    "approach_summary": "构造合成需求数据，贪心近似求解并与随机基线对比",
    "progress_note": "实验代码已跑通，核心指标 rmse=0.5，下一步进入结果检验。",
}

#: 清洗沙盒会话（仅有数据文件下发的用例走到）：读 data/ 原文件、写 cleaned/
#: 同名文件、打印影响面统计标记行（数字为脚本真实统计）。
CLEANING_CODE = (
    "import csv, json, os\n"
    "os.makedirs('cleaned', exist_ok=True)\n"
    "with open('data/orders.csv', encoding='utf-8', newline='') as fh:\n"
    "    rows = list(csv.reader(fh))\n"
    "with open('cleaned/orders.csv', 'w', encoding='utf-8', newline='') as fh:\n"
    "    csv.writer(fh).writerows(rows)\n"
    "print('OMM_METRICS_JSON: ' + json.dumps("
    "{'rows_before': len(rows) - 1, 'rows_after': len(rows) - 1, "
    "'imputed_columns': []}))\n"
)

CLEANING_OUTPUT = {"summary": "按准备方案完成清洗：未删行、未插补，产物在 cleaned/"}

VALIDATION_OUTPUT = {
    "verdict": "concerns",
    "checks": [
        {"name": "结果合理性", "result": "pass", "note": "指标数量级正常"},
        {"name": "稳健性", "result": "warn", "note": "对需求率参数敏感"},
    ],
    "risks": ["合成数据外推风险"],
    "validation_summary": "结果整体可信，但对需求率参数敏感",
}


def robustness_code(*passed_flags: bool) -> str:
    """稳健性复跑沙盒会话里 stub 模型发出的检验脚本——由 python 沙箱真实执行，
    按传入的通过标志打印逐项判定（标记行数字就是 G3 的判定依据）。

    第一项回指全局假设 G2（FORMALIZE_OUTPUT 里的「待检验」项）：方案阶段有须
    检验的假设时，验证节点要求至少一项检查带 assumption_id；选全局假设是为了
    approve（方案 A）与 adopt:B 两条链共用同一份脚本。
    """
    checks = [
        {"id": "sensitivity", "name": "需求率扰动", "value": 0.05, "threshold": 0.2, "assumption_id": "G2"},
        {"id": "bootstrap", "name": "重采样稳定性", "value": 0.08, "threshold": 0.15},
        {"id": "baseline", "name": "对基线优势幅度", "value": 0.6, "threshold": 0.1},
    ]
    for check, passed in zip(checks, passed_flags):
        check["passed"] = passed
        if not passed:
            check["value"] = round(check["value"] * 5, 2)
        check["detail"] = "在阈值内" if passed else "超出阈值"
    # 嵌进 Python 源码要用 repr（True/False），json.dumps 的 true/false 在脚本里是 NameError
    return (
        "import json\n"
        f"checks = {checks!r}\n"
        "print('OMM_METRICS_JSON: ' + json.dumps({'checks': checks}))\n"
    )


ROBUSTNESS_CODE = robustness_code(True, True, True)
ROBUSTNESS_OUTPUT = {"summary": "三项稳健性检查均在阈值内，结论稳健"}

PAPER_OUTPUT = {
    "title": "基于整数规划的共享单车调度优化",
    "abstract": "本文建立整数规划模型求解调度问题……",
    "keywords": ["整数规划", "调度"],
    "sections": [
        {"heading": "问题重述", "content": "题目要求……"},
        {"heading": "模型检验", "content": "结果对需求率参数敏感，结论需谨慎外推。"},
    ],
}

# 论文阶段是分章多轮管线（总编规划 → 逐章写作 → 统稿收口）；标题与关键词
# 与 PAPER_OUTPUT 保持一致，stage-outputs 等下游断言不因此漂移。
PAPER_OUTLINE_OUTPUT = {
    "title": PAPER_OUTPUT["title"],
    "keywords": PAPER_OUTPUT["keywords"],
    "notation": "| 符号 | 含义 | 单位 |\n| --- | --- | --- |\n| $x_{ij}$ | 时段调度量 | 辆 |",
    "chapters": [
        {
            "heading": "1 问题重述",
            "brief": "背景与逐条任务要求",
            "target_chars": 600,
            "source_keys": ["problem_analysis"],
        },
        {
            "heading": "2 模型建立与求解",
            "brief": "整数规划建模与求解，引用 rmse=0.5",
            "target_chars": 1200,
            "source_keys": ["chosen_plan", "experiment_summary"],
        },
        {
            "heading": "3 模型检验",
            "brief": "检验结论与保留意见如实呈现",
            "target_chars": 700,
            "source_keys": ["validation_summary"],
        },
    ],
}

SECTION_LEAD = "围绕 rmse=0.5 与需求率敏感性的分析正文。"

PAPER_SECTION_OUTPUT = {
    "content": SECTION_LEAD,
    "digest": "本章围绕 rmse=0.5 完成分析",
}


def _section_reply(prompt: str) -> dict:
    """章节回复按提示词里的目标字数填充：达标稿不触发字数有界重写，调用数确定。"""
    matched = re.search(r"目标字数 (\d+) 字", prompt)
    target = int(matched.group(1)) if matched else 600
    return {
        "content": SECTION_LEAD + "析" * max(target - len(SECTION_LEAD), 0),
        "digest": PAPER_SECTION_OUTPUT["digest"],
    }

PAPER_FINALIZE_OUTPUT = {
    "abstract": PAPER_OUTPUT["abstract"],
    "keywords": PAPER_OUTPUT["keywords"],
    "progress_note": "论文已按三章完成，可在论文页查看与导出。",
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


# ── 沙盒会话（H3）的路由助手：清洗/实验是多轮写码/跑码会话，不再是单发模板 ──


def _wire_messages(request: httpx.Request) -> list[dict]:
    return json.loads(request.content)["messages"]


def _system_of(messages: list[dict]) -> str:
    """会话的 system 角色卡（沙盒阶段的锚点在这里，而不是最后一条消息）。"""
    return messages[0]["content"] if messages and messages[0]["role"] == "system" else ""


def _saw_observation(messages: list[dict]) -> bool:
    return any("[工具执行结果]" in str(m.get("content") or "") for m in messages)


def _python_envelope(code: str) -> httpx.Response:
    """模型侧的工具信封：chat_adapter 据此合成 python_run 调用。"""
    return _llm_reply({"tool": "python_run", "arguments": {"code": code}})


def _sandbox_reply(messages: list[dict], code: str, final: dict) -> httpx.Response:
    """沙盒会话的脚本应答：没跑过码先发信封，看到工具观察后交终答。

    按会话内容而非调用序号判断（与 agents/skills 测试的 sandbox_script 同
    纪律）：同一份脚本能原样服务修复波——每波都是全新装配的内环。
    """
    if _saw_observation(messages):
        return _llm_reply(final)
    return _python_envelope(code)


def _stage_router(request: httpx.Request) -> httpx.Response:
    """按各阶段提示词的角色锚点路由 stub 输出（锚点来自 agents/prompts 模板正文）。

    单发模板阶段的锚点在最后一条消息（complete 的整段渲染文本）；沙盒会话
    阶段（清洗/实验）的锚点在 system 角色卡，按会话脚本应答。
    """
    messages = _wire_messages(request)
    system = _system_of(messages)
    if "数据清洗执行工程师" in system:
        assert "- data/orders.csv" in system, "清洗任务卡应携带待清洗数据文件清单"
        return _sandbox_reply(messages, CLEANING_CODE, CLEANING_OUTPUT)
    if "实验工程师" in system:
        assert '"id": "A"' in system, "实验任务卡应携带选中的方案 A"
        return _sandbox_reply(messages, EXPERIMENT_CODE, EXPERIMENT_OUTPUT)
    if "稳健性检验工程师" in system:
        assert EXPERIMENT_CODE in system, "复跑任务卡应携带工作区里的实验脚本正文"
        assert '"rmse": 0.5' in system, "复跑任务卡应携带实验真实指标"
        assert "评审保留（warn）：稳健性" in system, "评审判读的保留意见应进风险点"
        return _sandbox_reply(messages, ROBUSTNESS_CODE, ROBUSTNESS_OUTPUT)
    prompt = messages[-1]["content"]
    if "赛题原文" in prompt:
        return _llm_reply(ANALYSIS_OUTPUT)
    if "数据工程师" in prompt:
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "数据准备应携带分析产出"
        return _llm_reply(PREPARATION_OUTPUT)
    proposer = re.search(r"「(.+?)」方案提议人", prompt)
    if proposer:
        # 方案阶段三路 Proposer 子代理（并行，各带自己的视角）
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "提议人应携带分析产出"
        assert PREPARATION_OUTPUT["profile_summary"] in prompt, "提议人应携带数据画像摘要"
        return _llm_reply(PROPOSAL_OUTPUT_BY_VIEW[proposer.group(1)])
    if "建模规范员" in prompt:
        # 归约之后的规范化（假设表 / 符号表）：拿到的是归约后的 A/B 两案与分析产出。
        # 规范化模板正文也提到「方案组长」，必须先于归约锚点判断。
        assert '"id": "A"' in prompt and '"id": "B"' in prompt, "规范化应携带归约后的方案卡"
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "规范化应携带分析产出"
        assert PREPARATION_OUTPUT["profile_summary"] in prompt, "规范化应携带数据画像摘要"
        return _llm_reply(FORMALIZE_OUTPUT)
    if "方案组长" in prompt:
        for view_output in PROPOSAL_OUTPUT_BY_VIEW.values():
            assert view_output["name"] in prompt, "归约应拿到三路提议"
        return _llm_reply(REDUCE_OUTPUT)
    if "两套可执行的建模方案" in prompt:
        # 无监督者装配的单次调用回落路径；API 装配有监督者，happy path 不应走到
        assert json.dumps(ANALYSIS_OUTPUT, ensure_ascii=False) in prompt, "规划节点应携带分析产出"
        assert PREPARATION_OUTPUT["profile_summary"] in prompt, "规划节点应携带数据画像摘要"
        return _llm_reply(PLANNING_OUTPUT)
    if "评审专家" in prompt:
        assert '"rmse": 0.5' in prompt, "检验节点应携带实验指标"
        return _llm_reply(VALIDATION_OUTPUT)
    # 论文分章多轮管线的三个角色锚点（总编 → 章节写手 → 统稿人）
    if "论文的总编" in prompt:
        assert VALIDATION_OUTPUT["validation_summary"] in prompt, "总编规划应携带检验结论"
        return _llm_reply(PAPER_OUTLINE_OUTPUT)
    if "章节写手" in prompt:
        assert PAPER_OUTLINE_OUTPUT["notation"] in prompt, "章节写作应携带全文符号约定"
        return _llm_reply(_section_reply(prompt))
    if "统稿人" in prompt:
        assert "本章围绕 rmse=0.5 完成分析" in prompt, "统稿应携带各章摘要"
        return _llm_reply(PAPER_FINALIZE_OUTPUT)
    if "论文写手" in prompt:
        # 回退路径（总编失败时整篇单次生成）；happy path 不应走到这里
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


def _stage_csv_attachment(client, project_id: str, name: str, csv_bytes: bytes) -> str:
    """把一份 CSV 写进内容寻址存储并登记为项目附件产物，返回 artifact_id。"""
    from omm_api.engine_glue import get_blobstore
    from omm_api.ids import new_id
    from omm_api.orm import ArtifactRow
    from omm_api.serialize import utcnow

    sha256, size = get_blobstore().put(csv_bytes)
    artifact_id = new_id("art")
    with client.app.state.db.session_factory() as session:
        session.add(
            ArtifactRow(
                id=artifact_id,
                project_id=project_id,
                run_id=None,
                kind="dataset",
                name=name,
                uri=f"local://{sha256}/{name}",
                sha256=sha256,
                size_bytes=size,
                media_type="text/csv",
                status="READY",
                created_at=utcnow(),
            )
        )
        session.commit()
    return artifact_id


def test_attachment_csv_is_profiled_into_data_stage_prompt(client, monkeypatch):
    """附件 CSV → 工作区下发 → table_profile 确定性画像 → 数据准备提示词。

    原则 5 的数据阶段落点：画像统计数字（行数/均值）由代码产出并原样进
    prompt，LLM 只判读；附件经 artifact_id 归属校验后按 basename 落入 data/。
    有数据文件在场时，清洗沙盒会话（H3）随数据阶段真实执行。
    """
    project = create_project(client)
    artifact_id = _stage_csv_attachment(
        client, project["id"], "orders.csv", "quarter,volume\n1,120.5\n2,130.0\n".encode("utf-8")
    )

    seen: dict[str, str] = {}

    def router(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "数据工程师" in prompt:
            seen["data_prompt"] = prompt
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(
        client,
        project["id"],
        goal="优化共享单车调度",
        params={"attachment_metadata": [{"name": "orders.csv", "artifact_id": artifact_id}]},
    )
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    prompt = seen["data_prompt"]
    assert "确定性画像" in prompt
    assert '"rows": 2' in prompt, "行数由 table_profile 统计"
    assert "120.5" in prompt and "130.0" in prompt, "数值统计原样进入提示词"
    assert "data/orders.csv" in prompt

    # 有数据文件在场：清洗沙盒会话随数据阶段真实执行（python_run 留痕成功），
    # 影响面极小（未删行、未插补）→ 不触发 G2，第一道审批仍是方案确认。
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    cleaning_runs = [entry for entry in logs if entry.get("tool") == "python_run"]
    assert cleaning_runs and cleaning_runs[0]["status"] == "succeeded"
    cleaning_calls = [
        entry
        for entry in logs
        if entry.get("kind") == "llm_call" and entry.get("prompt_id") == "data_cleaning.sandbox"
    ]
    assert len(cleaning_calls) == 2, "清洗会话恰两次模型调用（发码 + 终答）"


def test_provider_billing_error_fails_cleanly_without_traceback(client, monkeypatch):
    """供应商侧计费错误（HTTP 402 余额不足）→ 人话步骤失败并走标准 retry 路径。

    D2.1 纪律：UI 只显示人话文案——接口侧异常绝不允许以裸 traceback 上屏
    （曾在真实运行中把整段调用栈打给用户看）。指引必须对症：402 才提充值。
    """

    def broke(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    project = create_project(client)
    _configure_llm(client, monkeypatch, handler=broke)
    run = create_run(client, project["id"], goal="优化共享单车调度")

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))
    message = failed["failure"]["message"]
    assert "Traceback" not in message and "node raised" not in message
    assert "402" in message and "余额不足" in message
    assert "设置中心" in message and "充值" in message, "402 的指引必须指向充值/换接口"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    assert steps and steps[0]["status"] == "FAILED"


def test_network_error_guidance_never_mentions_recharge():
    """网络断流/超时/限流的失败指引绝不提余额充值（对症指引，D2.1）。

    真实误导案例：GLM 中转站流式断连（peer closed connection），旧版统一
    指引让用户去「检查余额与可用性，充值…」，用户余额明明充足。
    """
    from omm_api.engine_glue import _BudgetGuardedNode
    from omm_api.errors import ApiError

    class _Boom:
        def __init__(self, error: ApiError) -> None:
            self._error = error

        def run(self, ctx, services):
            raise self._error

    network_errors = [
        ApiError(502, "LLM_UNREACHABLE", "无法连接接口「GLM」（api.b.ai）：peer closed connection"),
        ApiError(504, "LLM_TIMEOUT", "接口「GLM」流式响应中断超过 300 秒（api.b.ai）"),
        ApiError(429, "LLM_RATE_LIMITED", "接口「GLM」触发限流（HTTP 429）"),
    ]
    for error in network_errors:
        result = _BudgetGuardedNode(_Boom(error)).run(None, None)
        assert result.status == "failed"
        assert error.message in result.error
        assert "充值" not in result.error and "余额" not in result.error.replace("与余额无关", "")
        assert "与余额无关" in result.error, "网络类失败要明说与余额无关"
        assert "重试" in result.error

    no_balance = ApiError(402, "LLM_NO_BALANCE", "接口「DeepSeek」余额不足（HTTP 402）：Insufficient Balance")
    result = _BudgetGuardedNode(_Boom(no_balance)).run(None, None)
    assert result.status == "failed"
    assert "充值" in result.error, "只有余额类失败才指向充值"


def test_sandbox_hardware_note_follows_gpu_probe(monkeypatch):
    """实验提示词的硬件口径跟随 GPU 探测：有 GPU 引导上 GPU，无 GPU 保守用 CPU。"""
    from omm_agent_skills import DEFAULT_HARDWARE_NOTE
    from omm_api import engine_glue

    engine_glue._sandbox_hardware.cache_clear()
    try:
        monkeypatch.setattr(
            engine_glue,
            "probe_sandbox_gpu",
            lambda: "NVIDIA GeForce RTX 4090, 24.0 GB VRAM",
        )
        note = engine_glue._sandbox_hardware()
        assert "检测到可用 GPU" in note and "RTX 4090" in note
        assert "禁止硬编码 cuda" in note, "GPU 口径必须要求自适应设备选择"

        engine_glue._sandbox_hardware.cache_clear()
        monkeypatch.setattr(engine_glue, "probe_sandbox_gpu", lambda: None)
        assert engine_glue._sandbox_hardware() == DEFAULT_HARDWARE_NOTE
    finally:
        # lru_cache 是进程级状态，脏了会污染同进程后续用例的真实探测
        engine_glue._sandbox_hardware.cache_clear()


def test_configured_run_uses_llm_nodes_end_to_end(client, monkeypatch, tmp_path):
    """全链真实节点：审批前三个阶段 + 审批后实验（沙箱真跑代码）/检验/论文。"""
    project = create_project(client)
    _configure_llm(client, monkeypatch)
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    # G1 三选（H3）：标题与选项来自归约后的方案卡（LLM 节点经 review_meta 声明），
    # 推荐案保留 approve id，其余候选 adopt:<id>，退回仍是 reject
    assert approval["title"] == "请确认建模方案：推荐 A「整数规划」；备选 B「启发式」", (
        "标题来自 LLM 节点而非模拟节点"
    )
    assert approval["decision_type"] == "confirm_plan"
    assert [option["id"] for option in approval["options"]] == ["approve", "adopt:B", "reject"]
    assert approval["options"][0]["label"] == "采用推荐方案 A（整数规划）"
    assert approval["options"][0].get("recommended") is True
    assert approval["options"][1]["label"] == "改用方案 B（启发式）"
    assert approval["options"][1]["description"].startswith("可用基线：")
    with client.app.state.db.session_factory() as session:
        row = session.get(ApprovalRequestRow, approval["id"])
        assert row.evidence["gate"] == "G1"
        assert row.evidence["impact"]["proposers"] == {
            "succeeded": ["mechanism", "data_driven", "operations_research"],
            "failed": [],
        }
        assert [plan["role"] for plan in row.evidence["impact"]["plans"]] == ["primary", "baseline"]

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    by_node = {step["node"]: step for step in steps}
    assert by_node["PROBLEM_ANALYSIS"]["status"] == "SUCCEEDED"
    assert by_node["DATA_PREPARATION"]["status"] == "SUCCEEDED", "数据准备走真实节点"
    assert by_node["MODEL_PLANNING"]["status"] == "SUCCEEDED", "方案产出成功后停在审批"
    # 方案页契约投影：三路提议归约后的两案，多出的归约字段不进契约五键
    proposal = client.get(f"/api/v1/task-runs/{run['id']}/stage-outputs").json()["plan_proposal"]
    assert [plan["id"] for plan in proposal["plans"]] == ["A", "B"]
    assert set(proposal["plans"][0]) == {"id", "name", "approach", "steps", "risks"}
    assert proposal["recommended_plan_id"] == "A"
    # 假设表 / 符号表随方案卡一起在 G1 之前就位（切片 2）
    assert [entry["id"] for entry in proposal["assumptions"]] == ["G1", "G2", "A1", "B1"]
    assert [entry["symbol"] for entry in proposal["symbols"]] == [
        "i \\in \\mathcal{I}", "d_i", "x_i", "z", "\\mathcal{N}(s)",
    ]
    # 三路提议 + 归约 + 规范化 = 方案阶段 5 次模型调用，全部记入用量监控并归属方案节点
    with client.app.state.db.session_factory() as session:
        usage_rows = session.execute(
            select(LlmUsageRow).where(LlmUsageRow.run_id == run["id"])
        ).scalars().all()
    assert len(usage_rows) == 7, "题意 1 + 数据 1 + 提议 3 + 归约 1 + 规范化 1（尚未审批）"
    assert sum(1 for row in usage_rows if row.run_id == run["id"]) == 7

    approve_when_asked(client, run["id"], option_id="approve")
    # 论文发布后停在 G4 定稿闸门（必停）：草稿与审计已落库，卡片给「确认交付 / 退回修改」
    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert gate["decision_type"] == "generic"
    assert gate["title"].startswith("论文草稿已生成（3 章")
    assert "全部对账通过" in gate["title"]
    assert [option["id"] for option in gate["options"]] == [
        "confirm_delivery",
        "redo:PAPER_WRITING",
    ]
    assert gate["options"][0].get("recommended") is True, "0 审计发现 → 推荐确认交付"
    assert not gate["options"][1].get("recommended")
    run_view = client.get(f"/api/v1/task-runs/{run['id']}").json()
    assert run_view["status"] == "WAITING_APPROVAL"
    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    paper_status = {s["node"]: s["status"] for s in steps}["PAPER_WRITING"]
    assert paper_status == "SUCCEEDED", "闸门前论文步骤已成功落库"
    # 闸门元数据（gate / impact）留在审批行 evidence 里供投影与审计用，不出契约接口
    with client.app.state.db.session_factory() as session:
        row = session.get(ApprovalRequestRow, gate["id"])
        assert row.evidence["gate"] == "G4"
        assert row.evidence["impact"]["audit_findings_total"] == 0
        assert row.evidence["impact"]["frozen_numbers_total"] >= 1
        assert row.evidence["impact"]["recommended"] == "confirm_delivery"
    draft = client.get(f"/api/v1/task-runs/{run['id']}/stage-outputs").json()["document_draft"]
    assert draft is not None, "等待交付确认期间论文页已能看到草稿"
    assert {e["id"] for e in draft["frozen_numbers"]} >= {"metrics.rmse"}
    assert draft["audit_findings"] == []
    confirm_delivery(client, run["id"])
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

    # 沙盒工作区必须落在 create_app 注入的 workspaces_dir（本用例的 tmp_path），
    # 而不是进程 .env 指向的真实 backend/api/data/workspaces：曾因 engine_glue 直读
    # get_settings() 让每次跑测试都往开发数据目录写几十个 run_* 目录，还触发
    # 监视该目录的 uvicorn --reload 反复重启。
    assert (tmp_path / "workspaces" / run["id"] / "experiment.py").is_file(), (
        "最终实验脚本应落在测试自己的工作区根"
    )
    assert not (SERVICE_ROOT / "data" / "workspaces" / run["id"]).exists(), (
        "测试不得污染开发数据目录"
    )

    csv_download = client.get(f"/api/v1/artifacts/{by_file['results.csv']['id']}/download")
    assert csv_download.status_code == 200
    assert csv_download.content == b"metric,value\nrmse,0.5\n"

    paper_download = client.get(f"/api/v1/artifacts/{by_file['paper-draft.md']['id']}/download")
    assert paper_download.status_code == 200
    paper_text = paper_download.content.decode("utf-8")
    assert "# 基于整数规划的共享单车调度优化" in paper_text
    assert "## 3 模型检验" in paper_text, "章节骨架来自总编规划（分章多轮管线）"

    # 分章直播的进度事件（骨架 + 每章一条）应落入 run.log
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    outline_events = [entry for entry in logs if entry.get("kind") == "paper_outline"]
    assert outline_events and outline_events[0]["total"] == 3
    section_events = [entry for entry in logs if entry.get("kind") == "paper_section"]
    assert [entry["index"] for entry in section_events] == [1, 2, 3]
    assert section_events[0]["heading"] == "1 问题重述"
    assert "rmse=0.5" in section_events[0]["content"]

    # 工具调用留痕：python_run 的 TOOL_CALLED 事件投影到 run.log；三路提议子代理
    # 的 spawn / result 审计同路落 run.log（工作台执行轨迹可见）
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    tool_calls = [entry for entry in logs if entry.get("tool") == "python_run"]
    assert tool_calls, "沙箱执行应产生 TOOL_CALLED 过程事件"
    assert tool_calls[0]["status"] == "succeeded"
    proposer_audits = [
        entry for entry in logs if str(entry.get("tool") or "").startswith("subagent:proposer:")
    ]
    spawns = [entry for entry in proposer_audits if entry.get("phase") == "spawn"]
    results = [entry for entry in proposer_audits if entry.get("phase") == "result"]
    assert sorted(entry["tool"] for entry in spawns) == [
        "subagent:proposer:data_driven",
        "subagent:proposer:mechanism",
        "subagent:proposer:operations_research",
    ]
    assert [entry["envelope_status"] for entry in results] == ["done"] * 3


def test_g1_adopt_b_routes_downstream_stages_to_plan_b(client, monkeypatch):
    """G1 三选（H3）：用户改选备选案 B，实验任务卡与论文材料都按 B 走，台账记 adopt:B。"""
    seen: dict[str, str] = {}

    def router(request: httpx.Request) -> httpx.Response:
        messages = _wire_messages(request)
        system = _system_of(messages)
        if "实验工程师" in system:
            seen["experiment_system"] = system
            return _sandbox_reply(messages, EXPERIMENT_CODE, EXPERIMENT_OUTPUT)
        if "稳健性检验工程师" in system:
            seen["robustness_system"] = system
        prompt = messages[-1]["content"]
        if "论文的总编" in prompt:
            seen["outline_prompt"] = prompt
        if "评审专家" in prompt:
            seen["judgement_prompt"] = prompt
        return _stage_router(request)

    project = create_project(client)
    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert [option["id"] for option in approval["options"]] == ["approve", "adopt:B", "reject"]
    approve_when_asked(client, run["id"], option_id="adopt:B")

    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert gate["title"].startswith("论文草稿已生成"), "选 B 后照常走到 G4"
    assert '"id": "B"' in seen["experiment_system"] and "启发式" in seen["experiment_system"]
    assert '"id": "A"' not in seen["experiment_system"], "实验任务卡只带用户选定的方案"
    # 假设表随选案进实验任务卡（切片 3）：全局 + B 的假设，A 的不进
    assert "## 模型假设" in seen["experiment_system"]
    assert "- G1【已确认｜影响中｜全局】需求服从泊松分布" in seen["experiment_system"]
    assert "- B1【待检验｜影响中｜方案 B】局部搜索邻域可覆盖可行域" in seen["experiment_system"]
    assert "A1【" not in seen["experiment_system"]
    # 判读与稳健性任务卡只带须检验的假设（G2 / B1 都是待检验；已确认的 G1 不进）
    assert "- G2【待检验｜影响低｜全局】调度点之间需求独立" in seen["judgement_prompt"]
    assert "- B1【待检验｜影响中｜方案 B】" in seen["judgement_prompt"]
    assert "G1【" not in seen["judgement_prompt"] and "A1【" not in seen["judgement_prompt"]
    assert "## 须检验的模型假设" in seen["robustness_system"]
    assert "- G2【待检验｜影响低｜全局】调度点之间需求独立" in seen["robustness_system"]
    assert "- B1【待检验｜影响中｜方案 B】" in seen["robustness_system"]
    assert "A1【" not in seen["robustness_system"]
    # 总编材料里的选中方案是 B（材料以 JSON 给出，id 与方法名一起核对）
    assert '"id": "B"' in seen["outline_prompt"] and "启发式" in seen["outline_prompt"]
    assert '"id": "A"' not in seen["outline_prompt"]
    # 检验脚本回指全局假设 G2 → 论文材料按覆盖表逐条说：G2 通过、B1 未被覆盖进局限性
    assert (
        "模型假设检验：G2「调度点之间需求独立」通过（sensitivity）；"
        "B1「局部搜索邻域可覆盖可行域」未被检验覆盖，须在局限性中说明。"
    ) in seen["outline_prompt"]
    resolved = client.get(f"/api/v1/task-runs/{run['id']}/approvals").json()["items"]
    g1 = next(item for item in resolved if item["id"] == approval["id"])
    assert g1["status"] == "RESOLVED" and g1["resolution"]["option_id"] == "adopt:B"

    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"


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
    # 方案已产出（等待确认中）：实验条目细化为「按方案实施」短句——只点名方案，
    # 不拼步骤明细，保持与其他条目等长感（步骤全文在建模方案页）。
    assert plan_by_key["experiments"] == "按方案「整数规划」实施"
    assert plan_by_key["editor"] == "撰写含调度对比与检验结论的论文"
    # 所有条目都是面板可容纳的单行短句
    assert all(len(text) <= 40 for text in plan_by_key.values() if text)
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
    # 模型接口失败按真实类别归为 TRANSIENT（重试可恢复），不再无脑 CODE_DEFECT
    assert failed["failure"]["failure_class"] == "TRANSIENT"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    assert steps and steps[0]["node"] == "PROBLEM_ANALYSIS"
    assert steps[0]["status"] == "FAILED"


def test_llm_process_events_land_in_run_log(client, monkeypatch):
    """真实节点的模型调用要产生过程事件：llm_call_started（调用开始，供工作台
    立即显示走秒思考行）→ thinking（思考内容）→ llm_call（调用摘要）。

    这是工作台执行轨迹「看到智能体在做什么」的数据来源（设计文档 §12.4）。
    """
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"])
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]

    started = [entry for entry in logs if entry.get("kind") == "llm_call_started"]
    assert started, "每次模型调用开始都应有 llm_call_started 过程事件"
    assert started[0]["prompt_id"] == "problem_analysis.default"

    thinking = [entry for entry in logs if entry.get("kind") == "thinking"]
    assert thinking, "推理内容应作为 thinking 过程事件进入 run.log"
    assert thinking[0]["prompt_id"] == "problem_analysis.default"
    assert "梳理目标" in thinking[0]["text"]
    assert logs.index(started[0]) < logs.index(thinking[0]), "调用开始事件先于思考内容"

    calls = [entry for entry in logs if entry.get("kind") == "llm_call"]
    assert len(calls) >= 2, "问题分析与建模方案各至少一次模型调用摘要"
    assert len(started) == len(calls), "开始事件与调用摘要一一配对"
    assert calls[0]["model"] == "gpt-test"
    assert calls[0]["endpoint"] == "测试网关"
    assert calls[0]["prompt_tokens"] == 10
    # D2.2 审计：llm.chat 类调用必须带最终 prompt 指纹（64 位 sha256 hex）
    assert re.fullmatch(r"[0-9a-f]{64}", calls[0]["prompt_hash"])


def _sse_from_reply(reply: httpx.Response) -> httpx.Response:
    """把非流式 stub 响应转成 OpenAI 形状的 SSE 流（思考帧 + 两段正文帧）。"""
    message = reply.json()["choices"][0]["message"]
    content = message.get("content") or ""
    half = max(1, len(content) // 2)
    frames: list[dict] = []
    if message.get("reasoning_content"):
        frames.append({"choices": [{"delta": {"reasoning_content": message["reasoning_content"]}}]})
    for piece in (content[:half], content[half:]):
        if piece:
            frames.append({"choices": [{"delta": {"content": piece}}]})
    frames.append({
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })
    body = "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


def test_streaming_llm_emits_delta_events(client, monkeypatch):
    """真流式网关：思考与正文增量应作为 llm_delta 过程事件进入 run.log
    （工作台「实时查看生成内容」的数据源），且开始/思考/摘要事件次序不变。"""
    _configure_llm(client, monkeypatch, handler=lambda request: _sse_from_reply(_stage_router(request)))
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]

    deltas = [entry for entry in logs if entry.get("kind") == "llm_delta"]
    assert deltas, "流式调用应产生 llm_delta 过程事件"
    analysis_deltas = [d for d in deltas if d["prompt_id"] == "problem_analysis.default"]
    assert {d["channel"] for d in analysis_deltas} == {"reasoning", "text"}
    joined = "".join(d["text"] for d in analysis_deltas if d["channel"] == "text")
    assert '"title"' in joined, "正文增量拼接后应是模型原始输出"

    started = next(entry for entry in logs if entry.get("kind") == "llm_call_started")
    thinking = next(entry for entry in logs if entry.get("kind") == "thinking")
    assert logs.index(started) < logs.index(analysis_deltas[0]) < logs.index(thinking), (
        "增量事件应在调用开始之后、思考摘要之前"
    )
    calls = [entry for entry in logs if entry.get("kind") == "llm_call"]
    assert calls and calls[0]["prompt_tokens"] == 10, "流式调用摘要应携带用量"


def test_failed_llm_call_emits_terminal_event(client, monkeypatch):
    """调用失败必须给事件流一个收尾（llm_call_failed）：没有它，工作台的
    走秒思考行会永远悬挂，页面重进时堆出一排僵尸行。"""
    _configure_llm(
        client,
        monkeypatch,
        handler=lambda request: httpx.Response(500, json={"error": {"message": "boom"}}),
    )
    run = create_run(client, create_project(client)["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    started = [entry for entry in logs if entry.get("kind") == "llm_call_started"]
    failed = [entry for entry in logs if entry.get("kind") == "llm_call_failed"]
    settled = [entry for entry in logs if entry.get("kind") == "llm_call"]
    assert failed, "失败的调用应留下 llm_call_failed 事件"
    assert failed[0]["prompt_id"] == "problem_analysis.default"
    assert "boom" in failed[0]["error"]
    assert len(started) == len(failed) + len(settled), "每个开始事件都要有终结事件配对"


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

    # 材料读取要在执行轨迹留痕（工作台「已读取题目附件与引用材料」行的数据源）
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    ingested = [entry for entry in logs if entry.get("kind") == "materials_ingested"]
    assert len(ingested) == 1, "材料读取事件应恰好一条（重试不重复）"
    assert ingested[0]["references"] == ["2024 APMCM C 共享单车调度"]


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
                    "subquestions": [],
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


def test_paper_stage_retry_resumes_from_completed_chapters(client, monkeypatch):
    """论文断点续写：第 3 章首轮尝试失败 → 运行失败；重试后总编不重跑、
    前两章直接复用事件检查点，只补写失败的章节并完整收尾。"""
    section_prompts: list[str] = []
    recovered: list[bool] = []

    def router(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "章节写手" in prompt:
            section_prompts.append(prompt)
            if "「3 模型检验」" in prompt and not recovered:
                return _llm_reply({"digest": "缺 content 字段"})  # 校验失败 → 修复后仍失败
            return _llm_reply(_section_reply(prompt))
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, create_project(client)["id"])

    approve_when_asked(client, run["id"], option_id="approve")
    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))
    assert "第 3/3 章" in failed["failure"]["message"], "失败必须可归因到章节号"

    recovered.append(True)
    retried = client.post(f"/api/v1/task-runs/{run['id']}/actions", json={"action": "retry"})
    assert retried.status_code == 200, retried.text
    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"

    # 章节调用分布：首轮 第1章 + 第2章 + 第3章×2（含修复）；重试轮只补第 3 章
    def chapter_calls(heading: str) -> int:
        return sum(1 for prompt in section_prompts if f"「{heading}」" in prompt)

    assert chapter_calls("1 问题重述") == 1, "已完成章节不得重写"
    assert chapter_calls("2 模型建立与求解") == 1, "已完成章节不得重写"
    assert chapter_calls("3 模型检验") == 3

    # 重试轮跳过总编：整个运行只规划过一次骨架
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    outline_events = [entry for entry in logs if entry.get("kind") == "paper_outline"]
    assert len(outline_events) == 1

    # 续写轮的执行事实进入 step 指标
    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    paper_steps = [step for step in steps if step["node"] == "PAPER_WRITING"]
    assert [step["status"] for step in paper_steps] == ["FAILED", "SUCCEEDED"]


def test_g4_redo_rewrites_paper_from_scratch_and_gates_again(client, monkeypatch):
    """G4 选「退回修改」（§11.1 必停闸门的第二个选项）：已交稿那趟的章节检查点
    不得被当成断点续写——输入指纹原样不变，但用户的修改要求走运行中备注注入，
    重做必须从总编起整篇重写、每章都带上「用户补充要求」，写完再次挂 G4，确认后完成。
    与上一例对照：崩溃重试续写（没交稿）vs 人要求重做（已交稿）。"""
    section_prompts: list[str] = []

    def router(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "章节写手" in prompt:
            section_prompts.append(prompt)
            return _llm_reply(_section_reply(prompt))
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, create_project(client)["id"])

    approve_when_asked(client, run["id"], option_id="approve")  # G1
    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert [o["id"] for o in gate["options"]] == ["confirm_delivery", "redo:PAPER_WRITING"]
    first_round_calls = len(section_prompts)
    assert first_round_calls == 3, "首趟三章各写一次（达标稿不触发重写）"

    # 用户先在聊天框写明修改要求（§11.3 运行中备注），再选「退回修改」
    note = client.post(
        f"/api/v1/task-runs/{run['id']}/notes",
        json={"text": "每章开头先给出本章结论", "scope": "PAPER_WRITING"},
    )
    assert note.status_code == 201, note.text
    resolved = client.post(
        f"/api/v1/task-runs/{run['id']}/actions",
        json={"action": "approve", "approval_id": gate["id"], "option_id": "redo:PAPER_WRITING"},
    )
    assert resolved.status_code == 200, resolved.text
    run_view = client.get(f"/api/v1/task-runs/{run['id']}").json()
    assert run_view["status"] == "RUNNING"
    assert run_view["current_node"] == "PAPER_WRITING", "退回修改：current_node 摆回论文阶段"

    second_gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert second_gate["id"] != gate["id"]
    assert [o["id"] for o in second_gate["options"]] == ["confirm_delivery", "redo:PAPER_WRITING"]

    # 整篇重写：三章各再写一次，且每一章的重写调用都带上了用户的修改要求
    rewrite_prompts = section_prompts[first_round_calls:]
    assert len(rewrite_prompts) == 3, "已交稿的章节不得被当成断点续写复用"
    assert all("每章开头先给出本章结论" in p for p in rewrite_prompts), "备注注入每一章的重写"
    assert not any("每章开头先给出本章结论" in p for p in section_prompts[:first_round_calls])

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    assert len([e for e in logs if e.get("kind") == "paper_outline"]) == 2, "重做从总编起"
    assert len([e for e in logs if e.get("kind") == "paper_published"]) == 2, "两趟各交稿一次"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    paper_steps = [step for step in steps if step["node"] == "PAPER_WRITING"]
    assert [(s["attempt"], s["status"]) for s in paper_steps] == [
        (1, "SUCCEEDED"),
        (2, "SUCCEEDED"),
    ]
    assert not any(s["node"] == "VALIDATING" and s["attempt"] > 1 for s in steps), "上游不重做"

    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"
    types = [event["type"] for event in client.get(
        f"/api/v1/task-runs/{run['id']}/events/history"
    ).json()["items"]]
    assert types.count("approval.requested") == 3, "G1 + G4 ×2"


def test_experiment_runtime_failure_regenerates_with_feedback(client, monkeypatch):
    """实验代码在沙箱里报错时，修复波必须携带运行时错误反馈并成功收尾。

    沙盒执行体按「波」修复（§5.4 R2）：第一波运行失败 → 验收断言未过 →
    第二波任务卡带 stderr 反馈与上一版代码重新装配（结构化反馈，不转录全
    对话）。"""
    experiment_cards: list[str] = []

    def router(request: httpx.Request) -> httpx.Response:
        messages = _wire_messages(request)
        if "实验工程师" not in _system_of(messages):
            return _stage_router(request)
        if _saw_observation(messages):
            return _llm_reply(EXPERIMENT_OUTPUT)
        card = messages[-1]["content"]
        experiment_cards.append(card)
        if "上一轮未通过验收" not in card:
            return _python_envelope("raise RuntimeError('bad seed')")
        assert "bad seed" in card, "修复波必须携带第一波的运行时错误"
        assert "raise RuntimeError" in card, "修复波必须携带上一轮代码"
        return _python_envelope(EXPERIMENT_CODE)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, create_project(client)["id"])

    approve_when_asked(client, run["id"], option_id="approve")
    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    assert final["status"] == "COMPLETED", "一波代码修复后任务应完整走完"
    assert len(experiment_cards) == 2, "两波各装配一次任务卡"

    # 两次沙箱运行都留 TOOL_CALLED 痕：先失败后成功。按 step 归属区分实验与
    # 检验阶段——验证阶段自 G3 落地后也在沙盒里复跑一次稳健性检查。
    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    logs = [event["payload"] for event in events if event["type"] == "run.log"]
    runs = [entry for entry in logs if entry.get("tool") == "python_run"]
    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    node_of_step = {step["id"]: step["node"] for step in steps}
    by_node: dict[str, list[str]] = {}
    for entry in runs:
        by_node.setdefault(node_of_step[entry["step_id"]], []).append(entry["status"])
    assert by_node["EXPERIMENTING"] == ["failed", "succeeded"]
    assert by_node["VALIDATING"] == ["succeeded"], "修好的脚本进工作区后检验阶段照常复跑"


def test_g2_data_gate_requests_confirmation_and_ledgers_decision(
    client, monkeypatch, validate_contract
):
    """G2 数据闸门端到端（§9.1）：清洗删行超阈值 → 运行停在 generic 决策卡
    （三选项 + 推荐项，CTA 预选推荐）→ 用户拍板「采用清洗结果」→ 决策进
    review_decisions 台账并进实验任务卡 → 后续方案确认（G1）照常 → 完成。"""
    project = create_project(client)
    csv_lines = ["quarter,volume"] + [f"{i},{100 + i}" for i in range(1, 41)]
    artifact_id = _stage_csv_attachment(
        client, project["id"], "orders.csv", ("\n".join(csv_lines) + "\n").encode("utf-8")
    )

    # 删 40 行中的 10 行（25% > 阈值 5%）：触发 G2 的清洗脚本
    heavy_cleaning_code = (
        "import csv, json, os\n"
        "os.makedirs('cleaned', exist_ok=True)\n"
        "with open('data/orders.csv', encoding='utf-8', newline='') as fh:\n"
        "    rows = list(csv.reader(fh))\n"
        "kept = rows[:31]\n"
        "with open('cleaned/orders.csv', 'w', encoding='utf-8', newline='') as fh:\n"
        "    csv.writer(fh).writerows(kept)\n"
        "print('OMM_METRICS_JSON: ' + json.dumps("
        "{'rows_before': len(rows) - 1, 'rows_after': len(kept) - 1, "
        "'imputed_columns': []}))\n"
    )

    experiment_cards: list[str] = []

    def router(request: httpx.Request) -> httpx.Response:
        messages = _wire_messages(request)
        system = _system_of(messages)
        if "数据清洗执行工程师" in system:
            return _sandbox_reply(
                messages, heavy_cleaning_code, {"summary": "剔除异常行 25% 后落 cleaned/"}
            )
        if "实验工程师" in system:
            experiment_cards.append(system)
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(
        client,
        project["id"],
        goal="优化共享单车调度",
        params={"attachment_metadata": [{"name": "orders.csv", "artifact_id": artifact_id}]},
    )

    # 第一道闸是 G2（数据阶段先于方案确认）：generic 决策卡，选项来自节点声明
    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    validate_contract("approval-request.schema.json", gate)
    assert gate["decision_type"] == "generic"
    assert "25.0%" in gate["title"] and "数据清洗影响面较大" in gate["title"]
    options = {option["id"]: option for option in gate["options"]}
    assert list(options) == ["adopt_cleaned", "use_raw", "reject"]
    assert options["adopt_cleaned"].get("recommended") is True, "推荐项必须显式标出"
    assert not options["use_raw"].get("recommended")

    # 工作台 CTA 落到推荐项：多正向选项时预选 recommended（而非不敢默选）
    workspace = client.get(f"/api/v1/task-runs/{run['id']}/workspace").json()
    validate_contract("modeling-workspace-view.schema.json", workspace)
    action = workspace["agent"]["action"]
    assert action["kind"] == "approve"
    assert action["approval_id"] == gate["id"]
    assert action["option_id"] == "adopt_cleaned"

    # 拍板「采用清洗结果」：决策台账 → 实验任务卡（模型据此选 cleaned/ 目录）
    resolved = client.post(
        f"/api/v1/task-runs/{run['id']}/actions",
        json={"action": "approve", "approval_id": gate["id"], "option_id": "adopt_cleaned"},
    )
    assert resolved.status_code == 200, resolved.text

    # G2 之后照常走到 G1 方案确认，批准后走到 G4 定稿确认，再批准全链完成
    approve_when_asked(client, run["id"], option_id="approve")
    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"

    assert experiment_cards, "实验阶段应已执行"
    card = experiment_cards[0]
    assert "adopt_cleaned" in card, "G2 决策的选项 id 应进实验任务卡"
    assert "用户已确认采用清洗后的数据" in card
    assert "- cleaned/orders.csv" in card, "清洗产物目录应进入工作区数据文件清单"


def _g3_router(passed_flags: tuple[bool, ...], outline_prompts: list[str]):
    """稳健性复跑按给定通过标志出结果；总编规划的提示词收集起来供断言材料。"""

    def router(request: httpx.Request) -> httpx.Response:
        messages = _wire_messages(request)
        system = _system_of(messages)
        if "稳健性检验工程师" in system:
            return _sandbox_reply(
                messages,
                robustness_code(*passed_flags),
                {"summary": "按阈值逐项判定完毕"},
            )
        if "论文的总编" in messages[-1]["content"]:
            outline_prompts.append(messages[-1]["content"])
        return _stage_router(request)

    return router


def test_g3_result_gate_accept_with_limitations_reaches_paper(client, monkeypatch, validate_contract):
    """G3 结果采用闸门端到端（§9.1/§11.1）：验证阶段沙盒复跑三项检查中一项未
    通过 → 运行停在 generic 决策卡（接受并记录局限 / 重做实验 / 回退方案，少数
    未过推荐接受、CTA 预选）→ 用户接受 → 决策进台账 → 论文材料带稳健性数字与
    「不得淡化」纪律 → 完成。"""
    project = create_project(client)
    outline_prompts: list[str] = []
    _configure_llm(client, monkeypatch, handler=_g3_router((True, False, True), outline_prompts))
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approve_when_asked(client, run["id"], option_id="approve")  # G1

    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    validate_contract("approval-request.schema.json", gate)
    assert gate["decision_type"] == "generic"
    assert "3 项中 1 项未通过" in gate["title"] and "重采样稳定性" in gate["title"]
    options = {option["id"]: option for option in gate["options"]}
    assert list(options) == ["accept_with_limitations", "redo:EXPERIMENTING", "redo:MODEL_PLANNING"]
    assert options["accept_with_limitations"].get("recommended") is True
    assert not options["redo:EXPERIMENTING"].get("recommended")

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    statuses = {step["node"]: step["status"] for step in steps}
    assert statuses["VALIDATING"] == "SUCCEEDED", "闸门未拍板前检验步骤已成功落库"
    run_view = client.get(f"/api/v1/task-runs/{run['id']}").json()
    assert run_view["status"] == "WAITING_APPROVAL"

    workspace = client.get(f"/api/v1/task-runs/{run['id']}/workspace").json()
    validate_contract("modeling-workspace-view.schema.json", workspace)
    action = workspace["agent"]["action"]
    assert action["kind"] == "approve"
    assert action["approval_id"] == gate["id"]
    assert action["option_id"] == "accept_with_limitations"

    resolved = client.post(
        f"/api/v1/task-runs/{run['id']}/actions",
        json={"action": "approve", "approval_id": gate["id"], "option_id": "accept_with_limitations"},
    )
    assert resolved.status_code == 200, resolved.text
    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    assert [step["node"] for step in steps if step["node"] == "EXPERIMENTING"] == ["EXPERIMENTING"], (
        "接受局限不重做实验"
    )
    assert outline_prompts, "论文总编规划应已执行"
    material = outline_prompts[0]
    assert "通过 2 项" in material and "bootstrap" in material, "稳健性数字进论文材料"
    assert "接受并记录局限" in material and "不得淡化" in material
    # 未通过项的实测值进数字冻结清单（总编 prompt 的「数字冻结清单」段）：论文只能原样引用
    assert "robustness.bootstrap" in material and "稳健性检查「" in material
    # 假设检验覆盖（切片 3）：方案 A 的重点验证假设 A1 没有检查覆盖 → 点明进局限性；
    # 全局待检验假设 G2 由 sensitivity 检查覆盖且通过。重点验证排前
    assert (
        "模型假设检验：A1「预算约束为硬约束」未被检验覆盖，须在局限性中说明；"
        "G2「调度点之间需求独立」通过（sensitivity）。"
    ) in material
    with client.app.state.db.session_factory() as session:
        row = session.get(ApprovalRequestRow, gate["id"])
        assert row.evidence["gate"] == "G3"
        coverage = row.evidence["impact"]["assumption_coverage"]
        assert [(entry["id"], entry["check_ids"], entry["passed"]) for entry in coverage] == [
            ("A1", [], None),
            ("G2", ["sensitivity"], True),
        ]

    artifacts = client.get(f"/api/v1/projects/{project['id']}/artifacts").json()["items"]
    by_file = {a["uri"].rstrip("/").rsplit("/", 1)[-1]: a for a in artifacts}
    assert by_file["validation_checks.py"]["kind"] == "code", "检验脚本发布为可复现的 code 产物"


def test_g3_redo_experiment_reruns_downstream_and_gates_again(client, monkeypatch):
    """G3 选「重做实验」：复用修订门的回退语义——实验、检验各跑第二趟（attempt 2），
    同一份失败脚本让 G3 再次弹出，接受后完成；期间没有 RUN_RETRIED，运行状态
    在回退时先摆回实验阶段。"""
    project = create_project(client)
    _configure_llm(client, monkeypatch, handler=_g3_router((False, False, True), []))
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approve_when_asked(client, run["id"], option_id="approve")  # G1

    gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert "3 项中 2 项未通过" in gate["title"]
    options = {option["id"]: option for option in gate["options"]}
    assert options["redo:EXPERIMENTING"].get("recommended") is True, "多数未过推荐重做实验"

    resolved = client.post(
        f"/api/v1/task-runs/{run['id']}/actions",
        json={"action": "approve", "approval_id": gate["id"], "option_id": "redo:EXPERIMENTING"},
    )
    assert resolved.status_code == 200, resolved.text
    run_view = client.get(f"/api/v1/task-runs/{run['id']}").json()
    assert run_view["status"] == "RUNNING"
    assert run_view["current_node"] == "EXPERIMENTING", "回退时 current_node 先摆回目标阶段"

    second_gate = wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert second_gate["id"] != gate["id"]
    assert "3 项中 2 项未通过" in second_gate["title"]
    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    attempts = {}
    for step in steps:
        attempts.setdefault(step["node"], []).append(step["attempt"])
    assert attempts["EXPERIMENTING"] == [1, 2]
    assert attempts["VALIDATING"] == [1, 2]
    assert attempts["MODEL_PLANNING"] == [1], "回退起点的上游不重做"

    approve_when_asked(client, run["id"], option_id="accept_with_limitations")
    confirm_delivery(client, run["id"])
    final = wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    assert final["status"] == "COMPLETED"

    events = client.get(f"/api/v1/task-runs/{run['id']}/events/history").json()["items"]
    types = [event["type"] for event in events]
    assert types.count("approval.requested") == 4, "G1 + G3 ×2 + G4"
    statuses = [
        event["payload"] for event in events if event["type"] == "run.status_changed"
    ]
    assert any("从「实验运行」重做" in str(entry.get("reason") or "") for entry in statuses), (
        "回退的状态原因要说清是回退而不是「已确认」"
    )
