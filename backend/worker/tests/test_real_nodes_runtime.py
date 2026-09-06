"""六阶段真实节点在 worker 运行时里的全链验证。

LLM 用 StubLlmPort 桩（输出形状对齐 agents/skills/tests/test_nodes.py 与
backend/api/tests/test_task_runs_llm_nodes.py 的六阶段常量），python 沙箱是
真实子进程执行（tmp_path 工作区）。覆盖的执行面不变量：

- 全链完成：三阶段 → 审批门（LLM 节点文案）→ 批准 → 实验/检验/论文，产物
  （实验 csv、论文 md）真实落盘且经事件日志可追溯；
- 实验代码运行时失败一轮后，第二轮生成携带错误反馈与上一版代码并成功收尾；
- 崩溃续跑：跑一半的事件日志换一个全新 runtime 实例，重放 + heal 后按
  attempt+1 续跑到完成，已完成阶段不重复执行；
- 审批语义：批准续跑下一阶段；拒绝退回重做规划并再次请求确认；
- 方案阶段三路提议人在会话里经 ToolBus 自主检索知识库（knowledge_search 留痕）。
"""

import json
import re
from pathlib import Path

import pytest
from omm_agent_core import (
    WORK_SEQUENCE,
    AdvanceOutcome,
    EventType,
    StepStatus,
    TaskState,
)
from omm_agent_skills import (
    CLEANING_PROMPT_ID,
    EXPERIMENT_SCRIPT_PATH,
    PROPOSER_PROMPT_ID,
    PYTHON_TOOL_NAME,
    ExperimentExecutionNode,
    PromptRegistry,
    StubLlmPort,
    ValidationNode,
    stub_response,
)
from omm_worker import WorkerConfig, WorkerLoop, build_real_nodes, create_real_runtime

# -- 六阶段 stub 输出（形状与 skills / api 参考测试一致） ----------------------

ANALYSIS_OK = {
    "viability": "ok",
    "missing_info": [],
    "title": "共享单车调度优化",
    "problem_type": "优化",
    "objectives": ["给出调度方案"],
    "constraints": ["车辆容量有限"],
    "data_requirements": ["历史订单数据"],
    "key_assumptions": ["需求平稳"],
    "subquestions": [{"id": "q1", "text": "给出调度方案", "depends_on": []}],
}

PREPARATION_OK = {
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

PLANNING_OK = {
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

#: 方案阶段（H3）：三视角提议人各回一案，归约桩把它们收成 PLANNING_OK 的 A/B
#: （多出 role / source_views 两个归约字段；投影只取契约五键，下游断言不漂移）。
PROPOSAL_BY_VIEW = {
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


def proposer_reply(variables):
    return stub_response(PROPOSAL_BY_VIEW[variables["view_name"]])


def _proposer_view(messages):
    """提议人会话的 system 消息是渲染后的角色卡：视角名在开头那句里。"""
    match = re.search(r"「(.+?)」方案提议人", messages[0]["content"])
    assert match, "提议人会话应以模板渲染的角色卡开场"
    return match.group(1)


PROPOSER_SEARCH_QUERY = "共享单车 调度 整数规划"


def proposer_chat(messages):
    """一路提议人会话（§10.3 切片二）：先经 ToolBus 查一次知识库，观察回来再交终答。

    按会话内容判断（看到过工具观察就终答），三路并行共用这一份脚本。
    """
    if _saw_observation(messages):
        return stub_response(PROPOSAL_BY_VIEW[_proposer_view(messages)])
    return tool_envelope("knowledge_search", query=PROPOSER_SEARCH_QUERY, kind="problem")


REDUCE_OK = {
    **PLANNING_OK,
    "plans": [
        {
            **PLANNING_OK["plans"][0],
            "role": "primary",
            "source_views": ["operations_research"],
        },
        {
            **PLANNING_OK["plans"][1],
            "role": "baseline",
            "source_views": ["mechanism", "data_driven"],
        },
    ],
    "dropped": [],
    "progress_note": "三路提议归约为两案，推荐整数规划。",
}

#: 归约之后的规范化（H3 切片 2）：假设表 + 符号表随方案卡进 G1。
FORMALIZE_OK = {
    "assumptions": [
        {"id": "G1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面", "impact": "medium", "status": "confirmed"},
        {"id": "A1", "text": "预算约束为硬约束", "scope": "A", "basis": "题面", "impact": "high", "status": "critical"},
    ],
    "symbols": [
        {"symbol": "i \\in \\mathcal{I}", "kind": "set", "definition": "调度点索引", "unit": None, "range": None, "plan_id": None},
        {"symbol": "x_i", "kind": "variable", "definition": "调度点 i 是否设站", "unit": None, "range": "{0,1}", "plan_id": "A"},
    ],
}

#: 沙箱真实执行的实验脚本：写产物文件并打印指标行。
#: newline='' 禁止平台换行转换，产物字节在 Windows 与 POSIX 上一致。
EXPERIMENT_CODE = (
    "import json\n"
    "with open('results.csv', 'w', encoding='utf-8', newline='') as fh:\n"
    "    fh.write('metric,value\\nrmse,0.5\\n')\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'rmse': 0.5}))\n"
)

#: 沙盒执行体的终答（代码经 python_run 信封提交，终答只报告叙事字段）。
EXPERIMENT_OK = {
    "summary": "贪心近似跑通，rmse 0.5 达标并写出结果表",
    "approach_summary": "构造合成需求数据，贪心近似求解并与随机基线对比",
    "progress_note": "实验代码已跑通，核心指标 rmse=0.5，下一步进入结果检验。",
}

#: 独立审稿人（§8.4 生成者-评审者）的终答：接受，留一条 minor 意见。
REVIEW_OK = {
    "verdict": "accept",
    "findings": [
        {
            "id": "R1",
            "severity": "minor",
            "location": "metrics",
            "issue": "只报了 rmse，未与随机基线的 rmse 并列",
            "fix_hint": "论文阶段并列基线指标",
        }
    ],
    "summary": "实现忠实于方案 A，同种子复跑一致，可作为检验与论文依据。",
}

VALIDATION_OK = {
    "verdict": "concerns",
    "checks": [
        {"name": "结果合理性", "result": "pass", "note": "指标数量级正常"},
        {"name": "稳健性", "result": "warn", "note": "对需求率参数敏感"},
    ],
    "risks": ["合成数据外推风险"],
    "validation_summary": "结果整体可信，但对需求率参数敏感",
}

#: 稳健性复跑的检验脚本（沙箱真实执行）：两项检查全过，只打印标记行。第一项回指
#: FORMALIZE_OK 里方案 A 的重点验证假设 A1（方案阶段有须检验的假设时，验证节点
#: 要求至少一项检查带 assumption_id）。
ROBUSTNESS_CODE = (
    "import json\n"
    "checks = [\n"
    "    {'id': 'sensitivity', 'name': '参数扰动', 'passed': True, 'value': 0.05, 'threshold': 0.2, 'assumption_id': 'A1'},\n"
    "    {'id': 'bootstrap', 'name': '重采样稳定性', 'passed': True, 'value': 0.08, 'threshold': 0.15},\n"
    "]\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'checks': checks}))\n"
)

ROBUSTNESS_OK = {"summary": "两项稳健性检查均在阈值内"}

PAPER_OK = {
    "title": "基于整数规划的共享单车调度优化",
    "abstract": "本文建立整数规划模型求解调度问题……",
    "keywords": ["整数规划", "调度"],
    "sections": [
        {"heading": "问题重述", "content": "题目要求……"},
        {"heading": "模型检验", "content": "结果对需求率参数敏感，结论需谨慎外推。"},
    ],
}


def stage_responses(**overrides):
    responses = {
        "problem_analysis.default": stub_response(ANALYSIS_OK),
        "data_preparation.default": stub_response(PREPARATION_OK),
        # 方案阶段走三路提议 + 归约 + 规范化；default 只在无监督者时才会被消费（worker 有）
        "model_planning.default": stub_response(PLANNING_OK),
        # 提议人在 worker 装配里走会话（知识库 + ToolBus + 会话端口三件齐，见
        # stage_chat_scripts）；这里的单发桩只是哨兵：被消费就说明装配退回了单次路径
        "model_planning.proposer": "不应走单次调用路径",
        "model_planning.reduce": stub_response(REDUCE_OK),
        "model_planning.formalize": stub_response(FORMALIZE_OK),
        "validating.default": stub_response(VALIDATION_OK),
        # 论文阶段是分章多轮管线：总编规划（paper_outline）在本桩给非法输出，
        # 节点走「总编失败回退整篇单次生成」路径消费 paper_writing 桩——worker
        # 套件因此锚定回退路径，完整分章路径由 backend/api 的 e2e 桩锚定，互补。
        "paper_outline.default": "（本环境无分章规划桩，验证回退整篇生成路径）",
        "paper_writing.default": stub_response(PAPER_OK),
    }
    responses.update(overrides)
    return responses


def tool_envelope(name, **arguments):
    """模型侧的工具信封文本（chat_adapter 的文本协议）。"""
    return json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)


def _saw_observation(messages):
    return any("[工具执行结果]" in message["content"] for message in messages)


def sandbox_script(final, code=EXPERIMENT_CODE):
    """一波会话：先发 python_run 信封，收到观察后给终答。

    按会话内容而非调用序号判断，同一份脚本可服务多波修复（每波观察清零）。
    """

    def reply(messages):
        if _saw_observation(messages):
            return stub_response(final)
        return tool_envelope(PYTHON_TOOL_NAME, code=code)

    return [reply]


def stage_chat_scripts(**overrides):
    """沙盒三节点的会话脚本。worker 运行不下发附件数据（data/ 为空），
    清洗如实跳过，清洗脚本仅作兜底不应被消费；实验、实验审稿与稳健性复跑真的走到。"""
    scripts = {
        CLEANING_PROMPT_ID: sandbox_script({"summary": "无数据文件，不应走到这里"}),
        # 方案阶段三路提议人：每路一次 knowledge_search（经 ToolBus 留痕）+ 终答
        PROPOSER_PROMPT_ID: [proposer_chat],
        ExperimentExecutionNode.prompt_id: sandbox_script(EXPERIMENT_OK),
        # 实验审稿人（独立上下文的 reviewer 子代理）：不查工具，直接给接受结论
        ExperimentExecutionNode.review_prompt_id: [stub_response(REVIEW_OK)],
        ValidationNode.sandbox_prompt_id: sandbox_script(ROBUSTNESS_OK, code=ROBUSTNESS_CODE),
    }
    scripts.update(overrides)
    return scripts


def stage_llm(chat_overrides=None, **overrides):
    return StubLlmPort(
        stage_responses(**overrides),
        chat_scripts=stage_chat_scripts(**(chat_overrides or {})),
    )


def make_runtime(tmp_path, llm, unattended=False, worker_id="worker_real", **config_kwargs):
    config = WorkerConfig(root=tmp_path / "rt", **config_kwargs)
    return create_real_runtime(config, llm, unattended=unattended, worker_id=worker_id)


def drain(loop, limit=10):
    outcomes = []
    for _ in range(limit):
        outcome = loop.tick()
        if outcome is None:
            break
        outcomes.append(outcome)
    return outcomes


def steps_for(snapshot, state):
    return [step for step in snapshot.steps if step.state is state]


def prompt_calls(llm, prompt_id):
    return [call for call in llm.calls if call.prompt_id == prompt_id]


def chat_calls(llm, label):
    return [call for call in llm.chat_calls if call.label == label]


# -- 装配 ---------------------------------------------------------------------


def test_build_real_nodes_requires_complete_prompt_set():
    with pytest.raises(ValueError, match=r"experiment_code\.sandbox"):
        build_real_nodes(prompts=PromptRegistry())  # 空注册表：缺全部模板


def test_build_real_nodes_covers_all_work_states():
    nodes = build_real_nodes()
    assert set(nodes) == set(WORK_SEQUENCE)


def test_build_real_nodes_wires_the_knowledge_library_into_planning():
    from omm_agent_core import KnowledgePort
    from omm_agent_tools import KnowledgeLibrary, load_knowledge_library

    planning = build_real_nodes()[TaskState.MODEL_PLANNING]
    # 缺省 = 进程内缓存的快照库（仓内快照存在时可用；部署包没带快照就是空库，
    # 装配照常——知识库是材料不是闸门）
    assert isinstance(planning.knowledge, KnowledgePort)
    assert planning.knowledge is load_knowledge_library()

    injected = KnowledgeLibrary.empty("test")
    assert build_real_nodes(knowledge=injected)[TaskState.MODEL_PLANNING].knowledge is injected


def test_real_runtime_puts_the_same_knowledge_library_on_the_tool_bus(tmp_path):
    """节点预检索与提议人会话里的工具检索必须读同一个库：create_real_runtime 把
    同一个知识库对象同时喂给方案节点与运行时，_open_run 把两个只读工具注册进
    ToolBus 的允许清单（execute 封顶下 readonly 工具可调）。"""
    from omm_agent_tools import KnowledgeLibrary

    injected = KnowledgeLibrary.empty("知识库快照不存在：测试注入")
    runtime = create_real_runtime(
        WorkerConfig(root=tmp_path / "rt"), stage_llm(), worker_id="worker_kb", knowledge=injected
    )
    assert runtime.knowledge is injected
    assert runtime._nodes[TaskState.MODEL_PLANNING].knowledge is injected

    # 缺省装配：运行时与节点拿到同一份进程缓存实例
    default_runtime = make_runtime(tmp_path / "default", stage_llm())
    assert default_runtime.knowledge is default_runtime._nodes[TaskState.MODEL_PLANNING].knowledge

    run_id = runtime.create_run("proj_1", inputs={"goal": "优化共享单车调度"})
    engine, snapshot = runtime._open_run(run_id)
    tools = engine._services.tools
    searched = tools.invoke(run_id, "step_probe", "knowledge_search", {"query": "共享单车"})
    assert searched.ok and searched.output["hits"] == []
    assert "测试注入" in searched.output["note"], "空库如实报原因，不算失败"
    missing = tools.invoke(run_id, "step_probe", "knowledge_read", {"card_id": "problem:none"})
    assert not missing.ok and "未找到卡片" in missing.error
    # 两次调用都经 record_external 留 TOOL_CALLED 痕
    tool_names = [
        e.payload["tool"] for e in runtime.events.load(run_id) if e.event_type is EventType.TOOL_CALLED
    ]
    assert tool_names == ["knowledge_search", "knowledge_read"]


# -- 全链 + 审批批准 ------------------------------------------------------------


def test_full_chain_review_gate_then_approval_completes(tmp_path):
    llm = stage_llm()
    runtime = make_runtime(tmp_path, llm)
    loop = WorkerLoop(runtime)

    run_id = runtime.create_run(
        "proj_1", inputs={"goal": "优化共享单车调度", "params": {}}
    )
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]

    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.NEEDS_REVIEW
    # 审批文案来自 LLM 规划节点（而非模拟节点），审批门确实由真实节点触发；
    # 归约后的推荐 / 备选案点名进文案（G1 三选，H3）
    assert snapshot.review.reason == "请确认建模方案：推荐 A「整数规划」；备选 B「启发式」"
    assert snapshot.review.resume_state is TaskState.MODEL_PLANNING
    # goal → problem_statement 的装配映射真实生效
    first = llm.calls[0]
    assert first.prompt_id == "problem_analysis.default"
    assert first.variables["problem_statement"] == "优化共享单车调度"

    state_after = runtime.apply_action(run_id, "approve", reason="方案可行")
    assert state_after == TaskState.MODEL_PLANNING.value
    # 论文发布后停在 G4 定稿闸门（必停）：草稿已落库，等人确认交付
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]
    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.review.resume_state is TaskState.PAPER_WRITING
    assert "论文草稿已生成" in snapshot.review.reason
    assert snapshot.outputs["PAPER_WRITING"]["audit_findings"] == []
    state_after = runtime.apply_action(run_id, "approve", reason="confirm_delivery")
    assert state_after == TaskState.PAPER_WRITING.value
    assert drain(loop) == [AdvanceOutcome.COMPLETED]

    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.COMPLETED
    assert snapshot.review_decisions["PAPER_WRITING"] == "confirm_delivery"
    for state in WORK_SEQUENCE:
        assert [step.status for step in steps_for(snapshot, state)] == [
            StepStatus.SUCCEEDED
        ], f"{state.value} 应恰好成功一次"

    # 阶段产出沿 prior_outputs 贯通：分析标题、实验指标进入快照
    assert snapshot.outputs["PROBLEM_ANALYSIS"]["title"] == ANALYSIS_OK["title"]
    assert snapshot.outputs["EXPERIMENTING"]["metrics"] == {"rmse": 0.5}

    # 实验代码由真实沙箱执行：results.csv 被捕获为产物且字节精确（newline=''）
    (experiment_step,) = steps_for(snapshot, TaskState.EXPERIMENTING)
    by_name = {Path(ref.uri).name: ref for ref in experiment_step.artifacts}
    assert "results.csv" in by_name
    assert by_name["results.csv"].kind == "table"
    assert Path(by_name["results.csv"].uri).read_bytes() == b"metric,value\nrmse,0.5\n"

    # 论文节点发布 markdown 产物
    (paper_step,) = steps_for(snapshot, TaskState.PAPER_WRITING)
    assert [ref.kind for ref in paper_step.artifacts] == ["paper"]
    paper_text = Path(paper_step.artifacts[0].uri).read_text(encoding="utf-8")
    assert "# 基于整数规划的共享单车调度优化" in paper_text
    assert "## 模型检验" in paper_text

    # 验证阶段真的在沙箱里复跑了检验脚本：判定数字来自标记行，全过不上 G3
    robustness = snapshot.outputs["VALIDATING"]["robustness"]
    assert robustness["executed"] is True and robustness["status"] == "passed"
    assert [check["id"] for check in robustness["checks"]] == ["sensitivity", "bootstrap"]
    assert "VALIDATING" not in snapshot.review_decisions
    # 假设表下游消费（切片 3）：检查回指方案 A 的重点验证假设 A1，覆盖表随产出落库；
    # 已确认的 G1 不在须检验之列
    assert [check["assumption_id"] for check in robustness["checks"]] == ["A1", None]
    assert [(row["id"], row["check_ids"], row["passed"]) for row in robustness["assumption_coverage"]] == [
        ("A1", ["sensitivity"], True),
    ]
    assert robustness["uncovered_focus"] == []
    # 方案阶段两张表进下游材料（切片 4）：实验任务卡带所选方案 A 的符号（共享 + A）；
    # 回退整篇生成的论文调用同样拿到假设表与符号表
    experiment_chat = next(c for c in llm.chat_calls if c.label == ExperimentExecutionNode.prompt_id)
    experiment_card = next(m["content"] for m in experiment_chat.messages if m["role"] == "system")
    assert "## 模型符号" in experiment_card
    assert "- x_i（决策变量｜方案 A）＝调度点 i 是否设站［取值：{0,1}］" in experiment_card
    (paper_call,) = prompt_calls(llm, "paper_writing.default")
    assert paper_call.variables["model_symbols"].splitlines() == [
        "- i \\in \\mathcal{I}（集合 / 索引｜共享）＝调度点索引",
        "- x_i（决策变量｜方案 A）＝调度点 i 是否设站［取值：{0,1}］",
    ]
    assert "- A1【重点验证｜影响高｜方案 A】预算约束为硬约束（依据：题面）" in paper_call.variables["model_assumptions"]
    # 实验最终脚本落在 run 工作区固定路径，复跑读的就是它
    workspace_script = tmp_path / "rt" / "workspaces" / run_id / EXPERIMENT_SCRIPT_PATH
    assert workspace_script.read_text(encoding="utf-8") == EXPERIMENT_CODE
    assert snapshot.outputs["EXPERIMENTING"]["script_path"] == EXPERIMENT_SCRIPT_PATH

    # 工具调用留痕（record_external → 事件日志）且事件序列无洞（先持久化再应用）
    events = runtime.events.load(run_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    tool_events = [e for e in events if e.event_type is EventType.TOOL_CALLED]
    # 方案阶段：三路 Proposer 子代理并行，spawn / result 各三条审计；三路的到达
    # 顺序随线程调度而变，只断言集合与相位，不断言先后。
    proposer_events = [
        e for e in tool_events if e.payload["tool"].startswith("subagent:proposer:")
    ]
    spawns = [e for e in proposer_events if e.payload["phase"] == "spawn"]
    results = [e for e in proposer_events if e.payload["phase"] == "result"]
    assert sorted(e.payload["tool"] for e in spawns) == [
        "subagent:proposer:data_driven",
        "subagent:proposer:mechanism",
        "subagent:proposer:operations_research",
    ]
    assert [e.payload["envelope_status"] for e in results] == ["done", "done", "done"]
    assert {e.payload["tool_tier"] for e in spawns} == {"readonly"}
    # 提议人子代理拿到知识库两个只读工具（spawn 审计可对账），并且真的用了：
    # 三路各一次 knowledge_search 经 ToolBus 留痕，挂在方案步骤上、成功返回
    # （仓内快照在则有命中，没快照就是「知识库不可用」的空命中——都是 succeeded）
    assert {tuple(e.payload["toolset"]) for e in spawns} == {("knowledge_search", "knowledge_read")}
    (planning_step,) = steps_for(snapshot, TaskState.MODEL_PLANNING)
    knowledge_events = [e for e in tool_events if e.payload["tool"] == "knowledge_search"]
    assert len(knowledge_events) == 3
    assert {e.payload["status"] for e in knowledge_events} == {"succeeded"}
    assert {e.payload["step_id"] for e in knowledge_events} == {planning_step.step_id}
    assert all(PROPOSER_SEARCH_QUERY in e.payload["input_summary"] for e in knowledge_events)
    # 提议人的会话形状：system = 渲染后的角色卡（预检索材料段仍在），user = 工具协议
    proposer_chats = chat_calls(llm, PROPOSER_PROMPT_ID)
    assert len(proposer_chats) == 6, "每路：检索信封 + 终答"
    opening = next(call for call in proposer_chats if not _saw_observation(call.messages))
    assert [m["role"] for m in opening.messages] == ["system", "user"]
    assert "## 相似赛题与获奖论文方法（知识库检索，按相关度）" in opening.messages[0]["content"]
    assert "- knowledge_search：" in opening.messages[1]["content"]
    assert prompt_calls(llm, PROPOSER_PROMPT_ID) == [], "提议人不再走单次调用路径"
    assert snapshot.outputs["MODEL_PLANNING"]["plans"][1]["role"] == "baseline"
    tool_events = [e for e in tool_events if e not in proposer_events and e not in knowledge_events]
    # 数据阶段画像前置一条 ws_list（无数据文件，清洗如实跳过）；实验阶段依次：
    # 任务卡数据清单 ws_list → 节点侧 env_probe（复现指纹）→ 沙盒 python_run
    # → 断言取证 ws_list（验收基于工作区证据而非模型自述）→ 确定性复跑 python_run
    # （§8.4 生成者-评审者：同一份脚本再跑一次给审稿人核对）→ 审稿任务卡的工作区
    # 清单 ws_list → reviewer 子代理 spawn / result 审计 → ws_write 落最终脚本；
    # 验证阶段：ws_list（脚本在场）→ ws_read（脚本正文进任务卡）→ env_probe →
    # 监督者 spawn 审计 → 沙盒 python_run → 断言取证 ws_list → 监督者 result 审计。
    assert [e.payload["tool"] for e in tool_events] == [
        "ws_list",
        "ws_list",
        "env_probe",
        "python_run",
        "ws_list",
        "python_run",
        "ws_list",
        "subagent:reviewer",
        "subagent:reviewer",
        "ws_write",
        "ws_list",
        "ws_read",
        "env_probe",
        "subagent:sandbox",
        "python_run",
        "ws_list",
        "subagent:sandbox",
    ]
    experiment_event, rerun_event, robustness_event = [
        e for e in tool_events if e.payload["tool"] == "python_run"
    ]
    assert experiment_event.payload["status"] == "succeeded"
    assert experiment_event.payload["artifact_ids"], "沙箱捕获的产物要进工具事件"
    assert experiment_event.payload["step_id"] == experiment_step.step_id
    assert rerun_event.payload["status"] == "succeeded"
    assert rerun_event.payload["step_id"] == experiment_step.step_id
    assert robustness_event.payload["status"] == "succeeded"
    (validation_step,) = steps_for(snapshot, TaskState.VALIDATING)
    assert robustness_event.payload["step_id"] == validation_step.step_id
    spawn_event, result_event = [
        e for e in tool_events if e.payload["tool"] == "subagent:sandbox"
    ]
    assert spawn_event.payload["phase"] == "spawn"
    assert result_event.payload["phase"] == "result"
    assert result_event.payload["envelope_status"] == "done"
    # 实验审稿人：只读 tier、只拿工作区两个只读工具 + 知识库两个只读工具（worker
    # 装配把方案提议人共用的知识库端口也接给了实验节点），一轮接受、复跑一致
    review_spawn, review_result = [
        e for e in tool_events if e.payload["tool"] == "subagent:reviewer"
    ]
    assert review_spawn.payload["phase"] == "spawn"
    assert review_spawn.payload["tool_tier"] == "readonly"
    assert review_spawn.payload["toolset"] == ["ws_read", "ws_list", "knowledge_search", "knowledge_read"]
    assert review_result.payload["envelope_status"] == "done"
    review = snapshot.outputs["EXPERIMENTING"]["review"]
    assert review["executed"] is True and review["verdict"] == "accept"
    assert review["rounds"] == 1 and review["stalemate"] is False
    assert review["rerun"]["consistent"] is True and review["rerun"]["metrics"] == {"rmse": 0.5}
    assert experiment_step.metrics["review_rounds"] == 1
    (review_chat,) = chat_calls(llm, ExperimentExecutionNode.review_prompt_id)
    review_card = next(m["content"] for m in review_chat.messages if m["role"] == "system")
    assert "实验审稿人" in review_card and EXPERIMENT_CODE in review_card
    assert "核心指标与首跑逐键一致" in review_card
    assert "- knowledge_search：" in next(m["content"] for m in review_chat.messages if m["role"] == "user")


def test_unattended_mode_completes_without_review(tmp_path):
    llm = stage_llm()
    runtime = make_runtime(tmp_path, llm, unattended=True)

    run_id = runtime.create_run("proj_1", inputs={"goal": "优化共享单车调度"})
    assert drain(WorkerLoop(runtime)) == [AdvanceOutcome.COMPLETED]

    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.COMPLETED
    events = runtime.events.load(run_id)
    assert not any(e.event_type is EventType.REVIEW_REQUESTED for e in events)
    # 规划产出仍然完整进入快照，供实验阶段选方案
    assert snapshot.outputs["MODEL_PLANNING"]["recommended_plan_id"] == "A"


# -- 实验失败一轮后带反馈重生成 ---------------------------------------------------


def test_experiment_runtime_failure_regenerates_with_feedback(tmp_path):
    bad_code = "raise RuntimeError('bad seed')"
    envelopes_sent = []

    def experiment_reply(messages):
        if _saw_observation(messages):
            return stub_response(EXPERIMENT_OK)
        code = bad_code if not envelopes_sent else EXPERIMENT_CODE
        envelopes_sent.append(code)
        return tool_envelope(PYTHON_TOOL_NAME, code=code)

    llm = stage_llm(
        chat_overrides={ExperimentExecutionNode.prompt_id: [experiment_reply]}
    )
    runtime = make_runtime(tmp_path, llm, unattended=True)

    run_id = runtime.create_run("proj_1", inputs={"goal": "优化共享单车调度"})
    assert drain(WorkerLoop(runtime)) == [AdvanceOutcome.COMPLETED]

    # 第二波会话的开场用户消息必须携带第一波的运行时错误与上一版代码
    assert envelopes_sent == [bad_code, EXPERIMENT_CODE]
    experiment_chats = [
        call
        for call in llm.chat_calls
        if call.label == ExperimentExecutionNode.prompt_id
    ]
    second_wave_open = next(
        m["content"]
        for m in experiment_chats[2].messages
        if m["role"] == "user"
    )
    assert "bad seed" in second_wave_open
    assert bad_code in second_wave_open

    snapshot = runtime.get_snapshot(run_id)
    (experiment_step,) = steps_for(snapshot, TaskState.EXPERIMENTING)
    assert experiment_step.status is StepStatus.SUCCEEDED
    assert experiment_step.metrics["waves"] == 2, "节点内按波自愈，不产生额外步骤尝试"
    assert experiment_step.metrics["code_rounds"] == 2

    # 两轮沙箱执行都留 TOOL_CALLED 痕：先失败后成功，随后是审稿前对通过版脚本的
    # 确定性复跑（画像前置的 ws_list 与验证阶段自己那次稳健性复跑的 python_run
    # 不在其中——按实验步骤过滤）
    tool_events = [
        e
        for e in runtime.events.load(run_id)
        if e.event_type is EventType.TOOL_CALLED
        and e.payload["tool"] == "python_run"
        and e.payload["step_id"] == experiment_step.step_id
    ]
    assert [e.payload["status"] for e in tool_events] == ["failed", "succeeded", "succeeded"]
    assert snapshot.outputs["EXPERIMENTING"]["review"]["verdict"] == "accept"


# -- 崩溃续跑 -------------------------------------------------------------------


def test_crash_midway_fresh_runtime_resumes_to_completion(tmp_path):
    validating_calls = []

    def validating_reply(variables):
        validating_calls.append(dict(variables))
        if len(validating_calls) == 1:
            # SystemExit 绕过引擎的 Exception 兜底，模拟执行进程在
            # STEP_STARTED 持久化之后、成败落定之前死掉。
            raise SystemExit("simulated worker death")
        return stub_response(VALIDATION_OK)

    llm = stage_llm(**{"validating.default": validating_reply})
    config = WorkerConfig(root=tmp_path / "rt")

    runtime1 = create_real_runtime(config, llm, unattended=True, worker_id="worker_a")
    run_id = runtime1.create_run("proj_1", inputs={"goal": "优化共享单车调度"})
    with pytest.raises(SystemExit):
        drain(WorkerLoop(runtime1))

    # 掉电现场：检验步骤悬挂在 RUNNING（STEP_STARTED 已持久化，无成败事件）
    mid = runtime1.get_snapshot(run_id)
    assert mid.state is TaskState.VALIDATING
    assert [s.status for s in steps_for(mid, TaskState.VALIDATING)] == [
        StepStatus.RUNNING
    ]

    # 全新 runtime 实例（同一事件日志根，无任何内存继承）：重放恢复快照，
    # heal_interrupted 把悬挂步骤落为 FAILED，再按 attempt+1 续跑到完成。
    runtime2 = create_real_runtime(config, llm, unattended=True, worker_id="worker_b")
    runtime2.queue.enqueue(run_id, kind="advance")
    assert drain(WorkerLoop(runtime2)) == [AdvanceOutcome.COMPLETED]

    final = runtime2.get_snapshot(run_id)
    assert final.state is TaskState.COMPLETED
    validating_steps = steps_for(final, TaskState.VALIDATING)
    assert [(s.attempt, s.status) for s in validating_steps] == [
        (1, StepStatus.FAILED),
        (2, StepStatus.SUCCEEDED),
    ]
    assert "interrupted" in validating_steps[0].error

    # 已完成阶段不重复执行：除检验外各阶段的 LLM 只被调用一次
    for prompt_id, expected in {
        "problem_analysis.default": 1,
        "data_preparation.default": 1,
        "model_planning.default": 0,
        "model_planning.proposer": 0,
        "model_planning.reduce": 1,
        "model_planning.formalize": 1,
        "validating.default": 2,
        "paper_writing.default": 1,
    }.items():
        assert len(prompt_calls(llm, prompt_id)) == expected, prompt_id
    # 提议人会话（三路 × 信封 + 终答）与实验沙盒会话同样不重跑：续跑后无新增
    assert len(chat_calls(llm, PROPOSER_PROMPT_ID)) == 6
    experiment_chats = [
        call
        for call in llm.chat_calls
        if call.label == ExperimentExecutionNode.prompt_id
    ]
    assert len(experiment_chats) == 2

    # 续跑追加的事件仍然无洞（幂等去重 + 单调序列）
    seqs = [e.seq for e in runtime2.events.load(run_id)]
    assert seqs == list(range(1, len(seqs) + 1))


# -- 审批拒绝：退回重做 -----------------------------------------------------------


def test_reject_redoes_planning_and_asks_again(tmp_path):
    llm = stage_llm()
    runtime = make_runtime(tmp_path, llm)
    loop = WorkerLoop(runtime)

    run_id = runtime.create_run("proj_1", inputs={"goal": "优化共享单车调度"})
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]

    state_after = runtime.apply_action(run_id, "reject", reason="方案不满足预算约束")
    assert state_after == TaskState.MODEL_PLANNING.value, "拒绝后立即回到规划态待重跑"

    # 重做规划并再次请求确认
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]
    snapshot = runtime.get_snapshot(run_id)
    assert snapshot.state is TaskState.NEEDS_REVIEW
    planning_steps = steps_for(snapshot, TaskState.MODEL_PLANNING)
    assert [(s.attempt, s.status) for s in planning_steps] == [
        (1, StepStatus.SUCCEEDED),
        (2, StepStatus.SUCCEEDED),
    ]
    # 重做 = 三路提议（会话：信封 + 终答）、归约与规范化整个重来一遍
    assert len(chat_calls(llm, PROPOSER_PROMPT_ID)) == 12
    assert prompt_calls(llm, PROPOSER_PROMPT_ID) == []
    assert len(prompt_calls(llm, "model_planning.reduce")) == 2
    assert len(prompt_calls(llm, "model_planning.formalize")) == 2
    assert prompt_calls(llm, "model_planning.default") == []

    # 审批事件轨迹：请求 → 拒绝 → 重做 → 再请求；拒绝原因落在事件日志里
    events = runtime.events.load(run_id)
    review_trail = [
        (e.event_type, e.payload.get("approved"))
        for e in events
        if e.event_type in (EventType.REVIEW_REQUESTED, EventType.REVIEW_RESOLVED)
    ]
    assert review_trail == [
        (EventType.REVIEW_REQUESTED, None),
        (EventType.REVIEW_RESOLVED, False),
        (EventType.REVIEW_REQUESTED, None),
    ]
    rejected = next(
        e for e in events if e.event_type is EventType.REVIEW_RESOLVED
    )
    assert rejected.payload["reason"] == "方案不满足预算约束"

    # 第二版方案获批后全链走到 G4 定稿闸门，确认交付即完成
    runtime.apply_action(run_id, "approve", reason="第二版方案可行")
    assert drain(loop) == [AdvanceOutcome.REVIEW_REQUESTED]
    assert runtime.get_snapshot(run_id).review.resume_state is TaskState.PAPER_WRITING
    runtime.apply_action(run_id, "approve", reason="confirm_delivery")
    assert drain(loop) == [AdvanceOutcome.COMPLETED]
    assert runtime.get_snapshot(run_id).state is TaskState.COMPLETED
