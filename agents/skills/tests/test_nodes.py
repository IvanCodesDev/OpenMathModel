import json

import pytest

from omm_agent_core import (
    ArtifactRef,
    FixedClock,
    InMemoryArtifactStore,
    NodeContext,
    NodeResult,
    NodeServices,
    SequentialIdGenerator,
    TaskState,
    ToolResult,
)
from omm_agent_harness import BudgetGovernor, RunBudget, SubagentSupervisor
from omm_agent_skills import (
    CLEANING_PROMPT_ID,
    DEFAULT_HARDWARE_NOTE,
    EXPERIMENT_SCRIPT_PATH,
    G3_ACCEPT_OPTION_ID,
    KNOWLEDGE_MATERIAL_CHARS,
    NO_KNOWLEDGE_HITS_NOTE,
    NO_KNOWLEDGE_NOTE,
    PYTHON_TOOL_NAME,
    ROBUSTNESS_PROMPT_ID,
    DataPreparationNode,
    LlmCall,
    ExperimentExecutionNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    ScriptedLlmPort,
    StubLlmPort,
    ValidationNode,
    allowed_number_tokens,
    assumption_material,
    assumptions_to_verify,
    build_frozen_numbers,
    chosen_plan,
    complete_notation,
    extract_json,
    gpu_hardware_note,
    knowledge_hit_ids,
    knowledge_material,
    knowledge_query,
    load_default_registry,
    missing_symbols,
    normalize_language,
    normalize_plan_languages,
    plan_assumptions,
    plan_symbols,
    render_frozen_numbers,
    render_paper_markdown,
    stub_response,
    symbol_material,
    unsourced_numbers,
)

ANALYSIS_OK = {
    "viability": "ok",
    "missing_info": [],
    "title": "门店选址优化",
    "problem_type": "优化",
    "objectives": ["确定最优布局"],
    "constraints": ["预算不超过 100 万"],
    "data_requirements": ["历史销量数据"],
    "key_assumptions": ["需求服从泊松分布"],
    "subquestions": [{"id": "q1", "text": "确定最优布局", "depends_on": []}],
}

ANALYSIS_INSUFFICIENT = {
    "viability": "insufficient",
    "missing_info": ["题目正文", "数据文件或数据说明"],
    "title": "赛题信息缺失",
    "problem_type": "未知",
    "objectives": [],
    "constraints": [],
    "data_requirements": [],
    "key_assumptions": [],
    "subquestions": [],
}

PLANNING_OK = {
    "plans": [
        {
            "id": "A",
            "name": "整数规划",
            "approach": "MILP 建模，分支定界求解",
            "steps": ["定义决策变量", "构建约束", "求解并做敏感性分析"],
            "risks": ["规模过大时求解超时"],
        },
        {
            "id": "B",
            "name": "启发式搜索",
            "approach": "模拟退火 + 局部搜索",
            "steps": ["设计邻域", "退火调度", "多次重启对比"],
            "risks": ["无法证明最优性"],
        },
    ],
    "recommended_plan_id": "A",
    "rationale": "数据规模中等，精确解可行且评审更认可",
}


def make_ctx(state, inputs=None, prior=None):
    return NodeContext(
        run_id="run_1",
        project_id="proj_1",
        state=state,
        step_id="step_1",
        attempt=1,
        inputs=inputs or {},
        prior_outputs=prior or {},
    )


def make_services(llm, tools=None):
    return NodeServices(
        clock=FixedClock(),
        ids=SequentialIdGenerator(),
        artifacts=InMemoryArtifactStore(),
        llm=llm,
        tools=tools,
    )


@pytest.fixture(scope="module")
def registry():
    return load_default_registry()


# -- extract_json ----------------------------------------------------------


def test_extract_json_plain_fenced_and_prose():
    payload = {"a": 1}
    assert extract_json(json.dumps(payload)) == payload
    assert extract_json(stub_response(payload, fenced=True)) == payload
    assert extract_json("前置说明\n" + json.dumps(payload) + "\n后置说明") == payload


def test_extract_json_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        extract_json("完全不是 JSON")


# -- ProblemAnalysisNode -----------------------------------------------------


def test_problem_analysis_happy_path(registry):
    llm = StubLlmPort(
        {"problem_analysis.default": stub_response(ANALYSIS_OK, fenced=True)}
    )
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(
        TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "题目全文……"}
    )

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["problem_type"] == "优化"
    assert result.metrics["llm_attempts"] == 1
    assert llm.calls[0].variables["attachments_summary"] == "无"


def test_problem_analysis_repairs_malformed_output_once(registry):
    llm = ScriptedLlmPort(
        {
            "problem_analysis.default": [
                "这不是 JSON",
                stub_response(ANALYSIS_OK),
            ]
        }
    )
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "题目"})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert result.metrics["llm_attempts"] == 2
    repair_call = llm.calls[1]
    assert "__repair_error" in repair_call.variables
    assert "__previous_output" in repair_call.variables


def test_problem_analysis_fails_after_two_bad_attempts(registry):
    llm = ScriptedLlmPort({"problem_analysis.default": ["bad", "still bad"]})
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "题目"})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "after 2 attempts" in result.error


def test_problem_analysis_schema_violation_counts_as_invalid(registry):
    incomplete = {"problem_type": "优化"}  # missing required arrays
    llm = ScriptedLlmPort(
        {"problem_analysis.default": [stub_response(incomplete)] }
    )
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "题目"})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required property" in result.error


def test_problem_analysis_missing_input_fails_before_llm(registry):
    llm = StubLlmPort({"problem_analysis.default": stub_response(ANALYSIS_OK)})
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


def test_problem_analysis_invalid_input_type_fails_before_llm(registry):
    llm = StubLlmPort({"problem_analysis.default": stub_response(ANALYSIS_OK)})
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": 123})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "prompt input invalid" in result.error
    assert llm.calls == []


def test_node_without_llm_port_fails_cleanly(registry):
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "题目"})
    services = make_services(llm=None)

    result = node.run(ctx, services)

    assert result.status == NodeResult.FAILED
    assert "no LLM port" in result.error


def test_problem_analysis_insufficient_input_stops_with_guidance(registry):
    """准入门：输入不构成可建模问题时第一阶段即停止，带缺失清单引导。"""
    llm = StubLlmPort(
        {"problem_analysis.default": stub_response(ANALYSIS_INSUFFICIENT)}
    )
    node = ProblemAnalysisNode(registry)
    ctx = make_ctx(TaskState.PROBLEM_ANALYSIS, inputs={"problem_statement": "你好"})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "题目信息不足" in result.error
    assert "题目正文" in result.error
    assert "数据文件或数据说明" in result.error
    assert result.metrics["viability"] == "insufficient"
    assert len(llm.calls) == 1, "判定不足后不再有任何后续模型调用"


# -- ModelPlanningNode -------------------------------------------------------


def prior_with_analysis():
    return {TaskState.PROBLEM_ANALYSIS.value: dict(ANALYSIS_OK)}


def test_model_planning_requests_review_by_default(registry):
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "确认建模方案" in result.review_reason
    assert [plan["id"] for plan in result.outputs["plans"]] == ["A", "B"]
    # The prompt received the analysis as serialized JSON.
    sent = llm.calls[0].variables["problem_analysis"]
    assert "泊松分布" in sent


def test_model_planning_can_run_unattended(registry):
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry, require_confirmation=False)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["recommended_plan_id"] == "A"


def test_model_planning_rejects_dangling_recommendation(registry):
    bad = dict(PLANNING_OK, recommended_plan_id="Z")
    llm = StubLlmPort({"model_planning.default": stub_response(bad)})
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "recommended_plan_id" in result.error


def test_model_planning_requires_prior_analysis(registry):
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior={})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


# -- ModelPlanningNode：三视角 Proposer 并行提议 → 归约 → G1 三选（H3） ---------

PROPOSALS_BY_VIEW = {
    "机理建模": {
        "name": "排队论模型",
        "approach": "把门店看作 M/M/c 排队系统，用到达率与服务率刻画客流。",
        "steps": ["估计到达率", "拟合服务时间", "求稳态指标", "按布局枢举比较", "敏感性分析"],
        "risks": ["到达过程非泊松时失效"],
        "fit": "题面给出需求服从泊松分布，与排队机理契合",
    },
    "数据驱动": {
        "name": "需求回归预测",
        "approach": "以历史销量为标签训练梯度提升回归，预测各候选点需求。",
        "steps": ["整理特征", "交叉验证", "训练回归", "预测候选点", "按预测排序"],
        "risks": ["历史数据不足时过拟合"],
        "fit": "依赖历史销量数据，画像显示数据量中等",
    },
    "运筹优化": {
        "name": "整数规划",
        "approach": "MILP 建模，分支定界求解，预算作硬约束。",
        "steps": ["定义决策变量", "构建约束", "求解", "敏感性分析", "输出布局"],
        "risks": ["规模过大时求解超时"],
        "fit": "预算约束与离散选址天然是整数规划",
    },
}

REDUCE_OK = {
    "plans": [
        {
            "id": "A",
            "name": "整数规划",
            "approach": "MILP 建模，分支定界求解，预算作硬约束。",
            "steps": ["定义决策变量", "构建约束", "求解", "敏感性分析", "输出布局"],
            "risks": ["规模过大时求解超时"],
            "role": "primary",
            "source_views": ["operations_research"],
        },
        {
            "id": "B",
            "name": "排队论模型",
            "approach": "把门店看作 M/M/c 排队系统。",
            "steps": ["估计到达率", "拟合服务时间", "求稳态指标"],
            "risks": ["到达过程非泊松时失效"],
            "role": "baseline",
            "source_views": ["mechanism"],
        },
        {
            "id": "C",
            "name": "需求回归预测",
            "approach": "梯度提升回归预测候选点需求。",
            "steps": ["整理特征", "交叉验证", "训练回归"],
            "risks": ["历史数据不足时过拟合"],
            "role": "fallback",
            "source_views": ["data_driven"],
            "fallback_condition": "历史销量数据覆盖全部候选点时",
        },
    ],
    "recommended_plan_id": "A",
    "rationale": "预算约束与离散选址是整数规划的典型场景；排队论作对照基线。",
    "dropped": [],
    "progress_note": "三路提议各有侧重，推荐整数规划。",
}

#: 规范化调用的模型原样输出：故意带上模型常犯的毛病（乱序、$ 定界、别名枚举、
#: 写错的方案 id、缺 id），由 normalize_* 收拾成契约形状。
FORMALIZE_RAW = {
    "assumptions": [
        {"id": "M1", "text": "预算约束为硬约束", "scope": "方案 A", "basis": "题面", "impact": "High", "status": "重点验证"},
        {"id": "A1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面给定", "impact": "medium", "status": "confirmed"},
        {"id": "A2", "text": "候选点之间需求独立", "scope": "GLOBAL", "basis": "简化需要", "impact": "low", "status": "to_verify"},
        {"text": "到达过程近似泊松", "scope": "B", "basis": "排队论前提", "impact": "medium", "status": "to_verify"},
        {"id": "X9", "text": "历史销量覆盖全部候选点", "scope": "D", "basis": "数据画像", "impact": "high", "status": "pending"},
        {"id": "E0", "text": "   ", "scope": "global", "basis": "", "impact": "low", "status": "confirmed"},
    ],
    "symbols": [
        {"symbol": "$x_i$", "kind": "decision variable", "definition": "候选点 i 是否开店", "unit": "无", "range": "{0,1}", "plan_id": "A"},
        {"symbol": "i \\in \\mathcal{I}", "kind": "集合", "definition": "候选点索引", "unit": None, "range": "1…N", "plan_id": None},
        {"symbol": "\\(c_i\\)", "kind": "parameter", "definition": "候选点 i 的开店成本", "unit": "万元", "range": "≥ 0", "plan_id": "null"},
        {"symbol": "\\lambda", "kind": "rate", "definition": "顾客到达率", "unit": "人/小时", "range": "> 0", "plan_id": "Plan B"},
        {"symbol": "z", "kind": "objective", "definition": "总利润", "unit": "万元", "range": "最大化", "plan_id": "A"},
        {"symbol": "", "kind": "parameter", "definition": "空符号被剔除", "unit": None, "range": None, "plan_id": None},
    ],
}


def fanout_stubs(**overrides):
    """三路提议 + 归约 + 规范化的默认桩；按需覆盖某一环。"""
    responses = {
        "model_planning.proposer": proposer_stub(),
        "model_planning.reduce": stub_response(REDUCE_OK),
        "model_planning.formalize": stub_response(FORMALIZE_RAW),
    }
    responses.update(overrides)
    return responses


def proposer_stub(failing_views=(), barrier=None):
    """按 view_name 回提案；failing_views 里的视角回垃圾（两次都过不了校验）。"""

    def reply(variables):
        if barrier is not None:
            barrier.wait()
        view = variables["view_name"]
        if view in failing_views:
            return "这不是 JSON"
        return stub_response(PROPOSALS_BY_VIEW[view])

    return reply


def make_fanout_services(llm, audit=None):
    services = make_services(llm)
    services.extras["subagents"] = SubagentSupervisor(audit=audit)
    return services


def test_model_planning_fans_out_three_proposers_in_parallel_then_reduces(registry):
    import threading

    # 三路必须同时在飞：屏障要等满 3 个线程才放行，串行执行会在 5 秒后炸掉
    barrier = threading.Barrier(3, timeout=5)
    llm = StubLlmPort(fanout_stubs(**{"model_planning.proposer": proposer_stub(barrier=barrier)}))
    audits = []
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm, audit=audits.append))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.startswith("请确认建模方案：推荐 A「整数规划」")
    assert "备选 B「排队论模型」 / C「需求回归预测」" in result.review_reason
    meta = result.review_meta
    assert meta["gate"] == "G1" and meta["decision_type"] == "confirm_plan"
    assert [option["id"] for option in meta["options"]] == [
        "approve", "adopt:B", "adopt:C", "reject",
    ]
    assert meta["options"][0]["recommended"] is True
    assert meta["options"][0]["label"] == "采用推荐方案 A（整数规划）"
    assert meta["options"][2]["description"].startswith("条件回退：")
    assert "触发条件：历史销量数据覆盖全部候选点时" in meta["options"][2]["description"]
    assert meta["impact"]["proposers"] == {
        "succeeded": ["mechanism", "data_driven", "operations_research"],
        "failed": [],
    }
    assert [plan["role"] for plan in meta["impact"]["plans"]] == ["primary", "baseline", "fallback"]

    outputs = result.outputs
    assert [plan["id"] for plan in outputs["plans"]] == ["A", "B", "C"]
    assert outputs["recommended_plan_id"] == "A"
    assert outputs["rationale"] == REDUCE_OK["rationale"]
    assert outputs["progress_note"] == REDUCE_OK["progress_note"]
    # 三路提议按视角顺序留档（去重前原样），与线程完成顺序无关
    assert [proposal["view"] for proposal in outputs["proposals"]] == [
        "mechanism", "data_driven", "operations_research",
    ]
    assert outputs["proposer_failures"] == [] and outputs["quality_warnings"] == []
    # 3 路提议 + 归约 + 规范化（假设表 / 符号表）
    assert outputs["llm_attempts"] == 5

    # 调用形状：三次提议人调用都在归约之前；归约拿到三份提案与视角说明；
    # 规范化在归约之后、G1 之前
    prompt_ids = [call.prompt_id for call in llm.calls]
    assert prompt_ids[:3] == ["model_planning.proposer"] * 3
    assert prompt_ids[3:] == ["model_planning.reduce", "model_planning.formalize"]
    briefs = {call.variables["view_name"]: call.variables["view_brief"] for call in llm.calls[:3]}
    assert set(briefs) == {"机理建模", "数据驱动", "运筹优化"}
    assert all("泊松分布" in call.variables["problem_analysis"] for call in llm.calls[:3])
    proposals_sent = json.loads(llm.calls[3].variables["proposals"])
    assert [entry["view"] for entry in proposals_sent] == [
        "mechanism", "data_driven", "operations_research",
    ]
    assert {entry["name"] for entry in proposals_sent} == {"排队论模型", "需求回归预测", "整数规划"}

    # Supervisor 双审计：每路 spawn + result，kind 带视角
    spawns = [payload for payload in audits if payload["phase"] == "spawn"]
    results = [payload for payload in audits if payload["phase"] == "result"]
    assert sorted(payload["tool"] for payload in spawns) == [
        "subagent:proposer:data_driven",
        "subagent:proposer:mechanism",
        "subagent:proposer:operations_research",
    ]
    assert [payload["envelope_status"] for payload in results] == ["done"] * 3
    assert all(payload["tool_tier"] == "readonly" for payload in spawns)


def test_model_planning_quorum_two_of_three_reduces_and_records_the_failure(registry):
    llm = StubLlmPort(fanout_stubs(**{
        "model_planning.proposer": proposer_stub(failing_views=("运筹优化",)),
    }))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.endswith("；1 路视角提议未成功")
    assert result.outputs["proposer_failures"] == result.outputs["quality_warnings"]
    [failure] = result.outputs["proposer_failures"]
    assert failure.startswith("视角「运筹优化」未成功（failed")
    succeeded = ["mechanism", "data_driven"]
    assert [proposal["view"] for proposal in result.outputs["proposals"]] == succeeded
    assert result.review_meta["impact"]["proposers"]["succeeded"] == succeeded
    # 归约照做（≥2 路成功），并被告知哪一路缺席
    reduce_call = next(call for call in llm.calls if call.prompt_id == "model_planning.reduce")
    proposals_sent = json.loads(reduce_call.variables["proposals"])
    assert proposals_sent[-1]["status"] == "failed" and "运筹优化" in proposals_sent[-1]["note"]
    # 失败那一路用掉一次修复重试：2 + 1 + 1 + 归约 1 + 规范化 1
    assert result.outputs["llm_attempts"] == 6


def test_model_planning_single_success_degrades_to_one_plan_without_reduce(registry):
    llm = StubLlmPort(fanout_stubs(**{
        "model_planning.proposer": proposer_stub(failing_views=("数据驱动", "运筹优化")),
    }))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "model_planning.reduce" not in [call.prompt_id for call in llm.calls]
    plans = result.outputs["plans"]
    assert [(plan["id"], plan["name"], plan["role"]) for plan in plans] == [
        ("A", "排队论模型", "primary"),
    ]
    assert result.outputs["recommended_plan_id"] == "A"
    assert result.outputs["rationale"].startswith("按「机理建模」视角的提议作为推荐方案：")
    assert [option["id"] for option in result.review_meta["options"]] == ["approve", "reject"]
    assert result.review_reason == "请确认建模方案：推荐 A「排队论模型」；2 路视角提议未成功"
    [*_, degraded] = result.outputs["quality_warnings"]
    assert degraded.startswith("仅「机理建模」一路视角成功，未做归约")
    assert len(result.outputs["proposer_failures"]) == 2
    # 降级路径也做规范化：单案照样有假设表 / 符号表，方案 id 只认 A
    formalize_call = next(call for call in llm.calls if call.prompt_id == "model_planning.formalize")
    assert [plan["id"] for plan in json.loads(formalize_call.variables["plans"])] == ["A"]
    assert {entry["scope"] for entry in result.outputs["assumptions"]} <= {"global", "A"}
    assert {entry["plan_id"] for entry in result.outputs["symbols"]} <= {None, "A"}


def test_model_planning_fails_when_every_proposer_fails(registry):
    every_view = ("机理建模", "数据驱动", "运筹优化")
    llm = StubLlmPort(fanout_stubs(**{
        "model_planning.proposer": proposer_stub(failing_views=every_view),
    }))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.FAILED
    assert result.error.startswith("全部视角的方案提议都未成功：")
    assert result.error.count("视角「") == 3
    assert result.metrics["llm_attempts"] == 6


def test_model_planning_budget_stop_propagates_instead_of_degrading(registry):
    """预算硬停（E310）是运行级事实：不能被当成「某一路视角未成功」吞掉。"""
    from omm_agent_core.errors import AgentError, ErrorCode

    def reply(variables):
        if variables["view_name"] == "数据驱动":
            raise AgentError(ErrorCode.BUDGET_RUN, "LLM 调用次数将超过上限 2")
        return stub_response(PROPOSALS_BY_VIEW[variables["view_name"]])

    llm = StubLlmPort(fanout_stubs(**{"model_planning.proposer": reply}))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    with pytest.raises(AgentError) as raised:
        node.run(ctx, make_fanout_services(llm))

    assert raised.value.code is ErrorCode.BUDGET_RUN
    assert "[E310]" in str(raised.value)
    # 另两路照常跑完（fan-out 已收束），归约没有开始
    assert [call.prompt_id for call in llm.calls] == ["model_planning.proposer"] * 3


def test_model_planning_reduce_failure_falls_back_to_direct_candidates(registry):
    llm = StubLlmPort(fanout_stubs(**{"model_planning.reduce": "归约人跑题了"}))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    plans = result.outputs["plans"]
    assert [(plan["id"], plan["name"], plan["role"]) for plan in plans] == [
        ("A", "排队论模型", "primary"),
        ("B", "需求回归预测", "candidate"),
        ("C", "整数规划", "candidate"),
    ]
    assert [plan["source_views"] for plan in plans] == [
        ["mechanism"], ["data_driven"], ["operations_research"],
    ]
    assert [option["id"] for option in result.review_meta["options"]] == [
        "approve", "adopt:B", "adopt:C", "reject",
    ]
    assert result.review_meta["options"][1]["description"].startswith("候选：")
    [warning] = result.outputs["quality_warnings"]
    assert warning.startswith("方案归约未成功（")
    # 归约的一次修复也算进去：3 + 2 + 规范化 1
    assert result.outputs["llm_attempts"] == 6


def test_model_planning_rejects_an_inconsistent_reduction(registry):
    bad = dict(REDUCE_OK, plans=[dict(REDUCE_OK["plans"][0]), dict(REDUCE_OK["plans"][1], id="A")])
    llm = StubLlmPort(fanout_stubs(**{"model_planning.reduce": stub_response(bad)}))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.FAILED
    assert "方案 id 'A' 重复" in result.error
    # 不合法的归约在规范化之前就止步，不再多烧一次调用
    assert "model_planning.formalize" not in [call.prompt_id for call in llm.calls]


def test_model_planning_fanout_can_run_unattended(registry):
    llm = StubLlmPort(fanout_stubs())
    node = ModelPlanningNode(registry, require_confirmation=False)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert [plan["id"] for plan in result.outputs["plans"]] == ["A", "B", "C"]
    assert result.metrics == {"llm_attempts": 5}
    assert result.outputs["assumptions"] and result.outputs["symbols"]


def test_model_planning_formalizes_assumptions_and_symbols_after_reduce(registry):
    """归约 → 假设表 + 符号表 → G1（§9.1）：模型输出的毛病由确定性归一化收拾干净。"""
    llm = StubLlmPort(fanout_stubs())
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    formalize_call = llm.calls[-1]
    assert formalize_call.prompt_id == "model_planning.formalize"
    # 规范化拿到归约后的全部方案卡（A/B/C）与问题分析、数据画像
    assert [plan["id"] for plan in json.loads(formalize_call.variables["plans"])] == ["A", "B", "C"]
    assert "泊松分布" in formalize_call.variables["problem_analysis"]
    assert formalize_call.variables["data_profile"]

    assumptions = result.outputs["assumptions"]
    # 全局在前（G1、G2），其后按方案 A、B 顺序重编号；scope 写成「方案 A」认得出，
    # 写成不存在的 D 归为全局；空 text 剔除；枚举别名（High / 重点验证 / pending）收敛
    assert [(entry["id"], entry["scope"]) for entry in assumptions] == [
        ("G1", "global"), ("G2", "global"), ("G3", "global"), ("A1", "A"), ("B1", "B"),
    ]
    assert [entry["text"] for entry in assumptions] == [
        "需求服从泊松分布", "候选点之间需求独立", "历史销量覆盖全部候选点",
        "预算约束为硬约束", "到达过程近似泊松",
    ]
    assert assumptions[3]["impact"] == "high" and assumptions[3]["status"] == "critical"
    assert assumptions[2]["status"] == "to_verify"
    assert set(assumptions[0]) == {"id", "text", "scope", "basis", "impact", "status"}

    symbols = result.outputs["symbols"]
    # 共享在前（plan_id None），其后按方案顺序；$ / \( \) 定界剥掉；kind 别名收敛、
    # 认不出的 rate → other；plan_id 写成「Plan B」认得出、字面 "null" 当共享；空符号剔除
    assert [(entry["symbol"], entry["plan_id"]) for entry in symbols] == [
        ("i \\in \\mathcal{I}", None), ("c_i", None), ("x_i", "A"), ("z", "A"), ("\\lambda", "B"),
    ]
    assert [entry["kind"] for entry in symbols] == ["set", "parameter", "variable", "objective", "other"]
    assert symbols[2]["unit"] is None and symbols[2]["range"] == "{0,1}"
    assert set(symbols[0]) == {"symbol", "kind", "definition", "unit", "range", "plan_id"}
    assert result.outputs["quality_warnings"] == []


def test_model_planning_formalize_failure_keeps_the_gate_and_records_a_warning(registry):
    """两表是材料不是闸门依据：规范化失败只留 null + 警告，G1 照常挂出。"""
    llm = StubLlmPort(fanout_stubs(**{"model_planning.formalize": "规范化员跑题了"}))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_meta["gate"] == "G1"
    assert result.outputs["assumptions"] is None and result.outputs["symbols"] is None
    [warning] = result.outputs["quality_warnings"]
    assert warning.startswith("模型假设表与符号表未生成（")
    # 规范化的一次修复也算进去：3 + 1 + 2
    assert result.outputs["llm_attempts"] == 6


def test_normalize_assumptions_and_symbols_clip_and_stay_deterministic():
    from omm_agent_skills import normalize_assumptions, normalize_symbols

    many = [
        {"text": f"全局假设 {index}", "scope": "global", "impact": "low", "status": "confirmed"}
        for index in range(10)
    ] + [
        {"text": f"方案假设 {index}", "scope": "A", "impact": "low", "status": "confirmed"}
        for index in range(5)
    ]
    clipped = normalize_assumptions(many, ["A"])
    # 上限 12：先保全局 10 条，方案 A 只剩 2 条；id 按组重编号
    assert len(clipped) == 12
    assert [entry["id"] for entry in clipped][-3:] == ["G10", "A1", "A2"]
    assert normalize_assumptions(many, ["A"]) == clipped
    assert normalize_assumptions("不是列表", ["A"]) == []
    assert normalize_assumptions([{"scope": "global"}], ["A"]) == []

    symbols = [
        {"symbol": f"p_{index}", "kind": "parameter", "definition": f"参数 {index}"}
        for index in range(30)
    ]
    assert len(normalize_symbols(symbols, ["A"])) == 24
    [only] = normalize_symbols(
        [{"symbol": "$$y$$", "kind": "Variable", "definition": "产量", "unit": "N/A", "range": "-", "plan_id": "a"}],
        ["A"],
    )
    assert only == {
        "symbol": "y", "kind": "variable", "definition": "产量",
        "unit": None, "range": None, "plan_id": "A",
    }
    assert normalize_symbols(None, ["A"]) == []


def test_model_planning_without_views_keeps_the_single_call_path(registry):
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry, proposer_views=())
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_meta is None
    assert [call.prompt_id for call in llm.calls] == ["model_planning.default"]


# -- ModelPlanningNode：知识库预检索材料（§10.3 薄版一） ---------------------------

KNOWLEDGE_PROBLEM_HITS_FAKE = [
    {
        "id": "problem:cumcm-2021-c",
        "kind": "problem",
        "title": "生产企业原材料的订购与运输",
        "year": 2021,
        "competition": "全国大学生数学建模竞赛",
        "code": "2021 CUMCM C",
        "problem_type": "规划优化",
        "modeling_directions": ["优化模型", "决策分析"],
        "keywords": ["订购", "运输"],
        "source_id": "cumcm_official",
        "source_url": "https://example.test/cumcm-2021-c",
        "linked_paper_count": 2,
        "linked_paper_models": [{"model": "TOPSIS", "count": 2}, {"model": "整数规划", "count": 1}],
    },
    {
        "id": "problem:cumcm-2020-b",
        "kind": "problem",
        "title": "穿越沙漠",
        "year": 2020,
        "competition": "全国大学生数学建模竞赛",
        "code": "2020 CUMCM B",
        "problem_type": "规划优化",
        "modeling_directions": ["优化模型"],
        "keywords": [],
        "source_id": "cumcm_official",
        "source_url": "https://example.test/cumcm-2020-b",
        "linked_paper_count": 0,
        "linked_paper_models": [],
    },
]

KNOWLEDGE_PAPER_HITS_FAKE = [
    {
        "id": "paper:paper-2021-c-1",
        "kind": "paper",
        "title": "基于混合整数规划的订购方案",
        "year": 2021,
        "competition": "全国大学生数学建模竞赛",
        "award": "国家一等奖",
        "models": ["整数规划", "TOPSIS"],
        "problem_id": "problem:cumcm-2021-c",
        "source_id": "cumcm_papers",
        "source_url": "https://example.test/paper-2021-c-1",
    },
    {
        "id": "paper:no-models",
        "kind": "paper",
        "title": "只有封面的论文",
        "year": 2019,
        "competition": "COMAP MCM/ICM",
        "award": "Outstanding Winner",
        "models": [],
        "problem_id": None,
        "source_id": "comap_mcm_icm",
        "source_url": "https://example.test/no-models",
    },
    {
        "id": "paper:orphan",
        "kind": "paper",
        "title": "机场出租车排队仿真",
        "year": 2018,
        "competition": "中国研究生数学建模竞赛",
        "award": "优秀论文",
        "models": ["排队论", "离散事件仿真"],
        "problem_id": None,
        "source_id": "cpmcm_papers",
        "source_url": "https://example.test/orphan",
    },
]


class FakeKnowledge:
    """KnowledgePort 假端口：记录每次查询，按 kind 回固定命中。"""

    def __init__(self, problems=None, papers=None, error=None):
        self.problems = KNOWLEDGE_PROBLEM_HITS_FAKE if problems is None else problems
        self.papers = KNOWLEDGE_PAPER_HITS_FAKE if papers is None else papers
        self.error = error
        self.queries = []

    def search(self, query, *, kind=None, task_type=None, limit=8):
        self.queries.append({"query": query, "kind": kind, "task_type": task_type, "limit": limit})
        if self.error is not None:
            raise self.error
        hits = self.problems if kind == "problem" else self.papers
        return [dict(hit) for hit in hits[:limit]]

    def read(self, card_id):
        return None


def test_knowledge_material_lists_problem_cards_with_linked_models_then_papers():
    port = FakeKnowledge()
    material = knowledge_material(port, ANALYSIS_OK)

    lines = material.splitlines()
    assert lines[0] == (
        "- [problem:cumcm-2021-c] 2021 全国大学生数学建模竞赛 2021 CUMCM C「生产企业原材料的订购与运输」"
        "（规划优化｜优化模型、决策分析｜订购、运输）——获奖论文用过：TOPSIS ×2、整数规划"
    )
    assert lines[1].startswith("- [problem:cumcm-2020-b] 2020 全国大学生数学建模竞赛 2020 CUMCM B「穿越沙漠」")
    assert lines[1].endswith("——暂无挂接的获奖论文模型记录")
    # 论文行：没有 models 的不列；挂在已展示赛题上的不再单列（模型已在赛题行聚合）
    assert lines[2:] == [
        "- [paper:orphan] 2018 中国研究生数学建模竞赛「机场出租车排队仿真」（优秀论文）：模型 = 排队论、离散事件仿真"
    ]
    assert knowledge_hit_ids(material) == ["problem:cumcm-2021-c", "problem:cumcm-2020-b", "paper:orphan"]

    # query 由题名 / 题型 / 目标 / 子问题拼成；两次检索分别限定 kind
    assert [entry["kind"] for entry in port.queries] == ["problem", "paper"]
    assert port.queries[0]["query"] == "门店选址优化 优化 确定最优布局 确定最优布局"
    assert knowledge_query(ANALYSIS_OK) == port.queries[0]["query"]
    assert knowledge_material(port, ANALYSIS_OK) == material, "确定性：同分析同材料"


def test_knowledge_material_falls_back_to_the_three_kinds_of_none():
    assert knowledge_material(None, ANALYSIS_OK) == NO_KNOWLEDGE_NOTE
    assert knowledge_material(FakeKnowledge(problems=[], papers=[]), ANALYSIS_OK) == NO_KNOWLEDGE_HITS_NOTE
    # 只有没模型记录的论文 → 等于没命中
    assert knowledge_material(FakeKnowledge(problems=[], papers=[KNOWLEDGE_PAPER_HITS_FAKE[1]]), ANALYSIS_OK) == NO_KNOWLEDGE_HITS_NOTE
    # 分析里没有可检索的文本 → 不调端口
    port = FakeKnowledge()
    assert knowledge_material(port, {"viability": "ok"}) == NO_KNOWLEDGE_HITS_NOTE and port.queries == []
    # 检索抛错：材料落「无（…失败…）」，不上抛
    failing = knowledge_material(FakeKnowledge(error=RuntimeError("index corrupt")), ANALYSIS_OK)
    assert failing.startswith("无（知识库检索失败：") and "index corrupt" in failing
    assert knowledge_hit_ids(NO_KNOWLEDGE_NOTE) == []


def test_knowledge_material_is_bounded_by_whole_lines():
    many = [
        {**KNOWLEDGE_PROBLEM_HITS_FAKE[0], "id": f"problem:p{index}", "title": "长标题" * 60}
        for index in range(4)
    ]
    material = knowledge_material(FakeKnowledge(problems=many, papers=[]), ANALYSIS_OK)
    assert len(material) <= KNOWLEDGE_MATERIAL_CHARS
    assert all(line.startswith("- [problem:p") for line in material.splitlines())
    assert material.splitlines()[0].startswith("- [problem:p0]")


def test_model_planning_feeds_knowledge_material_to_every_proposer_and_the_audit_slice(registry):
    port = FakeKnowledge()
    captured = []

    class RecordingSupervisor(SubagentSupervisor):
        def spawn(self, spec, runner, **kwargs):
            captured.append(dict(spec.context_slice))
            return super().spawn(spec, runner, **kwargs)

    llm = StubLlmPort(fanout_stubs())
    node = ModelPlanningNode(registry, knowledge=port)
    assert node.knowledge is port
    services = make_services(llm)
    services.extras["subagents"] = RecordingSupervisor()
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, services)

    assert result.status == NodeResult.NEEDS_REVIEW
    proposer_calls = [call for call in llm.calls if call.prompt_id == "model_planning.proposer"]
    assert len(proposer_calls) == 3
    for call in proposer_calls:
        assert call.variables["knowledge"].startswith("- [problem:cumcm-2021-c]")
        rendered = registry.get("model_planning.proposer").render(call.variables)
        assert "## 相似赛题与获奖论文方法（知识库检索，按相关度）" in rendered
        assert "获奖论文用过：TOPSIS ×2、整数规划" in rendered
        assert "不得照搬" in rendered
    # 归约与规范化不带先例材料
    for call in llm.calls:
        if call.prompt_id in ("model_planning.reduce", "model_planning.formalize"):
            assert "knowledge" not in call.variables
    # 检索只做一次（fan-out 前），不是每路一次
    assert [entry["kind"] for entry in port.queries] == ["problem", "paper"]
    assert len(captured) == 3
    assert all(
        slice_["knowledge_hits"] == ["problem:cumcm-2021-c", "problem:cumcm-2020-b", "paper:orphan"]
        for slice_ in captured
    )


def test_model_planning_without_knowledge_port_renders_the_none_note(registry):
    llm = StubLlmPort(fanout_stubs())
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    proposer_calls = [call for call in llm.calls if call.prompt_id == "model_planning.proposer"]
    assert all(call.variables["knowledge"] == NO_KNOWLEDGE_NOTE for call in proposer_calls)
    rendered = registry.get("model_planning.proposer").render(proposer_calls[0].variables)
    assert "## 相似赛题与获奖论文方法（知识库检索，按相关度）\n\n无（知识库不可用）" in rendered


def test_model_planning_single_call_path_also_gets_knowledge(registry):
    port = FakeKnowledge()
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry, proposer_views=(), knowledge=port)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert llm.calls[0].variables["knowledge"].startswith("- [problem:cumcm-2021-c]")
    rendered = registry.get("model_planning.default").render(llm.calls[0].variables)
    assert "## 相似赛题与获奖论文方法（知识库检索，按相关度）" in rendered


def test_model_planning_knowledge_failure_does_not_block_the_stage(registry):
    llm = StubLlmPort(fanout_stubs())
    node = ModelPlanningNode(registry, knowledge=FakeKnowledge(error=RuntimeError("boom")))
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    proposer_calls = [call for call in llm.calls if call.prompt_id == "model_planning.proposer"]
    assert all(call.variables["knowledge"].startswith("无（知识库检索失败：boom") for call in proposer_calls)
    assert result.outputs["quality_warnings"] == [], "知识库是材料不是闸门：不产生质量警告"


# -- 提议人自主检索（§10.3 切片二）：知识库两个只读工具经 ToolBus 进提议人会话 -------


class KnowledgeToolBus:
    """假 ToolBus：只挂知识库两个只读工具，其余按允许清单拒绝；记录每次调用。"""

    def __init__(self, port, cards=None):
        self.port = port
        self.cards = dict(cards or {})
        self.calls = []

    def invoke(self, run_id, step_id, tool_name, arguments):
        self.calls.append((run_id, step_id, tool_name, dict(arguments)))
        if tool_name == "knowledge_search":
            hits = self.port.search(
                arguments["query"],
                kind=arguments.get("kind"),
                task_type=arguments.get("task_type"),
                limit=int(arguments.get("limit", 8)),
            )
            return ToolResult(
                status="succeeded",
                output={"query": arguments["query"], "hits": hits, "total": len(hits)},
            )
        if tool_name == "knowledge_read":
            card = self.cards.get(arguments.get("card_id"))
            if card is None:
                return ToolResult(status="failed", error=f"未找到卡片：{arguments.get('card_id')}")
            return ToolResult(status="succeeded", output=card)
        return ToolResult(status="failed", error=f"tool {tool_name!r} is not on the allowlist")


def _view_of(messages):
    import re

    match = re.search(r"「(.+?)」方案提议人", messages[0]["content"])
    assert match, "提议人会话的 system 消息应是渲染后的角色卡"
    return match.group(1)


def _observations(messages):
    return [m["content"] for m in messages if m["content"].startswith("[工具执行结果]")]


def _envelope(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False)


CARD_2021_C = {
    "id": "problem:cumcm-2021-c",
    "kind": "problem",
    "title": "生产企业原材料的订购与运输",
    "content": "某建筑和装饰板材的生产企业所用原材料主要是木质纤维……",
    "papers": [{"id": "paper:paper-2021-c-1", "models": ["整数规划", "TOPSIS"]}],
}


def searching_proposer(messages):
    """一路提议人的会话脚本：先检索，命中后顺链读全卡，再交终答。"""
    seen = _observations(messages)
    if not seen:
        return _envelope("knowledge_search", query="原材料订购 运输 整数规划", kind="problem")
    if len(seen) == 1:
        assert '"id": "problem:cumcm-2021-c"' in seen[0], "检索观察应带命中卡片 id"
        return _envelope("knowledge_read", card_id="problem:cumcm-2021-c")
    assert "木质纤维" in seen[1], "读卡观察应是全卡正文"
    return stub_response(PROPOSALS_BY_VIEW[_view_of(messages)])


def make_tool_use_llm(script=searching_proposer):
    return StubLlmPort(
        fanout_stubs(**{"model_planning.proposer": "不应走单次调用路径"}),
        chat_scripts={"model_planning.proposer": [script]},
    )


def test_model_planning_proposers_search_the_knowledge_library_through_the_tool_bus(registry):
    port = FakeKnowledge()
    bus = KnowledgeToolBus(port, cards={"problem:cumcm-2021-c": CARD_2021_C})
    llm = make_tool_use_llm()
    audits = []
    node = ModelPlanningNode(registry, knowledge=port)
    services = make_services(llm, tools=bus)
    services.extras["subagents"] = SubagentSupervisor(audit=audits.append)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert [plan["id"] for plan in result.outputs["plans"]] == ["A", "B", "C"]
    # 提议人全部走会话（label = 提示词 id），归约 / 规范化仍是模板单发
    assert [call.prompt_id for call in llm.calls] == ["model_planning.reduce", "model_planning.formalize"]
    proposer_chats = [call for call in llm.chat_calls if call.label == "model_planning.proposer"]
    assert len(proposer_chats) == 9, "每路：检索信封 + 读卡信封 + 终答 = 3 次调用"
    # 3 路 × 3 次会话调用 + 归约 + 规范化
    assert result.outputs["llm_attempts"] == 11

    # 会话形状：system = 模板渲染全文（视角 + 预检索材料仍在），user = 协议 + 策略
    opening = next(call for call in proposer_chats if not _observations(call.messages))
    assert [m["role"] for m in opening.messages] == ["system", "user"]
    assert "## 相似赛题与获奖论文方法（知识库检索，按相关度）\n\n- [problem:cumcm-2021-c]" in opening.messages[0]["content"]
    brief = opening.messages[1]["content"]
    assert "- knowledge_search：" in brief and "- knowledge_read：" in brief
    assert "python_run" not in brief, "提议人拿不到沙盒工具"
    assert "按「输出要求」输出终答 JSON" in brief and "至多 3 次" in brief
    views_opened = {_view_of(call.messages) for call in proposer_chats}
    assert views_opened == {"机理建模", "数据驱动", "运筹优化"}

    # 工具调用经 services.tools（ToolBus 留痕 TOOL_CALLED 的那条路），带 run / step 标识
    assert len(bus.calls) == 6
    assert {(run_id, step_id) for run_id, step_id, _, _ in bus.calls} == {("run_1", "step_1")}
    assert sorted(name for _, _, name, _ in bus.calls) == ["knowledge_read"] * 3 + ["knowledge_search"] * 3
    searches = [args for _, _, name, args in bus.calls if name == "knowledge_search"]
    assert searches == [{"query": "原材料订购 运输 整数规划", "kind": "problem"}] * 3
    # 节点预检索（fan-out 前两次）之外，三路各自检索一次，都打在同一个端口上
    assert [entry["kind"] for entry in port.queries] == ["problem", "paper"] + ["problem"] * 3
    # 观察以 user 消息回给模型（文本协议），终答那次会话带两条观察
    final_call = next(
        call for call in proposer_chats if len(_observations(call.messages)) == 2
    )
    assert [m["role"] for m in final_call.messages] == ["system", "user", "assistant", "user", "assistant", "user"]

    # Supervisor spawn 审计能看见子代理拿到的工具集与档位
    spawns = [payload for payload in audits if payload["phase"] == "spawn"]
    assert len(spawns) == 3
    assert all(payload["toolset"] == ["knowledge_search", "knowledge_read"] for payload in spawns)
    assert all(payload["tool_tier"] == "readonly" for payload in spawns)
    assert [payload["envelope_status"] for payload in audits if payload["phase"] == "result"] == ["done"] * 3


def test_proposer_tool_rounds_are_capped_and_off_list_tools_are_refused(registry):
    def script(messages):
        view = _view_of(messages)
        seen = _observations(messages)
        if view == "数据驱动":
            # 越界工具：拿到拒绝观察后直接终答
            if not seen:
                return _envelope("ws_list", prefix="data/")
            assert "不在本任务允许清单" in seen[0] and "knowledge_read, knowledge_search" in seen[0]
            return stub_response(PROPOSALS_BY_VIEW[view])
        if view == "机理建模":
            # 检索上限：三轮之后收到「已用完」观察就终答
            if any("检索次数已用完" in item for item in seen):
                return stub_response(PROPOSALS_BY_VIEW[view])
            return _envelope("knowledge_search", query=f"第 {len(seen) + 1} 次检索")
        # 运筹优化：无视「已用完」继续检索 → 同一工具连败两次判无进展，这一路失败
        return _envelope("knowledge_search", query=f"第 {len(seen) + 1} 次检索")

    port = FakeKnowledge()
    bus = KnowledgeToolBus(port)
    llm = make_tool_use_llm(script)
    node = ModelPlanningNode(registry, knowledge=port)
    services = make_services(llm, tools=bus)
    services.extras["subagents"] = SubagentSupervisor()
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.outputs["proposer_failures"] == [
        "视角「运筹优化」未成功（failed，knowledge_search: 检索次数已用完（至多 3 次），请直接输出终答 JSON）"
    ]
    assert [proposal["view"] for proposal in result.outputs["proposals"]] == ["mechanism", "data_driven"]
    # 端口只被工具路径打了 3 + 3 次（两路各三轮真检索；越界工具与超限信封都不到端口）
    search_calls = [args for _, _, name, args in bus.calls if name == "knowledge_search"]
    assert len(search_calls) == 6
    assert not any(name == "ws_list" for _, _, name, _ in bus.calls), "越界工具名在执行器就被挡下"
    assert [entry["kind"] for entry in port.queries] == ["problem", "paper"] + [None] * 6
    # 每路的会话调用次数：机理 3 检索 + 1 超限观察 + 终答 = 5；数据驱动 1 拒绝 + 终答 = 2；
    # 运筹 3 检索 + 2 超限（同名连败两次即止）= 5
    per_view = {}
    for call in llm.chat_calls:
        per_view[_view_of(call.messages)] = per_view.get(_view_of(call.messages), 0) + 1
    assert per_view == {"机理建模": 5, "数据驱动": 2, "运筹优化": 5}
    assert result.outputs["llm_attempts"] == 12 + 2


def test_proposer_tool_use_needs_knowledge_port_tool_bus_and_chat_support(registry):
    class CompleteOnlyPort:
        """只有 complete 的端口（不支持会话）：提议人回到单次调用路径。"""

        def __init__(self, inner):
            self._inner = inner
            self.calls = inner.calls

        def complete(self, prompt_id, variables):
            return self._inner.complete(prompt_id, variables)

    port = FakeKnowledge()
    cases = {
        "无知识库端口": (None, KnowledgeToolBus(port), StubLlmPort(fanout_stubs())),
        "无 ToolBus": (port, None, StubLlmPort(fanout_stubs())),
        "端口不支持会话": (port, KnowledgeToolBus(port), CompleteOnlyPort(StubLlmPort(fanout_stubs()))),
    }
    for label, (knowledge, tools, llm) in cases.items():
        audits = []
        node = ModelPlanningNode(registry, knowledge=knowledge)
        services = make_services(llm, tools=tools)
        services.extras["subagents"] = SubagentSupervisor(audit=audits.append)
        ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

        result = node.run(ctx, services)

        assert result.status == NodeResult.NEEDS_REVIEW, label
        assert [call.prompt_id for call in llm.calls[:3]] == ["model_planning.proposer"] * 3, label
        assert getattr(llm, "chat_calls", []) == [], label
        assert result.outputs["llm_attempts"] == 5, label
        spawns = [payload for payload in audits if payload["phase"] == "spawn"]
        assert [payload["toolset"] for payload in spawns] == [[], [], []], label
        if tools is not None:
            assert tools.calls == [], label


# -- 方案卡「实现语言」（§7.4：语言随 G1 确认）------------------------------------


def test_normalize_plan_languages_fills_defaults_and_folds_aliases():
    assert normalize_language(" Python3 ") == "python"
    assert normalize_language("Rscript") == "r" and normalize_language("北太天元") == "baltamatica"
    assert normalize_language(None) == "" and normalize_language("Fortran") == "fortran"

    plans = [
        {"id": "A"},                        # 缺省 → 可用列表首项
        {"id": "B", "language": "PY"},      # 别名 → python
        {"id": "C", "language": "matlab"},  # 不在可用列表 → 首项 + 留痕
    ]
    warnings = normalize_plan_languages(plans, ("python",))
    assert [plan["language"] for plan in plans] == ["python", "python", "python"]
    assert warnings == ["方案 C 提出的实现语言 matlab 当前执行器不支持，按 python 记"]

    # 多语言执行器：在列表内的原样认；列表本身也做别名归一与去重
    plans = [{"id": "A", "language": "R"}, {"id": "B", "language": "Julia"}]
    assert normalize_plan_languages(plans, ("Python", "r language", "python")) == [
        "方案 B 提出的实现语言 julia 当前执行器不支持，按 python 记",
    ]
    assert [plan["language"] for plan in plans] == ["r", "python"]
    # 空可用列表退回缺省 python，卡片不会没有语言
    plans = [{"id": "A"}]
    assert normalize_plan_languages(plans, ()) == [] and plans[0]["language"] == "python"


def test_model_planning_reduce_gets_the_language_menu_and_cards_carry_it(registry):
    reduce_with_languages = {
        **REDUCE_OK,
        "plans": [
            {**REDUCE_OK["plans"][0], "language": "Python"},
            {**REDUCE_OK["plans"][1], "language": "matlab"},
            dict(REDUCE_OK["plans"][2]),  # 归约人漏写 → 缺省
        ],
    }
    llm = StubLlmPort(fanout_stubs(**{"model_planning.reduce": stub_response(reduce_with_languages)}))
    node = ModelPlanningNode(registry)
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_fanout_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert node.implementation_languages == ("python",)
    assert [plan["language"] for plan in result.outputs["plans"]] == ["python", "python", "python"]
    assert result.outputs["quality_warnings"] == [
        "方案 B 提出的实现语言 matlab 当前执行器不支持，按 python 记",
    ]
    # 归约 prompt 拿到可用语言菜单；提议人不选语言（变量里有但模板不渲染）
    reduce_call = next(call for call in llm.calls if call.prompt_id == "model_planning.reduce")
    assert reduce_call.variables["implementation_languages"] == "python"
    rendered = registry.get("model_planning.reduce").render(reduce_call.variables)
    assert "## 当前可用的实现语言\n\npython" in rendered
    assert "`language`" in rendered
    proposer_rendered = registry.get("model_planning.proposer").render(llm.calls[0].variables)
    assert "可用的实现语言" not in proposer_rendered
    # 语言随 G1 确认：出现在用户点选的那一行上
    descriptions = [option["description"] for option in result.review_meta["options"][:3]]
    assert all(description.endswith("；实现语言 python") for description in descriptions)
    assert result.review_meta["options"][2]["description"].startswith("条件回退：")
    # 规范化调用拿到的是带语言的方案卡（同一份卡片进假设表 / 符号表整理）
    formalize_call = next(call for call in llm.calls if call.prompt_id == "model_planning.formalize")
    assert all(plan["language"] == "python" for plan in json.loads(formalize_call.variables["plans"]))


def test_model_planning_single_call_path_also_carries_languages(registry):
    llm = StubLlmPort({"model_planning.default": stub_response(PLANNING_OK)})
    node = ModelPlanningNode(registry, proposer_views=(), implementation_languages=("Python", "R"))
    ctx = make_ctx(TaskState.MODEL_PLANNING, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert node.implementation_languages == ("python", "r")
    assert llm.calls[0].variables["implementation_languages"] == "python、r"
    assert "## 当前可用的实现语言\n\npython、r" in registry.get("model_planning.default").render(
        llm.calls[0].variables
    )
    # 桩输出没写 language：缺省补首项，不记警告
    assert [plan["language"] for plan in result.outputs["plans"]] == ["python", "python"]
    assert "quality_warnings" not in result.outputs

    with_r = {**PLANNING_OK, "plans": [dict(PLANNING_OK["plans"][0], language="R"), dict(PLANNING_OK["plans"][1], language="octave")]}
    llm = StubLlmPort({"model_planning.default": stub_response(with_r)})
    result = node.run(ctx, make_services(llm))
    assert [plan["language"] for plan in result.outputs["plans"]] == ["r", "python"]
    assert result.outputs["quality_warnings"] == [
        "方案 B 提出的实现语言 octave 当前执行器不支持，按 python 记",
    ]


def test_chosen_plan_honours_the_g1_ledger():
    assert chosen_plan(PLANNING_OK)["id"] == "A"
    assert chosen_plan(PLANNING_OK, {"MODEL_PLANNING": "approve"})["id"] == "A"
    assert chosen_plan(PLANNING_OK, {"MODEL_PLANNING": "adopt:B"})["id"] == "B"
    # 台账指向不存在的方案（旧运行重做后 id 变了）：回到推荐案而不是炸
    assert chosen_plan(PLANNING_OK, {"MODEL_PLANNING": "adopt:Z"})["id"] == "A"


# -- shared fixtures for the downstream stages --------------------------------


PREPARATION_OK = {
    "profile_summary": "合成销量数据共 3 个数据集，质量良好，可直接用于建模",
    "datasets": [
        {
            "name": "历史销量",
            "source": "需构造",
            "fields": ["date 日期", "sales 销量（件）"],
            "quality_risks": ["节假日效应未标注"],
        }
    ],
    "preparation_steps": ["构造合成数据", "划分训练/验证集"],
    "missing_value_strategy": "线性插值",
    "outlier_strategy": "3σ 截断",
    "derived_features": ["7 日移动平均"],
}

EXPERIMENT_CODE = "print('OMM_METRICS_JSON: {\"rmse\": 0.12}')"

#: 沙盒执行体的终答（summary + 节点声明的两个叙事键）。
EXPERIMENT_FINAL = {
    "summary": "贪心近似跑通，rmse 0.12 优于随机基线 0.31",
    "approach_summary": "构造泊松需求数据，MILP 简化为贪心近似并与随机基线对比",
    "progress_note": "实验代码已跑通，rmse 0.12 明显优于随机基线，下一步检验对需求率的敏感性。",
}

VALIDATION_OK = {
    "verdict": "concerns",
    "checks": [
        {"name": "结果合理性", "result": "pass", "note": "数量级符合常识"},
        {"name": "稳健性", "result": "warn", "note": "对需求率参数敏感"},
    ],
    "risks": ["合成数据外推风险"],
    "validation_summary": "结果整体可信，但对需求率参数敏感，结论需谨慎外推",
}

PAPER_OK = {
    "title": "基于整数规划的门店选址优化",
    "abstract": "本文建立整数规划模型……",
    "keywords": ["整数规划", "选址"],
    "sections": [
        {"heading": "问题重述", "content": "题目要求……"},
        {"heading": "模型检验", "content": "结果对需求率参数敏感……"},
    ],
}

PAPER_OUTLINE_OK = {
    "title": "基于整数规划的门店选址优化",
    "keywords": ["整数规划", "选址"],
    "notation": "| 符号 | 含义 | 单位 |\n| --- | --- | --- |\n| $x_i$ | 是否在点 i 选址 | 0/1 |",
    "chapters": [
        {
            "heading": "1 问题重述",
            "brief": "背景与逐条任务要求",
            "target_chars": 600,
            "source_keys": ["problem_analysis"],
        },
        {
            "heading": "2 模型建立与求解",
            "brief": "整数规划模型构建与求解，引用 rmse=0.12",
            "target_chars": 1200,
            "source_keys": ["chosen_plan", "experiment_summary"],
        },
        # 故意不带 source_keys：材料路由应回落到全量四份材料
        {"heading": "3 结果分析与检验", "brief": "指标对比与检验结论", "target_chars": 800},
    ],
}

PAPER_FINALIZE_OK = {
    "abstract": "本文建立整数规划模型，rmse=0.12，结论对需求率参数敏感。",
    "keywords": ["整数规划", "选址", "0-1 规划"],
    "progress_note": "论文已按三章完成，可在论文页查看与导出。",
}


def multipass_paper_stub(finalize=None):
    """三段式论文管线的脚本化 LLM：章节回复按 chapter_heading 生成，便于断言顺序。

    正文填充到本章目标字数：达标稿不触发字数有界重写，调用序列保持确定。
    """

    def section_reply(variables):
        lead = f"围绕 rmse=0.12 展开的正文。（{variables['chapter_heading']}）"
        target = int(variables["target_chars"])
        return stub_response({
            "content": lead + "析" * max(target - len(lead), 0),
            "digest": f"{variables['chapter_heading']}摘要",
        })

    return StubLlmPort({
        "paper_outline.default": stub_response(PAPER_OUTLINE_OK),
        "paper_section.default": section_reply,
        "paper_finalize.default": stub_response(finalize or PAPER_FINALIZE_OK),
    })


class FakeToolInvoker:
    """Scripted ToolInvoker: returns queued results (repeats the last one)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def invoke(self, run_id, step_id, tool_name, arguments):
        self.calls.append((run_id, step_id, tool_name, dict(arguments)))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def artifact(name="results.csv"):
    return ArtifactRef(
        artifact_id="art_0001",
        kind="table",
        uri=f"local://deadbeef/{name}",
        sha256="deadbeef",
        size=42,
        media_type="text/csv",
        producer_step="step_1",
    )


def tool_success(stdout='OMM_METRICS_JSON: {"rmse": 0.12}\n', artifacts=()):
    return ToolResult(
        status="succeeded",
        output={"exit_code": 0, "stdout": stdout, "stderr": ""},
        artifacts=tuple(artifacts),
    )


def tool_failure(stderr="Traceback: NameError: x is not defined"):
    return ToolResult(
        status="failed",
        error="python exited with code 1",
        output={"exit_code": 1, "stdout": "", "stderr": stderr},
    )


# -- 沙盒执行体的会话脚本（文本协议） -------------------------------------------


def tool_envelope(name, **arguments):
    """模型侧的工具信封：适配器据此合成 ToolCall。"""
    return json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)


def _saw_observation(messages):
    return any("[工具执行结果]" in message["content"] for message in messages)


def sandbox_script(final, code=EXPERIMENT_CODE):
    """一波会话：先发 python_run 信封，收到观察后给终答。

    按会话内容而非调用序号判断，所以同一份脚本能原样服务多波修复——每波
    都是全新装配的内环（观察清零），不必为波数手工排队。
    """

    def reply(messages):
        if _saw_observation(messages):
            return stub_response(final)
        return tool_envelope(PYTHON_TOOL_NAME, code=code)

    return [reply]


class CompleteOnlyPort:
    """只有模板式 complete 的端口（没有会话扩展）：沙盒执行体的装配缺陷面。"""

    def __init__(self, responses=None):
        self._responses = dict(responses or {})
        self.calls: list[LlmCall] = []

    def complete(self, prompt_id, variables):
        self.calls.append(LlmCall(prompt_id=prompt_id, variables=dict(variables)))
        return self._responses[prompt_id]


class SandboxToolInvoker:
    """沙盒节点的假工具面：python_run 走队列，ws_list/env_probe/ws_read 固定应答。

    ``files_after_run`` 模拟脚本真的产出了文件——清洗断言看的是工作区清单，
    先跑后有才是诚实的时序。``ws_write`` 写进的文件随后对 ws_list / ws_read
    可见（实验节点落 experiment.py、验证节点再读它，走的就是这条链）。
    """

    def __init__(self, runs, files=(), files_after_run=(), texts=None, profiles=None):
        self._runs = list(runs)
        self._files = list(files)
        self._files_after_run = list(files_after_run)
        self._texts = dict(texts or {})
        self._profiles = dict(profiles or {})
        self._ran = False
        self.calls: list[tuple[str, dict]] = []
        self.python_calls: list[tuple[str, str, str, dict]] = []
        self.written: dict[str, str] = {}

    def invoke(self, run_id, step_id, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == PYTHON_TOOL_NAME:
            self.python_calls.append((run_id, step_id, tool_name, dict(arguments)))
            self._ran = True
            return self._runs.pop(0) if len(self._runs) > 1 else self._runs[0]
        if tool_name == "ws_write":
            path, text = arguments["path"], arguments["text"]
            self.written[path] = text
            self._texts[path] = text
            if path not in self._files:
                self._files.append(path)
            return ToolResult(
                status="succeeded",
                output={"path": path, "bytes": len(text.encode("utf-8"))},
            )
        if tool_name == "ws_list":
            files = self._files + (self._files_after_run if self._ran else [])
            prefix = str(arguments.get("prefix") or "")
            if prefix:
                files = [name for name in files if name.startswith(prefix)]
            return ToolResult(status="succeeded", output={"files": files})
        if tool_name == "env_probe":
            return ToolResult(
                status="succeeded",
                output={
                    "runtime": "python",
                    "version": "3.12.0",
                    "deps_hash": "sha256:deadbeef",
                },
            )
        if tool_name == "ws_read":
            path = arguments["path"]
            if path not in self._texts:
                return ToolResult(status="failed", error=f"no such file: {path}")
            return ToolResult(status="succeeded", output={"text": self._texts[path]})
        if tool_name == "table_profile":
            profile = self._profiles.get(arguments["path"])
            if profile is None:
                return ToolResult(status="failed", error="not scripted in this test")
            return ToolResult(status="succeeded", output=profile)
        raise AssertionError(f"unexpected tool call: {tool_name}")


def prior_through_planning():
    return {
        TaskState.PROBLEM_ANALYSIS.value: dict(ANALYSIS_OK),
        TaskState.DATA_PREPARATION.value: dict(PREPARATION_OK),
        TaskState.MODEL_PLANNING.value: dict(PLANNING_OK),
    }


def make_full_services(llm, tools=None):
    return NodeServices(
        clock=FixedClock(),
        ids=SequentialIdGenerator(),
        artifacts=InMemoryArtifactStore(),
        llm=llm,
        tools=tools,
    )


# -- chosen_plan ---------------------------------------------------------------


def test_chosen_plan_prefers_recommended_then_first():
    assert chosen_plan(PLANNING_OK)["id"] == "A"
    assert chosen_plan(dict(PLANNING_OK, recommended_plan_id="missing"))["id"] == "A"
    reordered = dict(PLANNING_OK, recommended_plan_id="B")
    assert chosen_plan(reordered)["id"] == "B"
    with pytest.raises(KeyError):
        chosen_plan({"plans": []})


# -- DataPreparationNode -------------------------------------------------------


def test_data_preparation_happy_path(registry):
    llm = StubLlmPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    node = DataPreparationNode(registry)
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert "合成销量数据" in result.outputs["profile_summary"]
    # ModelPlanningNode reads DATA_PREPARATION.profile_summary downstream.
    sent = llm.calls[0].variables["problem_analysis"]
    assert "泊松分布" in sent
    assert llm.calls[0].variables["attachments_summary"] == "无"


class RoutingToolInvoker:
    """按工具名路由的假执行器（数据阶段画像前置用）。"""

    def __init__(self, responses):
        self._responses = dict(responses)
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, run_id, step_id, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        return self._responses[tool_name]


def test_data_preparation_profiles_workspace_tables(registry):
    """工作区有 CSV：确定性画像先行，画像数字进提示词（原则 5 的数据阶段落点）。"""
    llm = StubLlmPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    tools = RoutingToolInvoker({
        "ws_list": ToolResult(
            status="succeeded",
            output={"files": ["data/orders.csv", "data/readme.txt"]},
        ),
        "table_profile": ToolResult(
            status="succeeded",
            output={
                "path": "data/orders.csv",
                "rows": 4,
                "columns": [
                    {"name": "volume", "type": "float", "missing": 1, "mean": 133.6667}
                ],
                "truncated": False,
            },
        ),
    })
    node = DataPreparationNode(registry)
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm, tools=tools))

    assert result.status == NodeResult.SUCCEEDED
    summary = llm.calls[0].variables["attachments_summary"]
    assert "确定性画像" in summary
    assert "133.6667" in summary, "画像统计数字必须原样进入提示词"
    profiled = [args["path"] for tool, args in tools.calls if tool == "table_profile"]
    assert profiled == ["data/orders.csv"], "只画像 CSV，readme.txt 不进画像"


def test_data_preparation_falls_back_when_listing_fails(registry):
    llm = StubLlmPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    tools = RoutingToolInvoker({
        "ws_list": ToolResult(status="failed", error="workspace unavailable"),
    })
    node = DataPreparationNode(registry)
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = node.run(ctx, make_services(llm, tools=tools))

    assert result.status == NodeResult.SUCCEEDED
    assert llm.calls[0].variables["attachments_summary"] == "无", "画像缺席如实退回摘要路径"


def test_data_preparation_requires_prior_analysis(registry):
    llm = StubLlmPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    node = DataPreparationNode(registry)
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior={})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


# -- 数据准备：清洗沙盒执行 + G2 数据闸门 ---------------------------------------


CLEANING_CODE = (
    "print('OMM_METRICS_JSON: "
    '{"rows_before": 1000, "rows_after": 995, "imputed_columns": ["volume"]}\')'
)


def cleaning_metrics(rows_before=1000, rows_after=995, imputed=("volume",)):
    return json.dumps(
        {
            "rows_before": rows_before,
            "rows_after": rows_after,
            "imputed_columns": list(imputed),
        },
        ensure_ascii=False,
    )


def cleaning_stdout(**kwargs):
    return f"OMM_METRICS_JSON: {cleaning_metrics(**kwargs)}\n"


def cleaning_llm(plan=None, summary="按方案清洗完成", sandbox=None, review=None):
    """数据阶段的三通道端口：模板调用出方案，会话调用跑清洗，再一路会话给审稿人。

    审稿人缺省直接接受（意见照录）；要演驳回 / 僵持的用例自带 ``review`` 脚本。
    """
    return StubLlmPort(
        {"data_preparation.default": stub_response(plan or PREPARATION_OK)},
        chat_scripts={
            CLEANING_PROMPT_ID: sandbox
            if sandbox is not None
            else sandbox_script({"summary": summary}, code=CLEANING_CODE),
            DataPreparationNode.review_prompt_id: (
                review if review is not None else [stub_response(REVIEW_ACCEPT)]
            ),
        },
    )


def cleaning_services(llm, tools, audits=None, governor=None):
    services = make_services(llm, tools)
    services.extras["subagents"] = SubagentSupervisor(
        audit=audits.append if audits is not None else None
    )
    if governor is not None:
        services.extras["budget_governor"] = governor
    return services


def cleaning_tools(stdout=None, runs=None, **kwargs):
    return SandboxToolInvoker(
        runs=list(runs) if runs is not None else [tool_success(stdout=stdout or cleaning_stdout(**kwargs))],
        files=["data/orders.csv"],
        files_after_run=["cleaned/orders.csv"],
    )


def test_data_preparation_dispatches_cleaning_sandbox_and_auto_adopts_small_impact():
    """删行 0.5%、无目标列插补 → 不惊动用户，自动采用清洗结果。"""
    registry = load_default_registry()
    llm = cleaning_llm()
    tools = cleaning_tools()
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    cleaning = result.outputs["cleaning"]
    assert cleaning["executed"] is True
    assert cleaning["status"] == "passed"
    # 影响面数字来自清洗脚本的标记行，节点只做除法与求交
    assert cleaning["rows_before"] == 1000
    assert cleaning["rows_after"] == 995
    assert cleaning["rows_deleted_ratio"] == 0.005
    assert cleaning["imputed_columns"] == ["volume"]
    assert cleaning["imputed_target_columns"] == []
    assert cleaning["summary"] == "按方案清洗完成"
    # 清洗脚本本身发布为可复现产物
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["cleaning.py"]
    # 方案（而非全对话）进入清洗任务卡
    card = system_prompt_of(llm.chat_calls[0])
    assert "线性插值" in card and "3σ 截断" in card
    assert "- data/orders.csv" in card


def test_g2_gate_triggers_on_heavy_row_deletion():
    registry = load_default_registry()
    llm = cleaning_llm()
    tools = cleaning_tools(rows_before=1000, rows_after=900, imputed=())
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "10.0%" in result.review_reason
    meta = result.review_meta
    assert meta["gate"] == "G2"
    assert [option["id"] for option in meta["options"]] == [
        "adopt_cleaned",
        "use_raw",
        "reject",
    ]
    # 单 CTA 的前端要能落到一个确定动作：推荐项必须显式标出
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["adopt_cleaned"]
    assert meta["impact"]["rows_deleted_ratio"] == 0.1
    # 闸门未拍板前，清洗结论已随 STEP_SUCCEEDED 的 outputs 落库
    assert result.outputs["cleaning"]["executed"] is True


def test_g2_gate_triggers_on_target_column_imputation():
    """删行极少但目标列被插补：建模标签被改写，必须请人确认。"""
    registry = load_default_registry()
    plan = dict(PREPARATION_OK, target_columns=["Sales"])
    llm = cleaning_llm(plan=plan)
    tools = cleaning_tools(rows_before=1000, rows_after=1000, imputed=("sales",))
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "目标列被插补" in result.review_reason
    # 列名大小写不该决定是否上闸门
    assert result.outputs["cleaning"]["imputed_target_columns"] == ["sales"]


def test_no_g2_gate_when_imputed_column_is_not_a_target():
    registry = load_default_registry()
    plan = dict(PREPARATION_OK, target_columns=["sales"])
    llm = cleaning_llm(plan=plan)
    tools = cleaning_tools(rows_before=1000, rows_after=1000, imputed=("temperature",))
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED


@pytest.mark.parametrize(
    "kwargs, reason_fragment",
    [
        ({"supervisor": False}, "子代理监督者"),
        ({"chat": False}, "会话式调用"),
        ({"files": False}, "没有已下发的数据文件"),
        ({"tools": False}, "未配置工具端口"),
    ],
)
def test_cleaning_degrades_honestly_when_a_precondition_is_missing(kwargs, reason_fragment):
    """清洗是尽力而为的增强：装配缺项如实标注 executed=false，绝不假装跑过。"""
    registry = load_default_registry()
    llm = (
        cleaning_llm()
        if kwargs.get("chat", True)
        else CompleteOnlyPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    )
    tools = (
        cleaning_tools() if kwargs.get("files", True) else SandboxToolInvoker(runs=[tool_success()])
    )
    services = make_services(llm, tools if kwargs.get("tools", True) else None)
    if kwargs.get("supervisor", True):
        services.extras["subagents"] = SubagentSupervisor()
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED, "清洗缺席不阻塞数据阶段"
    assert result.outputs["profile_summary"]
    assert result.outputs["cleaning"]["executed"] is False
    assert reason_fragment in result.outputs["cleaning"]["reason"]


def test_cleaning_failure_does_not_block_the_stage_and_skips_g2():
    """清洗真跑但没跑成：如实记 failed，后续按原始数据继续，不上闸门。"""
    registry = load_default_registry()
    llm = cleaning_llm()
    tools = SandboxToolInvoker(
        runs=[tool_failure(stderr="ValueError: could not convert string to float")],
        files=["data/orders.csv"],
    )
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    cleaning = result.outputs["cleaning"]
    assert cleaning["executed"] is True
    assert cleaning["status"] == "failed"
    assert cleaning["rows_before"] == 0
    assert "review" not in cleaning, "没过验收的清洗不进审稿环"
    assert len(tools.python_calls) == 3, "三波修复各一次运行，没有额外的复跑"


# -- 清洗的生成者-评审者（§8.4）：复跑核对 → reviewer 子代理 → 驳回退 R2 → 僵持进 G2 ----

CLEANING_CODE_V2 = CLEANING_CODE.replace("995", "996")

CLEANING_REVIEW_REJECT = {
    "verdict": "reject",
    "findings": [
        {
            "id": "R1",
            "severity": "blocker",
            "location": "impact stats",
            "issue": "rows_after 是写死的常量，不是清洗后的计数",
            "fix_hint": "用清洗后 DataFrame 的长度计数",
        },
        {"id": "R2", "severity": "minor", "issue": "列名未规范化"},
    ],
    "summary": "影响面统计不可信。",
}


def test_cleaning_review_reruns_deterministically_then_spawns_an_accepting_reviewer():
    registry = load_default_registry()
    audits = []
    llm = cleaning_llm()
    tools = cleaning_tools()
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools, audits))

    assert result.status == NodeResult.SUCCEEDED, "审稿通过且影响面小：不惊动用户"
    # 复跑核对是节点自己做的：同一份清洗脚本再 python_run 一次
    assert [call[3]["code"] for call in tools.python_calls] == [CLEANING_CODE, CLEANING_CODE]
    cleaning = result.outputs["cleaning"]
    review = cleaning["review"]
    assert review["executed"] is True
    assert review["verdict"] == "accept" and review["rounds"] == 1 and review["stalemate"] is False
    assert review["rerun"] == {
        "executed": True,
        "consistent": True,
        "metrics": {"rows_before": 1000, "rows_after": 995, "imputed_columns": ["volume"]},
        "diff": [],
        "reason": "",
    }
    assert cleaning["llm_calls"] == 3, "两次生成者 + 一次审稿"
    assert cleaning["attempts"] == 1
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["cleaning.py"]
    # reviewer 子代理：独立上下文、只读工具、不接知识工具
    spawns = [entry for entry in audits if entry["phase"] == "spawn"]
    assert [entry["tool"] for entry in spawns] == ["subagent:sandbox", "subagent:reviewer"]
    reviewer = spawns[1]
    assert reviewer["tool_tier"] == "readonly"
    assert reviewer["toolset"] == ["ws_read", "ws_list"]
    assert reviewer["output_schema_id"] == "cleaning-review.v1"
    review_call = next(c for c in llm.chat_calls if c.label == DataPreparationNode.review_prompt_id)
    card = system_prompt_of(review_call)
    assert "数据清洗审稿人" in card and CLEANING_CODE in card
    assert "线性插值" in card and "- data/orders.csv" in card, "审稿人拿准备方案与数据文件表"
    assert '"rows_deleted_ratio": 0.005' in card
    assert "核心指标与首跑逐键一致" in card
    assert "按方案清洗完成" in card
    assert "- cleaned/orders.csv" in card, "工作区清单里看得到清洗产物"
    opening = user_prompt_of(review_call)
    assert "- ws_read：" in opening and "- ws_list：" in opening
    assert "python_run" not in opening and "knowledge_search" not in opening
    assert "缺失值 / 异常值处理是否按方案" in opening


def test_cleaning_reviewer_rejection_sends_the_generator_back_to_r2_then_re_reviews():
    registry = load_default_registry()
    llm = cleaning_llm(
        sandbox=repairing_sandbox_script(
            {"summary": "按审稿意见改为真实计数"},
            first_code=CLEANING_CODE,
            repaired_code=CLEANING_CODE_V2,
        ),
        review=reviewer_script((CLEANING_CODE_V2, REVIEW_ACCEPT), (CLEANING_CODE, CLEANING_REVIEW_REJECT)),
    )
    # 首跑 / 复跑报 995；修复波起报 996（队列尾项复用）
    tools = cleaning_tools(runs=[
        tool_success(stdout=cleaning_stdout()),
        tool_success(stdout=cleaning_stdout()),
        tool_success(stdout=cleaning_stdout(rows_after=996)),
    ])
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert [call[3]["code"] for call in tools.python_calls] == [
        CLEANING_CODE, CLEANING_CODE, CLEANING_CODE_V2, CLEANING_CODE_V2,
    ]
    cleaning = result.outputs["cleaning"]
    assert cleaning["review"]["verdict"] == "accept" and cleaning["review"]["rounds"] == 2
    # 最终采用修复波的结果：影响面按修复后的标记行重算
    assert cleaning["rows_after"] == 996 and cleaning["rows_deleted_ratio"] == 0.004
    assert cleaning["summary"] == "按审稿意见改为真实计数"
    assert cleaning["attempts"] == 2 and cleaning["llm_calls"] == 6
    repair_wave = next(
        user_prompt_of(c)
        for c in llm.chat_calls
        if c.label == CLEANING_PROMPT_ID and "审稿驳回意见" in user_prompt_of(c)
    )
    assert "[R1｜blocker] impact stats：rows_after 是写死的常量" in repair_wave
    assert "（修法：用清洗后 DataFrame 的长度计数）" in repair_wave
    # 修复波发布的脚本取代首波的（旧版在各波报告里可追溯，不重复列产物）
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["cleaning.py"]
    assert result.outputs["cleaning"]["final_code_artifact"] == result.artifacts[0].artifact_id


def test_cleaning_review_stalemate_opens_g2_and_recommends_raw_data():
    """审稿两轮仍有 blocker：不信清洗产物 → G2 挂门，推荐改用原始数据。"""
    registry = load_default_registry()
    llm = cleaning_llm(
        sandbox=repairing_sandbox_script(
            {"summary": "按方案清洗完成"}, first_code=CLEANING_CODE, repaired_code=CLEANING_CODE_V2
        ),
        review=[stub_response(CLEANING_REVIEW_REJECT)],
    )
    tools = cleaning_tools()
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    review = result.outputs["cleaning"]["review"]
    assert review["stalemate"] is True and review["rounds"] == 2
    assert review["reason"] == "审稿 2 轮后仍有阻断性意见未解决"
    assert result.review_reason == (
        "清洗脚本的独立审稿 2 轮后仍有 1 条阻断性意见未解决（审稿 2 轮后仍有阻断性意见未解决）。"
        "请确认数据处理方式"
    )
    meta = result.review_meta
    assert meta["gate"] == "G2"
    assert [option["id"] for option in meta["options"]] == ["adopt_cleaned", "use_raw", "reject"]
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["use_raw"]
    assert meta["impact"]["review_stalemate"] is True
    assert [entry["id"] for entry in meta["impact"]["reviewer_findings"]] == ["R1"]
    assert meta["impact"]["rows_deleted_ratio"] == 0.005, "影响面本身没超阈值"
    assert len(tools.python_calls) == 4, "两轮各一次生成者运行 + 一次复跑"


def test_g2_combines_heavy_impact_with_a_review_stalemate():
    registry = load_default_registry()
    llm = cleaning_llm(review=[stub_response(CLEANING_REVIEW_REJECT)])
    # 单波脚本不会因驳回改代码：修复波复用同一脚本 → 复审仍驳回 → 僵持
    tools = cleaning_tools(rows_before=1000, rows_after=900, imputed=())
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.startswith(
        "数据清洗影响面较大：删除了 10.0% 的数据行（阈值 5%）；清洗脚本的独立审稿 2 轮后"
    )
    assert [o["id"] for o in result.review_meta["options"] if o.get("recommended")] == ["use_raw"]


def test_cleaning_review_skips_the_rerun_when_the_budget_slice_cannot_afford_it():
    """预算切片给到 ≥1 次运行才派清洗；复跑需要再一次，切片不够就如实「未复跑」。

    生产 ToolBus 包装层每次 python_run 预先计费（engine_glue），这里的假工具面照做，
    切片才会像真实运行那样随首跑缩水（4 → 剩 3 → 25% = 0）。
    """
    registry = load_default_registry()
    governor = BudgetGovernor(RunBudget(max_sandbox_runs=4, max_llm_calls=40))

    class ChargingInvoker(SandboxToolInvoker):
        def invoke(self, run_id, step_id, tool_name, arguments):
            if tool_name == PYTHON_TOOL_NAME:
                governor.charge_sandbox_run()
            return super().invoke(run_id, step_id, tool_name, arguments)

    llm = cleaning_llm()
    tools = ChargingInvoker(
        runs=[tool_success(stdout=cleaning_stdout())],
        files=["data/orders.csv"],
        files_after_run=["cleaned/orders.csv"],
    )
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior=prior_with_analysis())

    result = DataPreparationNode(registry).run(ctx, cleaning_services(llm, tools, governor=governor))

    assert result.status == NodeResult.SUCCEEDED
    review = result.outputs["cleaning"]["review"]
    assert review["executed"] is True and review["verdict"] == "accept"
    assert review["rerun"] == {"executed": False, "reason": "剩余预算不足以复跑核对"}
    assert [call[3]["code"] for call in tools.python_calls] == [CLEANING_CODE]
    card = system_prompt_of(next(c for c in llm.chat_calls if c.label == DataPreparationNode.review_prompt_id))
    assert "未复跑：剩余预算不足以复跑核对" in card


# -- ExperimentExecutionNode（沙盒执行体） ---------------------------------------


def experiment_llm(final=None, code=EXPERIMENT_CODE):
    return StubLlmPort(
        {},
        chat_scripts={
            ExperimentExecutionNode.prompt_id: sandbox_script(
                final or EXPERIMENT_FINAL, code=code
            )
        },
    )


def system_prompt_of(chat_call):
    """取一次会话调用的 system 段（沙盒模板渲染的角色卡与任务口径）。"""
    return next(m["content"] for m in chat_call.messages if m["role"] == "system")


def user_prompt_of(chat_call):
    """取一次会话调用的首条 user 段（目标/任务说明/种子/验收/修复反馈）。"""
    return next(m["content"] for m in chat_call.messages if m["role"] == "user")


def test_experiment_runs_code_in_sandbox_and_reports_real_metrics(registry):
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success(artifacts=(artifact(),))])
    node = ExperimentExecutionNode(registry)
    services = make_full_services(llm, tools)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED
    run_id, step_id, tool_name, arguments = tools.python_calls[0]
    assert (run_id, step_id, tool_name) == ("run_1", "step_1", PYTHON_TOOL_NAME)
    assert arguments["code"] == EXPERIMENT_CODE
    # 指标来自脚本真实 stdout 的标记行，不是模型自述
    assert result.outputs["metrics"] == {"rmse": 0.12}
    assert result.outputs["approach_summary"] == EXPERIMENT_FINAL["approach_summary"]
    assert result.outputs["progress_note"] == EXPERIMENT_FINAL["progress_note"]
    assert "核心指标" in result.outputs["experiment_summary"]
    assert "results.csv" in result.outputs["experiment_summary"]
    assert result.metrics == {"llm_attempts": 2, "code_rounds": 1, "waves": 1}
    # 工具产物之外，生成的实验脚本本身也发布为 code 产物（可复现）
    assert [ref.kind for ref in result.artifacts] == ["table", "code"]
    code_ref = result.artifacts[-1]
    assert code_ref.uri.endswith("experiment.py")
    assert services.artifacts.blobs[code_ref.uri].decode("utf-8") == EXPERIMENT_CODE
    # 最终脚本同时落到工作区固定路径：验证阶段据此复跑
    assert tools.written == {EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE}
    assert result.outputs["script_path"] == EXPERIMENT_SCRIPT_PATH


def test_experiment_reports_empty_script_path_when_workspace_write_fails(registry):
    """落工作区失败只影响下游复跑，不影响实验步骤成败，且如实给空路径。"""

    class NoWriteInvoker(SandboxToolInvoker):
        def invoke(self, run_id, step_id, tool_name, arguments):
            if tool_name == "ws_write":
                self.calls.append((tool_name, dict(arguments)))
                return ToolResult(status="failed", error="workspace quota exceeded")
            return super().invoke(run_id, step_id, tool_name, arguments)

    llm = experiment_llm()
    tools = NoWriteInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["script_path"] == ""
    assert result.outputs["metrics"] == {"rmse": 0.12}


def test_experiment_output_carries_sandbox_report_for_replay(registry):
    """新增 sandbox_report：断言逐条结果/种子/环境指纹/预算用量的复现面。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    report = node.run(ctx, make_full_services(llm, tools)).outputs["sandbox_report"]

    assert report["status"] == "passed"
    assert report["seeds"] == {"random_seed": 42}
    assert report["usage"]["runs"] == 1
    assert [item["id"] for item in report["assertions"]] == [
        "run_ok",
        "metrics_reported",
    ]
    assert all(item["passed"] for item in report["assertions"])
    assert report["env_fingerprint"]["deps_hash"] == "sha256:deadbeef"


def test_experiment_passes_detected_packages_and_gpu_note_to_task_card(registry):
    """可用库与硬件口径进入沙盒角色卡（system 段），引导实验代码上 GPU。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    note = gpu_hardware_note("NVIDIA GeForce RTX 4090, 24.0 GB VRAM")
    node = ExperimentExecutionNode(
        registry, available_packages="numpy、pandas", hardware_note=note
    )
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    card = system_prompt_of(llm.chat_calls[0])
    assert "numpy、pandas" in card
    assert "RTX 4090" in card and "检测到可用 GPU" in card
    # GPU 口径必须自带回退纪律，别让生成代码在无 GPU 环境硬编码 cuda 崩掉
    assert "禁止硬编码 cuda" in card
    # 选中的方案（recommended A）进入任务卡
    assert "整数规划" in card


def test_experiment_defaults_stay_cpu_conservative(registry):
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    card = system_prompt_of(llm.chat_calls[0])
    assert DEFAULT_HARDWARE_NOTE in card
    assert "无（仅 Python 标准库）" in card


def test_experiment_lists_workspace_data_files_in_task_card(registry):
    """已下发的数据文件进任务卡：优先读真实数据，而不是一律造合成数据。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(
        runs=[tool_success()],
        files=["data/orders.csv", "cleaned/orders.csv", "notes/readme.md"],
    )
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    card = system_prompt_of(llm.chat_calls[0])
    assert "- data/orders.csv" in card
    assert "- cleaned/orders.csv" in card
    assert "notes/readme.md" not in card, "非数据目录的文件不进任务卡"


def test_experiment_task_card_carries_g2_user_decision(registry):
    """用户在 G2 选了原始数据：决策台账原样进任务卡，模型据此选目录。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = NodeContext(
        run_id="run_1",
        project_id="proj_1",
        state=TaskState.EXPERIMENTING,
        step_id="step_1",
        attempt=1,
        inputs={},
        prior_outputs=prior_through_planning(),
        review_decisions={TaskState.DATA_PREPARATION.value: "use_raw"},
    )

    ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    card = system_prompt_of(llm.chat_calls[0])
    assert "use_raw" in card
    assert "改用原始数据" in card


def test_experiment_repairs_across_waves_when_run_fails(registry):
    """第一波运行失败 → 断言未过 → 带着 stderr 反馈进第二波修复。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_failure(), tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert result.metrics["code_rounds"] == 2
    assert result.metrics["waves"] == 2
    assert len(tools.python_calls) == 2
    # 第二波任务卡必须带上第一波的真实报错与上一轮代码（结构化反馈，不转录全对话）
    second_wave = user_prompt_of(llm.chat_calls[2])
    assert "NameError" in second_wave
    assert EXPERIMENT_CODE in second_wave
    assert "问题分析结果" not in second_wave, "反馈是结构化差异，不是把上一波对话搬过来"


def test_experiment_fails_when_waves_exhausted_and_names_the_failing_assertion(registry):
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_failure()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.FAILED
    assert "3 wave(s)" in result.error
    assert "[run_ok]" in result.error
    assert "NameError" in result.error
    assert len(tools.python_calls) == 3
    assert result.metrics["waves"] == 3


def test_experiment_rejects_self_reported_success_without_running_code(registry):
    """§7.1 硬纪律：模型不跑代码直接交终答，断言不认，最终失败。"""
    llm = StubLlmPort(
        {},
        chat_scripts={
            ExperimentExecutionNode.prompt_id: [stub_response(EXPERIMENT_FINAL)]
        },
    )
    tools = SandboxToolInvoker(runs=[tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.FAILED
    assert "尚未用 python_run 运行任何代码" in result.error
    assert tools.python_calls == []


def test_experiment_without_chat_capable_port_fails_cleanly(registry):
    """装配缺陷（端口没有会话能力）必须明说，不能退回旧的单发模板路径。"""

    class CompleteOnlyPort:
        def complete(self, prompt_id, variables):
            return "{}"

    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(
        ctx, make_full_services(CompleteOnlyPort(), SandboxToolInvoker(runs=[tool_success()]))
    )

    assert result.status == NodeResult.FAILED
    assert "chat_text" in result.error


def test_experiment_without_tool_invoker_fails_cleanly(registry):
    llm = experiment_llm()
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools=None))

    assert result.status == NodeResult.FAILED
    assert "no tool invoker" in result.error
    assert llm.chat_calls == []


def test_experiment_requires_prior_planning(registry):
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_with_analysis())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert tools.python_calls == []


# -- 生成者-评审者（§8.4）：确定性复跑核对 → reviewer 子代理 → 驳回退 R2 → 僵持进 G3 ----

EXPERIMENT_CODE_V2 = EXPERIMENT_CODE.replace("rmse", "rmse_v2")

REVIEW_ACCEPT = {
    "verdict": "accept",
    "findings": [
        {
            "id": "R1",
            "severity": "minor",
            "location": "metrics",
            "issue": "只报了 rmse 一个口径",
            "fix_hint": "论文阶段补 MAE",
        }
    ],
    "summary": "实现忠实于方案，复跑一致，可作为后续检验依据。",
}

REVIEW_REJECT = {
    "verdict": "reject",
    "findings": [
        {
            "id": "R1",
            "severity": "blocker",
            "location": "baseline",
            "issue": "基线与模型用了不同的评估切分，rmse 不可比",
            "fix_hint": "基线改用同一份验证集",
        },
        {"id": "R2", "severity": "minor", "issue": "变量命名未按符号表"},
    ],
    "summary": "基线口径不同，结论暂不可信。",
}


def review_services(llm, tools, audits=None, governor=None):
    services = make_full_services(llm, tools)
    services.extras["subagents"] = SubagentSupervisor(
        audit=audits.append if audits is not None else None
    )
    if governor is not None:
        services.extras["budget_governor"] = governor
    return services


def repairing_sandbox_script(final, first_code=EXPERIMENT_CODE, repaired_code=EXPERIMENT_CODE_V2):
    """生成者会话：首波交 v1；看到「审稿驳回意见」的修复波交 v2。"""

    def reply(messages):
        if _saw_observation(messages):
            return stub_response(final)
        repairing = any("审稿驳回意见" in message["content"] for message in messages)
        return tool_envelope(PYTHON_TOOL_NAME, code=repaired_code if repairing else first_code)

    return [reply]


def reviewer_script(*verdicts_by_code):
    """审稿人会话：按 system 段里出现的脚本正文决定结论（独立上下文只认材料）。"""
    table = list(verdicts_by_code)

    def reply(messages):
        system = next(m["content"] for m in messages if m["role"] == "system")
        for code, verdict in table:
            if code in system:
                return stub_response(verdict)
        raise AssertionError("reviewer card carries an unexpected script body")

    return [reply]


def review_llm(sandbox_script_entries, reviewer_entries):
    return StubLlmPort(
        {},
        chat_scripts={
            ExperimentExecutionNode.prompt_id: sandbox_script_entries,
            ExperimentExecutionNode.review_prompt_id: reviewer_entries,
        },
    )


def test_experiment_review_reruns_deterministically_then_spawns_an_accepting_reviewer(registry):
    audits = []
    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), [stub_response(REVIEW_ACCEPT)])
    tools = SandboxToolInvoker(runs=[tool_success(artifacts=(artifact(),))], files=["data/orders.csv"])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools, audits))

    assert result.status == NodeResult.SUCCEEDED
    # 复跑核对是节点自己做的：同一份脚本再 python_run 一次，不占生成者的 R2 运行数
    assert [call[3]["code"] for call in tools.python_calls] == [EXPERIMENT_CODE, EXPERIMENT_CODE]
    review = result.outputs["review"]
    assert review["executed"] is True
    assert review["verdict"] == "accept" and review["rounds"] == 1 and review["blockers"] == 0
    assert review["stalemate"] is False
    assert review["rerun"] == {
        "executed": True,
        "consistent": True,
        "metrics": {"rmse": 0.12},
        "diff": [],
        "reason": "",
    }
    assert [entry["id"] for entry in review["findings"]] == ["R1"]
    assert result.metrics == {"llm_attempts": 3, "code_rounds": 1, "waves": 1, "review_rounds": 1}
    assert "独立审稿通过（1 轮，1 条意见）" in result.outputs["experiment_summary"]
    # 首跑的结论原样保留，脚本照常落工作区
    assert result.outputs["metrics"] == {"rmse": 0.12}
    assert tools.written == {EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE}
    assert [ref.kind for ref in result.artifacts] == ["table", "code"]
    # 审稿人是独立上下文的 reviewer 子代理：只读工具、不给 python_run；审计带 toolset
    spawn = next(entry for entry in audits if entry["phase"] == "spawn")
    assert spawn["tool"] == "subagent:reviewer"
    assert spawn["tool_tier"] == "readonly"
    assert spawn["toolset"] == ["ws_read", "ws_list"]
    assert spawn["output_schema_id"] == "experiment-review.v1"
    review_call = next(c for c in llm.chat_calls if c.label == ExperimentExecutionNode.review_prompt_id)
    card = system_prompt_of(review_call)
    assert "实验审稿人" in card and EXPERIMENT_CODE in card
    assert '"rmse": 0.12' in card
    assert "核心指标与首跑逐键一致" in card
    assert EXPERIMENT_FINAL["approach_summary"] in card
    assert "- data/orders.csv" in card
    assert "整数规划" in card, "审稿人拿到的是选中方案的结构化切片"
    opening = user_prompt_of(review_call)
    assert "- ws_read：" in opening and "- ws_list：" in opening
    assert "python_run" not in opening and "knowledge_search" not in opening
    assert "至多 3 次" in opening


def test_experiment_reviewer_gets_knowledge_tools_when_the_port_is_wired(registry):
    class KnowledgeAwareInvoker(SandboxToolInvoker):
        def invoke(self, run_id, step_id, tool_name, arguments):
            if tool_name == "knowledge_search":
                self.calls.append((tool_name, dict(arguments)))
                return ToolResult(status="succeeded", output={"hits": [], "reason": "知识库不可用"})
            return super().invoke(run_id, step_id, tool_name, arguments)

    def reviewer(messages):
        if _saw_observation(messages):
            return stub_response(REVIEW_ACCEPT)
        return tool_envelope("knowledge_search", query="整数规划 基线 口径", kind="paper")

    audits = []
    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), [reviewer])
    tools = KnowledgeAwareInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry, knowledge=object()).run(
        ctx, review_services(llm, tools, audits)
    )

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["review"]["verdict"] == "accept"
    spawn = next(entry for entry in audits if entry["phase"] == "spawn")
    assert spawn["toolset"] == ["ws_read", "ws_list", "knowledge_search", "knowledge_read"]
    assert ("knowledge_search", {"query": "整数规划 基线 口径", "kind": "paper"}) in tools.calls
    review_calls = [c for c in llm.chat_calls if c.label == ExperimentExecutionNode.review_prompt_id]
    assert len(review_calls) == 2, "一轮检索 + 一轮终答"
    assert "knowledge_search" in user_prompt_of(review_calls[0])
    assert result.metrics["llm_attempts"] == 4


def test_experiment_reviewer_rejection_sends_the_generator_back_to_r2_then_re_reviews(registry):
    llm = review_llm(
        repairing_sandbox_script(EXPERIMENT_FINAL),
        reviewer_script((EXPERIMENT_CODE_V2, REVIEW_ACCEPT), (EXPERIMENT_CODE, REVIEW_REJECT)),
    )
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    # 首跑 v1 → 复跑 v1 → 驳回 → 修复波 v2 → 复跑 v2 → 复审接受
    assert [call[3]["code"] for call in tools.python_calls] == [
        EXPERIMENT_CODE, EXPERIMENT_CODE, EXPERIMENT_CODE_V2, EXPERIMENT_CODE_V2,
    ]
    review = result.outputs["review"]
    assert review["verdict"] == "accept" and review["rounds"] == 2 and review["stalemate"] is False
    assert [entry["id"] for entry in review["findings"]] == ["R1"], "记录的是复审那一轮的意见"
    # 修复波的任务说明带着逐条驳回意见（结构化，不转录审稿对话），生成者必须处理
    repair_wave = next(
        user_prompt_of(c)
        for c in llm.chat_calls
        if c.label == ExperimentExecutionNode.prompt_id and "审稿驳回意见" in user_prompt_of(c)
    )
    assert "[R1｜blocker] baseline：基线与模型用了不同的评估切分" in repair_wave
    assert "（修法：基线改用同一份验证集）" in repair_wave
    assert "[R2｜minor]" in repair_wave
    assert "审稿总结：基线口径不同" in repair_wave
    # 最终以修复后的脚本为准
    assert tools.written == {EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE_V2}
    assert result.outputs["sandbox_report"]["status"] == "passed"
    assert result.metrics["code_rounds"] == 2 and result.metrics["waves"] == 2
    assert result.metrics["review_rounds"] == 2
    assert result.metrics["llm_attempts"] == 6, "两波生成者各 2 次 + 两轮审稿各 1 次"


def test_experiment_review_stalemate_after_max_rounds_keeps_the_result_and_flags_it(registry):
    llm = review_llm(repairing_sandbox_script(EXPERIMENT_FINAL), [stub_response(REVIEW_REJECT)])
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED, "僵持不是失败：已验收结果保留，交 G3 裁定"
    review = result.outputs["review"]
    assert review["verdict"] == "reject" and review["rounds"] == 2
    assert review["stalemate"] is True
    assert review["reason"] == "审稿 2 轮后仍有阻断性意见未解决"
    assert review["blockers"] == 1
    assert len(tools.python_calls) == 4, "两轮各一次生成者运行 + 一次复跑"
    assert tools.written == {EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE_V2}
    assert "仍有 1 条阻断性意见未解决" in result.outputs["experiment_summary"]


def test_experiment_review_stalemate_when_the_repair_wave_fails_keeps_the_first_accepted_result(registry):
    llm = review_llm(repairing_sandbox_script(EXPERIMENT_FINAL), [stub_response(REVIEW_REJECT)])
    # 首跑成功、复跑成功、修复波每次运行都失败（队列尾项复用）
    tools = SandboxToolInvoker(runs=[tool_success(), tool_success(), tool_failure()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    review = result.outputs["review"]
    assert review["stalemate"] is True
    assert review["reason"] == "按审稿意见的修复波未通过验收，保留已验收结果"
    assert review["rounds"] == 1
    # 首轮已验收的代码与指标是最终结果，修复波的失败不覆盖它
    assert result.outputs["metrics"] == {"rmse": 0.12}
    assert tools.written == {EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE}
    assert result.outputs["sandbox_report"]["status"] == "passed"
    # 修复波用掉的运行数计入 R2 账（1 首跑 + 3 波修复各 1 次）
    assert result.metrics["code_rounds"] == 4 and result.metrics["waves"] == 4


def test_experiment_review_rejects_without_a_blocker_count_as_accept(registry):
    soft_reject = {
        "verdict": "reject",
        "findings": [{"id": "R1", "severity": "major", "issue": "基线太弱"}],
        "summary": "建议换更强的基线",
    }
    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), [stub_response(soft_reject)])
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    review = result.outputs["review"]
    assert review["verdict"] == "accept" and review["blockers"] == 0
    assert review["findings"][0]["severity"] == "major", "意见照录，只是拿不到否决权"
    assert len(tools.python_calls) == 2, "没有修复波"


def test_experiment_review_reports_an_inconsistent_rerun_to_the_reviewer(registry):
    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), [stub_response(REVIEW_ACCEPT)])
    tools = SandboxToolInvoker(
        runs=[tool_success(), tool_success(stdout='OMM_METRICS_JSON: {"rmse": 0.3}\n')]
    )
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    rerun = result.outputs["review"]["rerun"]
    assert rerun["consistent"] is False
    assert rerun["diff"] == ["rmse：首跑 0.12，复跑 0.3"]
    card = system_prompt_of(
        next(c for c in llm.chat_calls if c.label == ExperimentExecutionNode.review_prompt_id)
    )
    assert "与首跑不一致" in card and "rmse：首跑 0.12，复跑 0.3" in card


def test_experiment_review_skips_the_rerun_when_the_budget_slice_cannot_afford_it(registry):
    from omm_agent_harness import BudgetGovernor, RunBudget

    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), [stub_response(REVIEW_ACCEPT)])
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())
    governor = BudgetGovernor(RunBudget(max_sandbox_runs=2))  # 25% 切片 = 0 次运行

    result = ExperimentExecutionNode(registry).run(
        ctx, review_services(llm, tools, governor=governor)
    )

    assert len(tools.python_calls) == 1, "没预算就不复跑"
    review = result.outputs["review"]
    assert review["rerun"] == {"executed": False, "reason": "剩余预算不足以复跑核对"}
    assert review["verdict"] == "accept", "审稿照常进行，材料里如实写未复跑"
    card = system_prompt_of(
        next(c for c in llm.chat_calls if c.label == ExperimentExecutionNode.review_prompt_id)
    )
    assert "未复跑：剩余预算不足以复跑核对" in card


def test_experiment_review_is_reported_not_faked_without_a_supervisor(registry):
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    assert result.outputs["review"] == {
        "executed": False,
        "reason": "未配置子代理监督者，跳过独立审稿",
        "llm_calls": 0,
    }
    assert len(tools.python_calls) == 1
    assert "review_rounds" not in result.metrics


def test_experiment_review_survives_a_reviewer_that_never_answers_validly(registry):
    llm = review_llm(sandbox_script(EXPERIMENT_FINAL), ["这不是 JSON"])
    tools = SandboxToolInvoker(runs=[tool_success()])
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = ExperimentExecutionNode(registry).run(ctx, review_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    review = result.outputs["review"]
    assert review["executed"] is False and review["rounds"] == 1
    assert review["reason"].startswith("审稿子代理未完成（failed")
    assert review["rerun"]["consistent"] is True
    assert result.outputs["metrics"] == {"rmse": 0.12}


# -- ValidationNode：LLM 判读 → 稳健性沙盒复跑 → G3 --------------------------------


def validation_prior():
    prior = prior_through_planning()
    prior[TaskState.EXPERIMENTING.value] = {
        "experiment_summary": "贪心近似 rmse=0.12",
        "metrics": {"rmse": 0.12},
        "stdout_tail": "OMM_METRICS_JSON: ...",
        "script_path": EXPERIMENT_SCRIPT_PATH,
    }
    return prior


def robustness_checks(*passed_flags):
    """检验脚本标记行里的 checks：按传入的通过标志生成三类检查。"""
    templates = [
        ("sensitivity_demand", "需求率 ±20% 扰动", 0.05, 0.2),
        ("bootstrap_stability", "bootstrap 重采样稳定性", 0.08, 0.15),
        ("baseline_margin", "对基线优势幅度", 0.6, 0.1),
    ]
    checks = []
    for (check_id, name, value, threshold), passed in zip(templates, passed_flags):
        checks.append(
            {
                "id": check_id,
                "name": name,
                "passed": passed,
                "value": value if passed else value * 5,
                "threshold": threshold,
                "detail": "相对退化在阈值内" if passed else "相对退化超出阈值",
            }
        )
    return checks


def robustness_stdout(*passed_flags, checks=None):
    payload = {"checks": checks if checks is not None else robustness_checks(*passed_flags)}
    return "OMM_METRICS_JSON: " + json.dumps(payload, ensure_ascii=False) + "\n"


ROBUSTNESS_CODE = "print('robustness checks')"
ROBUSTNESS_FINAL = {"summary": "三项稳健性检查按阈值判定完毕"}


def validation_llm(judgement=None, final=None, sandbox=None, review=None):
    """验证阶段的三通道端口：模板调用出判读，会话调用跑检验脚本，再一路会话给审稿人。

    审稿人缺省直接接受（意见照录）；要演驳回 / 僵持的用例自带 ``review`` 脚本。
    """
    return StubLlmPort(
        {"validating.default": stub_response(judgement or VALIDATION_OK)},
        chat_scripts={
            ROBUSTNESS_PROMPT_ID: sandbox
            if sandbox is not None
            else sandbox_script(final or ROBUSTNESS_FINAL, code=ROBUSTNESS_CODE),
            ValidationNode.review_prompt_id: (
                review if review is not None else [stub_response(REVIEW_ACCEPT)]
            ),
        },
    )


def validation_tools(*passed_flags, runs=None, files=None, texts=None):
    return SandboxToolInvoker(
        runs=runs or [tool_success(stdout=robustness_stdout(*passed_flags))],
        files=[EXPERIMENT_SCRIPT_PATH, "data/orders.csv"] if files is None else files,
        texts={EXPERIMENT_SCRIPT_PATH: EXPERIMENT_CODE} if texts is None else texts,
    )


def validation_services(llm, tools, audits=None):
    services = make_services(llm, tools)
    services.extras["subagents"] = SubagentSupervisor(
        audit=audits.append if audits is not None else None
    )
    return services


def test_validation_judgement_alone_when_no_tool_port(registry):
    """没有工具端口 = 旧行为：单轮判读照常成功，稳健性复跑如实标注未执行。"""
    llm = StubLlmPort({"validating.default": stub_response(VALIDATION_OK)})
    node = ValidationNode(registry)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["verdict"] == "concerns"
    assert "需求率参数敏感" in result.outputs["validation_summary"]
    assert json.loads(llm.calls[0].variables["metrics"]) == {"rmse": 0.12}
    assert result.outputs["robustness"]["executed"] is False
    assert "未配置工具端口" in result.outputs["robustness"]["reason"]


def test_validation_requires_prior_experiment(registry):
    llm = StubLlmPort({"validating.default": stub_response(VALIDATION_OK)})
    node = ValidationNode(registry)
    ctx = make_ctx(TaskState.VALIDATING, prior=prior_through_planning())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


def test_validation_reruns_experiment_in_sandbox_and_passes_without_gate(registry):
    """三项检查全过：不惊动用户，稳健性结论（数字来自标记行）随产出落库。"""
    llm = validation_llm()
    tools = validation_tools(True, True, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry, available_packages="numpy、pandas").run(
        ctx, validation_services(llm, tools)
    )

    assert result.status == NodeResult.SUCCEEDED
    robustness = result.outputs["robustness"]
    assert robustness["executed"] is True
    assert robustness["status"] == "passed"
    assert robustness["checks_total"] == 3
    assert robustness["checks_failed"] == 0
    assert [check["id"] for check in robustness["checks"]] == [
        "sensitivity_demand",
        "bootstrap_stability",
        "baseline_margin",
    ]
    assert robustness["summary"] == ROBUSTNESS_FINAL["summary"]
    assert robustness["summary_text"] == "沙盒复跑稳健性检查 3 项，通过 3 项，全部达标。"
    # 判读产出原样保留：稳健性是增强，不是替换
    assert result.outputs["verdict"] == "concerns"
    # 检验脚本发布为可复现产物
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["validation_checks.py"]
    # 任务卡：实验脚本正文、方案风险 + 评审保留意见、数据文件、包白名单进 system 段
    card = system_prompt_of(llm.chat_calls[0])
    assert EXPERIMENT_CODE in card
    assert "规模过大时求解超时" in card, "方案自报风险进风险点"
    assert "评审保留（warn）：稳健性" in card, "评审判读的保留意见进风险点"
    assert "合成数据外推风险" in card
    assert "- data/orders.csv" in card
    assert "numpy、pandas" in card
    assert '"rmse": 0.12' in card
    # 实验脚本经 ws_read 从工作区读取，而不是从对话里转录
    assert ("ws_read", {"path": EXPERIMENT_SCRIPT_PATH}) in tools.calls


def test_g3_gate_triggers_when_a_check_fails_and_recommends_accept_for_minority(registry):
    """3 项中 1 项未通过（<50%）：上 G3，推荐「接受并记录局限」。"""
    llm = validation_llm()
    tools = validation_tools(True, False, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "3 项中 1 项未通过" in result.review_reason
    assert "bootstrap 重采样稳定性" in result.review_reason
    meta = result.review_meta
    assert meta["gate"] == "G3"
    assert meta["decision_type"] == "generic"
    assert [option["id"] for option in meta["options"]] == [
        G3_ACCEPT_OPTION_ID,
        "redo:EXPERIMENTING",
        "redo:MODEL_PLANNING",
    ]
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == [G3_ACCEPT_OPTION_ID]
    assert meta["impact"]["checks_total"] == 3
    assert meta["impact"]["checks_failed"] == 1
    assert meta["impact"]["failed"][0]["id"] == "bootstrap_stability"
    # 判定数字来自标记行：value 与阈值原样进 evidence
    assert meta["impact"]["failed"][0]["threshold"] == 0.15
    # 闸门未拍板前，检验结论已随 STEP_SUCCEEDED 的 outputs 落库
    assert result.outputs["robustness"]["checks_failed"] == 1
    assert "未通过：bootstrap 重采样稳定性" in result.outputs["robustness"]["summary_text"]


def test_g3_recommends_redo_experiment_when_majority_fails(registry):
    llm = validation_llm()
    tools = validation_tools(False, False, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    meta = result.review_meta
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["redo:EXPERIMENTING"]
    assert meta["impact"]["recommended"] == "redo:EXPERIMENTING"


def stalemate_review(**overrides):
    """实验阶段生成者-评审者环僵持后留在 outputs["review"] 的形状。"""
    review = {
        "executed": True,
        "rounds": 2,
        "verdict": "reject",
        "blockers": 1,
        "findings": list(REVIEW_REJECT["findings"]),
        "summary": REVIEW_REJECT["summary"],
        "rerun": {"executed": True, "consistent": True, "metrics": {"rmse": 0.12}, "diff": [], "reason": ""},
        "stalemate": True,
        "reason": "审稿 2 轮后仍有阻断性意见未解决",
        "llm_calls": 2,
    }
    review.update(overrides)
    return review


def test_g3_gate_triggers_on_a_review_stalemate_even_when_every_check_passes(registry):
    """§8.4「僵持到预算尽 → 上闸门」= 复用 G3：稳健性全过也要请人裁定未解决的阻断性意见。"""
    llm = validation_llm()
    tools = validation_tools(True, True, True)
    prior = validation_prior()
    prior[TaskState.EXPERIMENTING.value]["review"] = stalemate_review()
    ctx = make_ctx(TaskState.VALIDATING, prior=prior)

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason == (
        "独立审稿 2 轮后仍有 1 条阻断性意见未解决（审稿 2 轮后仍有阻断性意见未解决）。"
        "请确认实验结果的处置方式"
    )
    meta = result.review_meta
    assert meta["gate"] == "G3"
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["redo:EXPERIMENTING"]
    assert meta["impact"]["checks_total"] == 3 and meta["impact"]["checks_failed"] == 0
    assert meta["impact"]["review_stalemate"] is True
    assert [entry["id"] for entry in meta["impact"]["reviewer_findings"]] == ["R1"], "只有 blocker 进卡片"
    # 稳健性检查照常执行并落库；审稿意见排在复跑任务卡风险点最前
    assert result.outputs["robustness"]["checks_failed"] == 0
    card = system_prompt_of(llm.chat_calls[0])
    assert "- 审稿意见（blocker｜未解决）：baseline——基线与模型用了不同的评估切分" in card
    assert card.index("审稿意见（blocker") < card.index("方案风险：")


def test_g3_gate_combines_failed_checks_with_a_review_stalemate(registry):
    llm = validation_llm()
    tools = validation_tools(True, False, True)
    prior = validation_prior()
    prior[TaskState.EXPERIMENTING.value]["review"] = stalemate_review()
    ctx = make_ctx(TaskState.VALIDATING, prior=prior)

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.startswith("稳健性检查 3 项中 1 项未通过：bootstrap 重采样稳定性；独立审稿 2 轮后")
    meta = result.review_meta
    assert meta["impact"]["checks_failed"] == 1 and meta["impact"]["review_stalemate"] is True
    # 少数未通过本会推荐「接受并记录局限」，僵持把推荐抬到「重做实验」
    assert meta["impact"]["recommended"] == "redo:EXPERIMENTING"


def test_g3_stays_closed_when_the_review_accepted_and_only_records_its_remarks(registry):
    llm = validation_llm()
    tools = validation_tools(True, True, True)
    prior = validation_prior()
    prior[TaskState.EXPERIMENTING.value]["review"] = stalemate_review(
        verdict="accept", blockers=0, stalemate=False, reason="", findings=list(REVIEW_ACCEPT["findings"])
    )
    ctx = make_ctx(TaskState.VALIDATING, prior=prior)

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    card = system_prompt_of(llm.chat_calls[0])
    assert "- 审稿意见（minor｜已记录）：metrics——只报了 rmse 一个口径" in card


# -- 检验脚本的生成者-评审者（§8.4）：复跑核对 → reviewer 子代理 → 驳回退 R2 → 僵持进 G3 ----

ROBUSTNESS_CODE_V2 = "print('robustness checks v2')"

ROBUSTNESS_REVIEW_REJECT = {
    "verdict": "reject",
    "findings": [
        {
            "id": "R1",
            "severity": "blocker",
            "location": "baseline_margin",
            "issue": "passed 写死为 True，value 与 threshold 的比较方向相反",
            "fix_hint": "passed = value >= threshold 并据此判定",
        },
    ],
    "summary": "有一项检查是假的，判定不可信。",
}


def test_robustness_review_reruns_deterministically_then_spawns_an_accepting_reviewer(registry):
    audits = []
    llm = validation_llm()
    tools = validation_tools(True, True, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools, audits))

    assert result.status == NodeResult.SUCCEEDED, "审稿通过、检查全过：不上 G3"
    # 复跑核对：同一份检验脚本再 python_run 一次；checks 列表递归比对（含浮点容差）
    assert [call[3]["code"] for call in tools.python_calls] == [ROBUSTNESS_CODE, ROBUSTNESS_CODE]
    robustness = result.outputs["robustness"]
    review = robustness["review"]
    assert review["executed"] is True and review["verdict"] == "accept" and review["rounds"] == 1
    assert review["rerun"]["executed"] is True and review["rerun"]["consistent"] is True
    assert review["rerun"]["metrics"] == {"checks": robustness_checks(True, True, True)}
    assert robustness["llm_calls"] == 3 and robustness["attempts"] == 1
    assert robustness["checks_total"] == 3, "检查结论照旧"
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["validation_checks.py"]
    spawns = [entry for entry in audits if entry["phase"] == "spawn"]
    assert [entry["tool"] for entry in spawns] == ["subagent:sandbox", "subagent:reviewer"]
    reviewer = spawns[1]
    assert reviewer["tool_tier"] == "readonly"
    assert reviewer["toolset"] == ["ws_read", "ws_list"]
    assert reviewer["output_schema_id"] == "robustness-review.v1"
    review_call = next(c for c in llm.chat_calls if c.label == ValidationNode.review_prompt_id)
    card = system_prompt_of(review_call)
    assert "稳健性检验审稿人" in card
    assert ROBUSTNESS_CODE in card and EXPERIMENT_CODE in card, "检验脚本与实验脚本都进材料"
    assert '"id": "bootstrap_stability"' in card and '"threshold": 0.15' in card
    assert "核心指标与首跑逐键一致" in card
    assert ROBUSTNESS_FINAL["summary"] in card
    assert "规模过大时求解超时" in card, "风险点段一并给审稿人"
    assert "整数规划" in card
    opening = user_prompt_of(review_call)
    assert "- ws_read：" in opening and "python_run" not in opening
    assert "每项检查是否真的扰动" in opening


def test_robustness_reviewer_rejection_sends_the_generator_back_to_r2_then_re_reviews(registry):
    llm = validation_llm(
        sandbox=repairing_sandbox_script(
            {"summary": "按审稿意见修正判定方向"},
            first_code=ROBUSTNESS_CODE,
            repaired_code=ROBUSTNESS_CODE_V2,
        ),
        review=reviewer_script(
            (ROBUSTNESS_CODE_V2, REVIEW_ACCEPT), (ROBUSTNESS_CODE, ROBUSTNESS_REVIEW_REJECT)
        ),
    )
    # 首跑 / 复跑三项全过；修复波起 baseline_margin 判为未通过（队列尾项复用）
    tools = validation_tools(
        runs=[
            tool_success(stdout=robustness_stdout(True, True, True)),
            tool_success(stdout=robustness_stdout(True, True, True)),
            tool_success(stdout=robustness_stdout(True, True, False)),
        ]
    )
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    # 修复后的检查有一项未通过 → 走 G3 的原有触发（少数未通过 → 推荐接受并记录局限）
    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.startswith("稳健性检查 3 项中 1 项未通过：对基线优势幅度")
    assert [call[3]["code"] for call in tools.python_calls] == [
        ROBUSTNESS_CODE, ROBUSTNESS_CODE, ROBUSTNESS_CODE_V2, ROBUSTNESS_CODE_V2,
    ]
    robustness = result.outputs["robustness"]
    assert robustness["review"]["verdict"] == "accept" and robustness["review"]["rounds"] == 2
    assert robustness["checks_failed"] == 1, "检查结论按最终采用波重算"
    assert robustness["summary"] == "按审稿意见修正判定方向"
    assert robustness["attempts"] == 2 and robustness["llm_calls"] == 6
    repair_wave = next(
        user_prompt_of(c)
        for c in llm.chat_calls
        if c.label == ROBUSTNESS_PROMPT_ID and "审稿驳回意见" in user_prompt_of(c)
    )
    assert "[R1｜blocker] baseline_margin：passed 写死为 True" in repair_wave
    meta = result.review_meta
    assert meta["impact"]["checks_review_stalemate"] is False
    assert [option["id"] for option in meta["options"]] == [
        G3_ACCEPT_OPTION_ID, "redo:EXPERIMENTING", "redo:MODEL_PLANNING",
    ], "审稿没僵持就不多给「重做检验」"
    assert [ref.uri.rsplit("/", 1)[-1] for ref in result.artifacts] == ["validation_checks.py"]


def test_robustness_review_stalemate_opens_g3_with_a_redo_validating_option(registry):
    """检验脚本审稿两轮仍有 blocker：检查不可信 ≠ 结论不稳健 → 多给「重做检验」并推荐。"""
    llm = validation_llm(
        sandbox=repairing_sandbox_script(
            ROBUSTNESS_FINAL, first_code=ROBUSTNESS_CODE, repaired_code=ROBUSTNESS_CODE_V2
        ),
        review=[stub_response(ROBUSTNESS_REVIEW_REJECT)],
    )
    tools = validation_tools(True, True, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    review = result.outputs["robustness"]["review"]
    assert review["stalemate"] is True and review["rounds"] == 2
    assert result.review_reason == (
        "检验脚本的独立审稿 2 轮后仍有 1 条阻断性意见未解决（审稿 2 轮后仍有阻断性意见未解决）。"
        "请确认实验结果的处置方式"
    )
    meta = result.review_meta
    assert [option["id"] for option in meta["options"]] == [
        G3_ACCEPT_OPTION_ID, "redo:VALIDATING", "redo:EXPERIMENTING", "redo:MODEL_PLANNING",
    ]
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["redo:VALIDATING"]
    assert meta["impact"]["recommended"] == "redo:VALIDATING"
    assert meta["impact"]["checks_review_stalemate"] is True
    assert [entry["id"] for entry in meta["impact"]["checks_reviewer_findings"]] == ["R1"]
    assert meta["impact"]["review_stalemate"] is False, "实验审稿那一路没僵持"
    assert meta["impact"]["checks_failed"] == 0
    assert len(tools.python_calls) == 4


def test_g3_prefers_redo_experiment_when_both_review_loops_stalemate(registry):
    llm = validation_llm(review=[stub_response(ROBUSTNESS_REVIEW_REJECT)])
    tools = validation_tools(True, True, True)
    prior = validation_prior()
    prior[TaskState.EXPERIMENTING.value]["review"] = stalemate_review()
    ctx = make_ctx(TaskState.VALIDATING, prior=prior)

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_reason.startswith("独立审稿 2 轮后仍有 1 条阻断性意见未解决")
    assert "；检验脚本的独立审稿 2 轮后仍有 1 条阻断性意见未解决" in result.review_reason
    meta = result.review_meta
    assert [option["id"] for option in meta["options"]] == [
        G3_ACCEPT_OPTION_ID, "redo:VALIDATING", "redo:EXPERIMENTING", "redo:MODEL_PLANNING",
    ]
    # 实验本身存疑时重做实验（下游检验一并重做）比只重做检验更彻底
    assert meta["impact"]["recommended"] == "redo:EXPERIMENTING"
    assert meta["impact"]["review_stalemate"] is True and meta["impact"]["checks_review_stalemate"] is True


def test_robustness_review_reports_an_inconsistent_rerun_per_check_field(registry):
    """复跑的 checks 与首跑不一致：差异路径精确到 checks[i].value，复跑不一致进审稿材料。"""
    drifted = robustness_checks(True, True, True)
    drifted[1]["value"] = 0.09
    llm = validation_llm()
    tools = validation_tools(
        runs=[
            tool_success(stdout=robustness_stdout(True, True, True)),
            tool_success(stdout=robustness_stdout(checks=drifted)),
        ]
    )
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED, "审稿人（桩）仍接受：不一致由审稿人裁量"
    rerun = result.outputs["robustness"]["review"]["rerun"]
    assert rerun["consistent"] is False
    assert rerun["diff"] == ["checks[1].value：首跑 0.08，复跑 0.09"]
    card = system_prompt_of(next(c for c in llm.chat_calls if c.label == ValidationNode.review_prompt_id))
    assert "与首跑不一致" in card and "checks[1].value：首跑 0.08，复跑 0.09" in card


@pytest.mark.parametrize(
    "kwargs, reason_fragment",
    [
        ({"supervisor": False}, "子代理监督者"),
        ({"chat": False}, "会话式调用"),
        ({"script": False}, "没有实验脚本"),
        ({"tools": False}, "未配置工具端口"),
    ],
)
def test_robustness_degrades_honestly_when_a_precondition_is_missing(kwargs, reason_fragment):
    """复跑是尽力而为的增强：装配缺项如实标注 executed=false，绝不假装跑过。"""
    registry = load_default_registry()
    llm = (
        validation_llm()
        if kwargs.get("chat", True)
        else CompleteOnlyPort({"validating.default": stub_response(VALIDATION_OK)})
    )
    tools = (
        validation_tools(True, True, True)
        if kwargs.get("script", True)
        else validation_tools(True, True, True, files=["data/orders.csv"], texts={})
    )
    services = make_services(llm, tools if kwargs.get("tools", True) else None)
    if kwargs.get("supervisor", True):
        services.extras["subagents"] = SubagentSupervisor()
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED, "复跑缺席不阻塞验证阶段"
    assert result.outputs["verdict"] == "concerns"
    assert result.outputs["robustness"]["executed"] is False
    assert reason_fragment in result.outputs["robustness"]["reason"]
    if hasattr(llm, "chat_calls"):
        assert llm.chat_calls == [], "缺项时不得派发会话"


def test_robustness_sandbox_failure_does_not_block_and_skips_g3(registry):
    """检验脚本真跑但没跑成：如实记 failed，沿用判读结论，不上闸门。"""
    llm = validation_llm()
    tools = validation_tools(runs=[tool_failure(stderr="ZeroDivisionError: division by zero")])
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    robustness = result.outputs["robustness"]
    assert robustness["executed"] is True
    assert robustness["status"] == "failed"
    assert robustness["checks"] == []
    assert "未完成" in robustness["summary_text"]
    # 波次修复用尽（最多 3 波），每波一次运行
    assert len(tools.python_calls) == 3


def test_robustness_rejects_single_or_malformed_checks(registry):
    """§7.1 硬纪律：一项检查或缺 value/threshold 的检查不算验收通过。"""
    llm = validation_llm()
    only_one = [
        {"id": "sensitivity_demand", "name": "需求率扰动", "passed": True, "value": 0.05, "threshold": 0.2}
    ]
    malformed = [
        {"id": "a", "name": "A", "passed": True, "value": "n/a", "threshold": 0.2},
        {"id": "b", "name": "B", "passed": "yes", "value": 0.1},
    ]
    tools = validation_tools(
        runs=[
            tool_success(stdout=robustness_stdout(checks=only_one)),
            tool_success(stdout=robustness_stdout(checks=malformed)),
            tool_success(stdout=robustness_stdout(True, True, True)),
        ]
    )
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["robustness"]["status"] == "passed"
    assert result.outputs["robustness"]["attempts"] == 3
    # 第二、三波任务卡带着上一波的断言差异（结构化反馈）
    second_wave = user_prompt_of(llm.chat_calls[2])
    assert "至少 2 项" in second_wave
    third_wave = user_prompt_of(llm.chat_calls[4])
    assert "value 不是数值" in third_wave and "passed 不是布尔值" in third_wave


def test_robustness_rejects_self_reported_success_without_running_code(registry):
    llm = StubLlmPort(
        {"validating.default": stub_response(VALIDATION_OK)},
        chat_scripts={ROBUSTNESS_PROMPT_ID: [stub_response(ROBUSTNESS_FINAL)]},
    )
    tools = validation_tools(True, True, True)
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["robustness"]["status"] == "failed"
    assert tools.python_calls == []


def test_paper_material_carries_robustness_and_g3_decision(registry):
    """论文材料：稳健性一句话 + 用户「接受并记录局限」的纪律一并进检验材料。"""
    prior = paper_prior()
    prior[TaskState.VALIDATING.value] = {
        **VALIDATION_OK,
        "robustness": {
            "executed": True,
            "status": "passed",
            "summary_text": "沙盒复跑稳健性检查 3 项，通过 2 项；未通过：bootstrap 重采样稳定性（bootstrap_stability：value 0.4，阈值 0.15）。",
        },
    }
    ctx = NodeContext(
        run_id="run_1",
        project_id="proj_1",
        state=TaskState.PAPER_WRITING,
        step_id="step_1",
        attempt=1,
        inputs={},
        prior_outputs=prior,
        review_decisions={TaskState.VALIDATING.value: G3_ACCEPT_OPTION_ID},
    )

    material = PaperWritingNode(registry).build_variables(ctx)["validation_summary"]

    assert material.startswith(VALIDATION_OK["validation_summary"])
    assert "通过 2 项" in material and "bootstrap_stability" in material
    assert "接受并记录局限" in material and "不得淡化" in material

    # 未执行复跑、未经 G3：材料保持旧形状（不多一个字）
    plain = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())
    assert (
        PaperWritingNode(registry).build_variables(plain)["validation_summary"]
        == VALIDATION_OK["validation_summary"]
    )


def test_paper_material_carries_review_verdicts_of_both_sandbox_consumers(registry):
    """论文材料：检验脚本的审稿结论进检验材料（通过与僵持都写）；实验代码审稿只在
    僵持时把未解决的阻断性意见追加进实验材料（通过句节点已写进 experiment_summary）。"""
    prior = paper_prior()
    prior[TaskState.EXPERIMENTING.value] = {
        **prior[TaskState.EXPERIMENTING.value],
        "review": {
            "executed": True, "rounds": 2, "verdict": "reject", "blockers": 1,
            "findings": [
                {"id": "R1", "severity": "blocker", "location": "experiment.py:split", "issue": "训练 / 评估切分泄漏", "fix_hint": "按时间切分"},
                {"id": "R2", "severity": "minor", "location": "", "issue": "命名", "fix_hint": ""},
            ],
            "summary": "存在泄漏", "rerun": {"executed": True, "consistent": True},
            "stalemate": True, "reason": "R2 运行预算已尽，无法按审稿意见修复",
        },
    }
    prior[TaskState.VALIDATING.value] = {
        **VALIDATION_OK,
        "robustness": {
            "executed": True,
            "status": "passed",
            "summary_text": "沙盒复跑稳健性检查 3 项，通过 3 项，全部达标。",
            "review": {
                "executed": True, "rounds": 1, "verdict": "accept", "blockers": 0,
                "findings": [], "summary": "检验口径与实验一致",
                "rerun": {"executed": True, "consistent": True}, "stalemate": False, "reason": "",
            },
        },
    }
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=prior)

    variables = PaperWritingNode(registry).build_variables(ctx)

    experiment_material = variables["experiment_summary"]
    assert experiment_material.startswith(prior[TaskState.EXPERIMENTING.value]["experiment_summary"])
    assert "实验代码经独立审稿 2 轮后仍有 1 条阻断性意见未解决" in experiment_material
    assert "[R1｜blocker] experiment.py:split：训练 / 评估切分泄漏（修法：按时间切分）" in experiment_material
    assert "[R2｜minor]" not in experiment_material, "非阻断意见不进论文的局限性清单"
    assert "须在模型检验与局限性部分如实说明" in experiment_material
    validation_material = variables["validation_summary"]
    assert validation_material.endswith(
        "稳健性检验脚本经独立审稿通过（1 轮，0 条意见；确定性复跑核对一致）。"
    )

    # 实验审稿通过（未僵持）：实验材料保持节点输出原样——通过句已在 experiment_summary 里
    prior[TaskState.EXPERIMENTING.value]["review"] = {
        "executed": True, "rounds": 1, "verdict": "accept", "blockers": 0, "findings": [],
        "summary": "忠实于方案", "rerun": {"executed": True, "consistent": True}, "stalemate": False, "reason": "",
    }
    accepted = PaperWritingNode(registry).build_variables(make_ctx(TaskState.PAPER_WRITING, prior=prior))
    assert accepted["experiment_summary"] == prior[TaskState.EXPERIMENTING.value]["experiment_summary"]


CLEANING_STALEMATE_REVIEW = {
    "executed": True, "rounds": 2, "verdict": "reject", "blockers": 1,
    "findings": [
        {"id": "R1", "severity": "blocker", "location": "cleaning.py:impute()", "issue": "目标列 sales 被插补", "fix_hint": "改为删行或标记"},
        {"id": "R2", "severity": "minor", "location": "", "issue": "删行数写死", "fix_hint": "用 len(df) 计算"},
    ],
    "summary": "目标列被越权插补", "rerun": {"executed": True, "consistent": True},
    "stalemate": True, "reason": "审稿 2 轮后仍有阻断性意见未解决",
}


def test_paper_material_carries_data_preparation_cleaning_and_g2_decision(registry):
    """论文材料新增「数据准备与清洗结论」：画像 + 清洗策略 + 清洗执行结论（数字来自
    标记行）+ 清洗脚本审稿结论（通过 / 僵持都写）+ G2 决策纪律；数据预处理小节的
    样本口径以此为准。"""
    prior = paper_prior()
    prior[TaskState.DATA_PREPARATION.value] = {
        **PREPARATION_OK,
        "cleaning": {
            "executed": True, "status": "passed", "attempts": 2, "llm_calls": 5,
            "summary": "按区域中位数插补 sales，删除 96 行负值记录",
            "target_columns": ["sales"], "final_code_artifact": "art_1", "produced_artifacts": ["art_2"],
            "rows_before": 1200, "rows_after": 1104, "rows_deleted_ratio": 0.08,
            "imputed_columns": ["sales"], "imputed_target_columns": ["sales"],
            "review": CLEANING_STALEMATE_REVIEW,
        },
    }
    ctx = NodeContext(
        run_id="run_1", project_id="proj_1", state=TaskState.PAPER_WRITING, step_id="step_1",
        attempt=1, inputs={}, prior_outputs=prior,
        review_decisions={TaskState.DATA_PREPARATION.value: "adopt_cleaned"},
    )

    material = PaperWritingNode(registry).build_variables(ctx)["data_preparation"]

    lines = material.splitlines()
    assert lines[0] == f"数据画像：{PREPARATION_OK['profile_summary']}"
    assert lines[1] == "缺失值策略：线性插值" and lines[2] == "异常值策略：3σ 截断"
    assert lines[3] == (
        "清洗执行：清洗脚本执行 2 波后通过验收，保留 1104 / 1200 行（删行 8.0%），"
        "插补列 sales（含目标列 sales）。清洗摘要：按区域中位数插补 sales，删除 96 行负值记录"
    )
    assert lines[4].startswith("清洗脚本经独立审稿 2 轮后仍有 1 条阻断性意见未解决（审稿 2 轮后仍有阻断性意见未解决；确定性复跑核对一致）")
    assert lines[5] == "[R1｜blocker] cleaning.py:impute()：目标列 sales 被插补（修法：改为删行或标记）"
    assert "[R2｜minor]" not in material, "非阻断意见不进论文的局限性清单"
    assert lines[6] == "用户已在数据确认闸门选择「采用清洗结果」：正文的样本口径与规模按清洗后数据描述。"
    assert "art_1" not in material and "llm_calls" not in material, "过程字段 / 产物引用不进论文材料"

    # 审稿通过 + 用户改用原始数据：通过句一行、口径句改为原始数据
    prior[TaskState.DATA_PREPARATION.value]["cleaning"]["review"] = {
        "executed": True, "rounds": 1, "verdict": "accept", "blockers": 0, "findings": [],
        "summary": "忠实于方案", "rerun": {"executed": True, "consistent": True}, "stalemate": False, "reason": "",
    }
    ctx_raw = NodeContext(
        run_id="run_1", project_id="proj_1", state=TaskState.PAPER_WRITING, step_id="step_1",
        attempt=1, inputs={}, prior_outputs=prior,
        review_decisions={TaskState.DATA_PREPARATION.value: "use_raw"},
    )
    raw_material = PaperWritingNode(registry).build_variables(ctx_raw)["data_preparation"]
    assert "清洗脚本经独立审稿通过（1 轮，0 条意见；确定性复跑核对一致）。" in raw_material
    assert raw_material.endswith("清洗过程只作为数据处理探索如实说明，不得以清洗后数据作为结论依据。")
    assert "采用清洗结果" not in raw_material

    # 首波没过验收：没有审稿；口径句点明不得以清洗后数据为样本；未经 G2 → 没有决策句
    prior[TaskState.DATA_PREPARATION.value]["cleaning"] = {
        "executed": True, "status": "failed", "attempts": 3, "summary": "",
        "rows_before": 100, "rows_after": 80, "imputed_columns": [], "imputed_target_columns": [],
    }
    failed_material = PaperWritingNode(registry).build_variables(make_ctx(TaskState.PAPER_WRITING, prior=prior))["data_preparation"]
    assert failed_material.splitlines()[3] == (
        "清洗执行：清洗脚本执行 3 波后未通过验收，保留 80 / 100 行（删行 20.0%）；"
        "清洗产物未通过验收，正文不得以清洗后数据作为样本口径。"
    )
    assert "独立审稿" not in failed_material and "数据确认闸门" not in failed_material


def test_paper_material_data_preparation_degrades_honestly(registry):
    """清洗没跑 → 写明未执行与原因、按原始数据建模；审稿环之前的运行没有 cleaning 键
    → 「无执行记录」；数据阶段整体缺席 → 「无」。绝不虚构清洗过程。"""
    prior = paper_prior()
    prior[TaskState.DATA_PREPARATION.value] = {
        **PREPARATION_OK,
        "cleaning": {"executed": False, "reason": "工作区没有已下发的数据文件，无需清洗"},
    }
    skipped = PaperWritingNode(registry).build_variables(make_ctx(TaskState.PAPER_WRITING, prior=prior))["data_preparation"]
    assert skipped.splitlines()[3] == (
        "清洗执行：未执行（工作区没有已下发的数据文件，无需清洗），建模按原始数据进行，正文不得虚构清洗过程。"
    )
    assert len(skipped.splitlines()) == 4

    legacy = PaperWritingNode(registry).build_variables(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()))["data_preparation"]
    assert legacy.splitlines()[3] == "清洗执行：无执行记录，建模按原始数据进行，正文不得虚构清洗过程。"

    prior = paper_prior()
    del prior[TaskState.DATA_PREPARATION.value]
    assert PaperWritingNode(registry).build_variables(make_ctx(TaskState.PAPER_WRITING, prior=prior))["data_preparation"] == "无"


def test_paper_pipeline_routes_data_material_and_admits_its_numbers(registry):
    """总编与回退单次调用都拿到「数据准备与清洗结论」；source_keys 能把它单独路由给
    数据预处理章；材料里的清洗数字（1104 / 1200）进审计允许集，正文引用不算无出处。"""
    prior = paper_prior_with_tables()
    prior[TaskState.DATA_PREPARATION.value] = {
        **prior[TaskState.DATA_PREPARATION.value],
        "cleaning": {
            "executed": True, "status": "passed", "attempts": 1, "summary": "",
            "rows_before": 1200, "rows_after": 1104, "rows_deleted_ratio": 0.08,
            "imputed_columns": [], "imputed_target_columns": [],
        },
    }
    outline = dict(PAPER_OUTLINE_OK, chapters=[
        {"heading": "1 数据预处理", "brief": "按清洗结论写样本口径", "target_chars": 600, "source_keys": ["data_preparation"]},
        {"heading": "2 模型建立与求解", "brief": "全量材料", "target_chars": 800},
        {"heading": "3 结果分析", "brief": "全量材料", "target_chars": 600},
    ])

    def section_reply(variables):
        lead = f"清洗后保留 1104 行（原 1200 行），rmse=0.12。（{variables['chapter_heading']}）"
        target = int(variables["target_chars"])
        return stub_response({
            "content": lead + "析" * max(target - len(lead), 0),
            "digest": f"{variables['chapter_heading']}摘要",
        })

    llm = StubLlmPort({
        "paper_outline.default": stub_response(outline),
        "paper_section.default": section_reply,
        "paper_finalize.default": stub_response(PAPER_FINALIZE_OK),
    })
    node = PaperWritingNode(registry)
    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=prior), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    outline_call = next(c for c in llm.calls if c.prompt_id == "paper_outline.default")
    rendered = registry.get("paper_outline.default").render(outline_call.variables)
    assert "## 数据准备与清洗结论" in rendered
    assert "保留 1104 / 1200 行（删行 8.0%）" in rendered
    assert "`data_preparation`" in rendered, "总编的 source_keys 枚举要认这份材料"
    section_calls = [c for c in llm.calls if c.prompt_id == "paper_section.default"]
    assert "### 数据准备与清洗结论" in section_calls[0].variables["materials"]
    assert "### 已确认的建模方案" not in section_calls[0].variables["materials"]
    assert "### 数据准备与清洗结论" in section_calls[1].variables["materials"], "未指定 source_keys → 全量材料含数据结论"
    assert result.outputs["audit_findings"] == [], "清洗数字有出处（材料 + 冻结清单）"

    fallback = ScriptedLlmPort({
        "paper_outline.default": ["不是 JSON", "还是不是 JSON"],
        "paper_writing.default": [stub_response(PAPER_OK)],
    })
    fallback_result = PaperWritingNode(registry).run(make_ctx(TaskState.PAPER_WRITING, prior=prior), make_services(fallback))
    assert fallback_result.metrics["fallback"] == "single_call"
    fallback_call = next(c for c in fallback.calls if c.prompt_id == "paper_writing.default")
    rendered_fallback = registry.get("paper_writing.default").render(fallback_call.variables)
    assert "## 数据准备与清洗结论" in rendered_fallback and "保留 1104 / 1200 行" in rendered_fallback


# -- 假设表的下游消费：实验任务卡 / 判读 / 稳健性检验 / 论文材料 ----------------------


PLANNING_ASSUMPTIONS = [
    {"id": "G1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面给定", "impact": "medium", "status": "confirmed"},
    {"id": "G2", "text": "候选点之间需求独立", "scope": "global", "basis": "简化需要", "impact": "low", "status": "to_verify"},
    {"id": "A1", "text": "预算约束为硬约束", "scope": "A", "basis": "题面", "impact": "high", "status": "critical"},
    {"id": "A2", "text": "开店成本与规模线性", "scope": "A", "basis": "数据画像", "impact": "medium", "status": "to_verify"},
    {"id": "B1", "text": "到达过程近似泊松", "scope": "B", "basis": "排队论前提", "impact": "medium", "status": "critical"},
    {"text": "缺 id 的行被忽略", "scope": "global", "impact": "low", "status": "critical"},
]


def planning_with_assumptions():
    return dict(PLANNING_OK, assumptions=[dict(row) for row in PLANNING_ASSUMPTIONS])


def assumption_prior(**experiment_extra):
    prior = validation_prior()
    prior[TaskState.MODEL_PLANNING.value] = planning_with_assumptions()
    if experiment_extra:
        prior[TaskState.EXPERIMENTING.value].update(experiment_extra)
    return prior


def test_assumption_helpers_scope_order_and_material():
    planning = planning_with_assumptions()
    rows_a = plan_assumptions(planning, "A")
    assert [row["id"] for row in rows_a] == ["G1", "G2", "A1", "A2"], "全局 + 本方案，原顺序；缺 id 行忽略"
    assert [row["id"] for row in plan_assumptions(planning, "B")] == ["G1", "G2", "B1"]
    assert plan_assumptions(PLANNING_OK, "A") == [], "旧运行没有假设表"

    focus = assumptions_to_verify(rows_a)
    assert [row["id"] for row in focus] == ["A1", "G2", "A2"], "重点验证在前，其后待检验按原顺序"

    material = assumption_material(focus)
    assert material.splitlines() == [
        "- A1【重点验证｜影响高｜方案 A】预算约束为硬约束（依据：题面）",
        "- G2【待检验｜影响低｜全局】候选点之间需求独立（依据：简化需要）",
        "- A2【待检验｜影响中｜方案 A】开店成本与规模线性（依据：数据画像）",
    ]
    assert assumption_material([]) == "无（方案阶段未生成假设表）"


def test_experiment_task_card_carries_the_chosen_plans_assumptions(registry):
    """实验任务卡带全部适用假设（全局 + 选定方案），其它方案的假设不进卡。"""
    llm = experiment_llm()
    tools = SandboxToolInvoker(runs=[tool_success()])
    prior = prior_through_planning()
    prior[TaskState.MODEL_PLANNING.value] = planning_with_assumptions()
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior)

    result = ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    card = system_prompt_of(llm.chat_calls[0])
    assert "## 模型假设" in card
    assert "- G1【已确认｜影响中｜全局】需求服从泊松分布" in card
    assert "- A1【重点验证｜影响高｜方案 A】预算约束为硬约束" in card
    assert "B1" not in card, "落选方案的假设不进实验任务卡"
    assert "不得在代码里悄悄替换" in card

    # 旧运行 / 单次调用路径没有假设表：卡上如实写「无」，不报错
    plain_llm = experiment_llm()
    plain_ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())
    ExperimentExecutionNode(registry).run(plain_ctx, make_full_services(plain_llm, SandboxToolInvoker(runs=[tool_success()])))
    assert "无（方案阶段未生成假设表）" in system_prompt_of(plain_llm.chat_calls[0])


def test_experiment_task_card_follows_the_adopted_plan_assumptions(registry):
    """G1 选了 adopt:B：任务卡换成 B 的假设（与 chosen_plan 同一套选择规则）。"""
    llm = experiment_llm()
    prior = prior_through_planning()
    prior[TaskState.MODEL_PLANNING.value] = planning_with_assumptions()
    ctx = NodeContext(
        run_id="run_1", project_id="proj_1", state=TaskState.EXPERIMENTING, step_id="step_1",
        attempt=1, inputs={}, prior_outputs=prior,
        review_decisions={TaskState.MODEL_PLANNING.value: "adopt:B"},
    )

    ExperimentExecutionNode(registry).run(ctx, make_full_services(llm, SandboxToolInvoker(runs=[tool_success()])))

    card = system_prompt_of(llm.chat_calls[0])
    assert "- B1【重点验证｜影响中｜方案 B】到达过程近似泊松" in card
    assert "A1" not in card and "A2" not in card


def robustness_checks_for_assumptions():
    """三项检查：两项回指假设（A1 过、G2 不过），一项通用（无 assumption_id）。"""
    return [
        {"id": "budget_slack", "name": "预算松紧 ±20% 扰动", "passed": True, "value": 0.03, "threshold": 0.2, "detail": "最优值相对变化 3%", "assumption_id": "A1"},
        {"id": "demand_corr", "name": "引入需求相关性", "passed": False, "value": 0.31, "threshold": 0.15, "detail": "相关系数 0.3 时 rmse 退化 31%", "assumption_id": "G2"},
        {"id": "baseline_margin", "name": "对基线优势幅度", "passed": True, "value": 0.6, "threshold": 0.1, "detail": "优势幅度 60%"},
    ]


def test_validation_routes_focus_assumptions_into_judgement_and_robustness(registry):
    """判读与检验卡都只带须检验的假设（重点验证在前）；检查回指假设后产出覆盖表。"""
    llm = validation_llm()
    tools = validation_tools(runs=[tool_success(stdout=robustness_stdout(checks=robustness_checks_for_assumptions()))])
    ctx = make_ctx(TaskState.VALIDATING, prior=assumption_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    # 判读模板：只带 focus（A1 / G2 / A2），已确认的 G1 不进
    judgement_vars = llm.calls[0].variables
    assert judgement_vars["model_assumptions"].splitlines()[0].startswith("- A1【重点验证")
    assert "G2【待检验" in judgement_vars["model_assumptions"] and "A2【待检验" in judgement_vars["model_assumptions"]
    assert "G1" not in judgement_vars["model_assumptions"]
    # 检验任务卡：同一份 focus + assumption_id 口径
    card = system_prompt_of(llm.chat_calls[0])
    assert "## 须检验的模型假设" in card and "- A1【重点验证｜影响高｜方案 A】预算约束为硬约束" in card
    assert '"assumption_id": "A1"' in card

    assert result.status == NodeResult.NEEDS_REVIEW, "G2 的检查未通过 → G3 照常"
    robustness = result.outputs["robustness"]
    assert robustness["attempts"] == 1, "首波即满足假设覆盖断言"
    assert [check["assumption_id"] for check in robustness["checks"]] == ["A1", "G2", None]
    assert robustness["assumption_coverage"] == [
        {"id": "A1", "text": "预算约束为硬约束", "status": "critical", "impact": "high", "plan_id": "A", "check_ids": ["budget_slack"], "passed": True},
        {"id": "G2", "text": "候选点之间需求独立", "status": "to_verify", "impact": "low", "plan_id": None, "check_ids": ["demand_corr"], "passed": False},
        {"id": "A2", "text": "开店成本与规模线性", "status": "to_verify", "impact": "medium", "plan_id": "A", "check_ids": [], "passed": None},
    ]
    assert robustness["uncovered_focus"] == ["A2"]
    assert result.review_meta["impact"]["assumption_coverage"] == robustness["assumption_coverage"]


def test_robustness_requires_at_least_one_check_to_target_an_assumption(registry):
    """有须检验的假设却没有检查回指：断言不过，反馈点名假设，下一波补上即过。"""
    llm = validation_llm()
    generic = robustness_checks(True, True, True)
    stray = [dict(check, assumption_id="Z9") for check in generic]
    tools = validation_tools(
        runs=[
            tool_success(stdout=robustness_stdout(checks=generic)),
            tool_success(stdout=robustness_stdout(checks=stray)),
            tool_success(stdout=robustness_stdout(checks=robustness_checks_for_assumptions())),
        ]
    )
    ctx = make_ctx(TaskState.VALIDATING, prior=assumption_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    robustness = result.outputs["robustness"]
    assert robustness["status"] == "passed"
    assert robustness["attempts"] == 3
    second_wave = user_prompt_of(llm.chat_calls[2])
    assert "没有任何检查针对须检验的模型假设" in second_wave
    assert "A1（重点验证）预算约束为硬约束" in second_wave
    assert "A1、A2、G2" in second_wave, "可选 id 列表按字母序"
    third_wave = user_prompt_of(llm.chat_calls[4])
    assert "没有任何检查针对须检验的模型假设" in third_wave, "写错的 assumption_id 不算覆盖"
    assert [check["assumption_id"] for check in robustness["checks"]] == ["A1", "G2", None]


def test_robustness_assumption_rules_stay_inert_without_an_assumption_table(registry):
    """旧运行 / 单次调用路径：没有假设表 → 零要求、覆盖表为空、检查项 assumption_id 为 None。"""
    llm = validation_llm()
    tools = validation_tools(runs=[tool_success(stdout=robustness_stdout(checks=[dict(check, assumption_id="A1") for check in robustness_checks(True, True, True)]))])
    ctx = make_ctx(TaskState.VALIDATING, prior=validation_prior())

    result = ValidationNode(registry).run(ctx, validation_services(llm, tools))

    robustness = result.outputs["robustness"]
    assert robustness["attempts"] == 1
    assert [check["assumption_id"] for check in robustness["checks"]] == [None, None, None], "没有假设表时任何 id 都不算已知"
    assert robustness["assumption_coverage"] == [] and robustness["uncovered_focus"] == []
    assert llm.calls[0].variables["model_assumptions"] == "无（方案阶段未生成假设表）"


def test_paper_material_reports_assumption_coverage(registry):
    """论文材料多一行「模型假设检验」：通过 / 未通过 / 未覆盖各一句，未覆盖点明进局限性。"""
    prior = paper_prior()
    prior[TaskState.VALIDATING.value] = {
        **VALIDATION_OK,
        "robustness": {
            "executed": True,
            "status": "passed",
            "summary_text": "沙盒复跑稳健性检查 3 项，通过 2 项；未通过：引入需求相关性（demand_corr：value 0.31，阈值 0.15）。",
            "assumption_coverage": [
                {"id": "A1", "text": "预算约束为硬约束", "status": "critical", "impact": "high", "plan_id": "A", "check_ids": ["budget_slack"], "passed": True},
                {"id": "G2", "text": "候选点之间需求独立", "status": "to_verify", "impact": "low", "plan_id": None, "check_ids": ["demand_corr"], "passed": False},
                {"id": "A2", "text": "开店成本与规模线性", "status": "to_verify", "impact": "medium", "plan_id": "A", "check_ids": [], "passed": None},
            ],
        },
    }
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=prior)

    material = PaperWritingNode(registry).build_variables(ctx)["validation_summary"]

    assert material.endswith(
        "模型假设检验：A1「预算约束为硬约束」通过（budget_slack）；"
        "G2「候选点之间需求独立」未通过（demand_corr）；"
        "A2「开店成本与规模线性」未被检验覆盖，须在局限性中说明。"
    )


# -- 符号表的下游消费：实验任务卡 / 论文材料 / 总编符号约定兜底 -----------------------


PLANNING_SYMBOLS = [
    {"symbol": "i \\in \\mathcal{I}", "kind": "set", "definition": "候选点索引", "unit": None, "range": "1…N", "plan_id": None},
    {"symbol": "d_i", "kind": "parameter", "definition": "候选点 i 的需求量", "unit": "件/日", "range": "≥ 0", "plan_id": None},
    {"symbol": "x_i", "kind": "variable", "definition": "是否在点 i 选址", "unit": None, "range": "{0,1}", "plan_id": "A"},
    {"symbol": "z", "kind": "objective", "definition": "总成本", "unit": "万元", "range": None, "plan_id": "A"},
    {"symbol": "T", "kind": "parameter", "definition": "退火温度", "unit": None, "range": "> 0", "plan_id": "B"},
    {"symbol": "y", "kind": "other", "definition": "", "unit": None, "range": None, "plan_id": None},
]


def planning_with_tables():
    return dict(planning_with_assumptions(), symbols=[dict(row) for row in PLANNING_SYMBOLS])


def paper_prior_with_tables():
    prior = paper_prior()
    prior[TaskState.MODEL_PLANNING.value] = planning_with_tables()
    return prior


def test_symbol_helpers_scope_order_and_material():
    planning = planning_with_tables()
    rows_a = plan_symbols(planning, "A")
    assert [row["symbol"] for row in rows_a] == ["i \\in \\mathcal{I}", "d_i", "x_i", "z"], "共享 + 本方案，原顺序；缺定义行忽略"
    assert [row["symbol"] for row in plan_symbols(planning, "B")] == ["i \\in \\mathcal{I}", "d_i", "T"]
    assert plan_symbols(PLANNING_OK, "A") == [], "旧运行没有符号表"

    assert symbol_material(rows_a).splitlines() == [
        "- i \\in \\mathcal{I}（集合 / 索引｜共享）＝候选点索引［取值：1…N］",
        "- d_i（参数｜共享）＝候选点 i 的需求量［单位：件/日；取值：≥ 0］",
        "- x_i（决策变量｜方案 A）＝是否在点 i 选址［取值：{0,1}］",
        "- z（目标函数｜方案 A）＝总成本［单位：万元］",
    ]
    assert symbol_material([]) == "无（方案阶段未生成符号表）"


def test_notation_completion_ignores_delimiters_and_is_idempotent():
    rows = plan_symbols(planning_with_tables(), "A")
    # 总编把 x_i 写成 $x_{i}$、索引写成 \(i \in \mathcal{I}\)：视为同一记号；d_i 与 z 漏了
    # （「size」里的 z、另一个量 $x_{ij}$ 里的 x_i 都不算出现）
    notation = (
        "| 符号 | 含义 |\n| $x_{i}$ | 是否选址 |\n| \\(i \\in \\mathcal{I}\\) | 索引 |\n"
        "| $x_{ij}$ | problem size 相关的调度量 |"
    )
    assert [row["symbol"] for row in missing_symbols(notation, rows)] == ["d_i", "z"]
    d_and_z = [row for row in rows if row["symbol"] in ("d_i", "z")]
    assert missing_symbols("目标 $z$ 与需求 $d_{i}$", d_and_z) == [], "单字母记号靠边界识别"
    assert [row["symbol"] for row in missing_symbols("size 与 $d_{i,t}$", d_and_z)] == ["z"]

    completed, filled = complete_notation(notation, rows)
    assert [row["symbol"] for row in filled] == ["d_i", "z"]
    assert completed.endswith(
        "方案阶段符号表补充（总编符号约定漏列，按方案符号表原样补齐）：\n"
        "- $d_i$：候选点 i 的需求量（单位：件/日；取值：≥ 0）\n"
        "- $z$：总成本（单位：万元）"
    )
    assert completed.startswith(notation), "原符号约定原样保留，只在末尾追加"
    # 幂等：续写路径对同一骨架再过一遍，不会重复追加
    again, filled_again = complete_notation(completed, rows)
    assert again == completed and filled_again == []
    # 全覆盖 / 无表：原样返回
    assert complete_notation(completed, []) == (completed, [])
    assert complete_notation("", rows)[0].startswith("方案阶段符号表补充")


def test_experiment_task_card_carries_the_chosen_plans_symbols(registry):
    prior = prior_through_planning()
    prior[TaskState.MODEL_PLANNING.value] = planning_with_tables()
    node = ExperimentExecutionNode(registry)

    material = node.build_variables(make_ctx(TaskState.EXPERIMENTING, prior=prior))["model_symbols"]
    assert "- x_i（决策变量｜方案 A）＝是否在点 i 选址［取值：{0,1}］" in material
    assert "- d_i（参数｜共享）" in material
    assert "T（参数｜方案 B）" not in material, "只带所选方案与共享符号"

    # 旧运行 / 单次调用路径没有符号表：如实写「无」，模板照常渲染
    variables = node.build_variables(make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning()))
    assert variables["model_symbols"] == "无（方案阶段未生成符号表）"
    rendered = registry.get("experiment_code.sandbox").render({**variables, "data_files": "无"})
    assert "## 模型符号（方案阶段确认" in rendered and "无（方案阶段未生成符号表）" in rendered


def test_paper_materials_carry_both_tables_and_route_by_source_keys(registry):
    outline = dict(PAPER_OUTLINE_OK, chapters=[
        {"heading": "1 模型假设", "brief": "按表逐条列出", "target_chars": 600, "source_keys": ["model_assumptions"]},
        {"heading": "2 符号说明", "brief": "按符号约定列表", "target_chars": 600, "source_keys": ["model_symbols"]},
        {"heading": "3 模型建立与求解", "brief": "全量材料", "target_chars": 800},
    ])

    def section_reply(variables):
        # 「{0,1}」「≥ 0」只出现在符号表里：数字审计得认它们有出处
        lead = f"决策变量取值 {{0,1}}，需求量 ≥ 0，rmse=0.12。（{variables['chapter_heading']}）"
        target = int(variables["target_chars"])
        return stub_response({
            "content": lead + "析" * max(target - len(lead), 0),
            "digest": f"{variables['chapter_heading']}摘要",
        })

    llm = StubLlmPort({
        "paper_outline.default": stub_response(outline),
        "paper_section.default": section_reply,
        "paper_finalize.default": stub_response(PAPER_FINALIZE_OK),
    })
    node = PaperWritingNode(registry)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior_with_tables())

    variables = node.build_variables(ctx)
    assert variables["model_assumptions"].splitlines()[0] == "- G1【已确认｜影响中｜全局】需求服从泊松分布（依据：题面给定）"
    assert "B1【" not in variables["model_assumptions"], "论文只带所选方案的假设"
    assert variables["model_symbols"].splitlines()[2] == "- x_i（决策变量｜方案 A）＝是否在点 i 选址［取值：{0,1}］"

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    outline_call = next(c for c in llm.calls if c.prompt_id == "paper_outline.default")
    rendered = registry.get("paper_outline.default").render(outline_call.variables)
    assert "## 模型假设表" in rendered and "## 模型符号表" in rendered
    section_calls = [c for c in llm.calls if c.prompt_id == "paper_section.default"]
    assert "### 模型假设表" in section_calls[0].variables["materials"]
    assert "### 模型符号表" not in section_calls[0].variables["materials"]
    assert "### 模型符号表" in section_calls[1].variables["materials"]
    assert "### 模型假设表" not in section_calls[1].variables["materials"]
    for label in ("### 模型假设表", "### 模型符号表", "### 数字冻结清单"):
        assert label in section_calls[2].variables["materials"], "未指定 source_keys → 全量材料"
    assert not any("未在冻结清单" in w for w in result.metrics.get("quality_warnings", [])), (
        "符号取值 / 假设文本里的数字进审计允许集"
    )
    assert result.outputs["audit_findings"] == []


def test_paper_outline_notation_gets_missing_symbols_filled_and_warned(registry):
    # PAPER_OUTLINE_OK 的符号约定只有 $x_i$：共享的索引 / 需求量与目标 z 都漏了
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior_with_tables())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.metrics["notation_filled"] == 3
    assert result.metrics["quality_warnings"] == [
        "总编符号约定漏列 3 个方案符号（i \\in \\mathcal{I}、d_i、z），已按方案符号表补齐",
    ]
    for call in (c for c in llm.calls if c.prompt_id == "paper_section.default"):
        notation = call.variables["notation"]
        assert notation.startswith(PAPER_OUTLINE_OK["notation"])
        assert "- $d_i$：候选点 i 的需求量（单位：件/日；取值：≥ 0）" in notation
        assert "$T$" not in notation, "方案 B 的符号不进方案 A 的论文"
    # 骨架事件存的是总编原始产出（检查点），补齐是节点每趟重算的
    assert [c.prompt_id for c in llm.calls].count("paper_outline.default") == 1


def test_paper_outline_notation_covering_all_symbols_leaves_no_trace(registry):
    outline = dict(PAPER_OUTLINE_OK, notation=(
        "| 符号 | 含义 |\n| $i \\in \\mathcal{I}$ | 索引 |\n| $d_{i}$ | 需求 |\n| $x_i$ | 选址 |\n| $z$ | 总成本 |"
    ))

    def section_reply(variables):
        lead = f"围绕 rmse=0.12 展开的正文。（{variables['chapter_heading']}）"
        target = int(variables["target_chars"])
        return stub_response({
            "content": lead + "析" * max(target - len(lead), 0),
            "digest": f"{variables['chapter_heading']}摘要",
        })

    llm = StubLlmPort({
        "paper_outline.default": stub_response(outline),
        "paper_section.default": section_reply,
        "paper_finalize.default": stub_response(PAPER_FINALIZE_OK),
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior_with_tables()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "notation_filled" not in result.metrics and "quality_warnings" not in result.metrics
    section_call = next(c for c in llm.calls if c.prompt_id == "paper_section.default")
    assert section_call.variables["notation"] == outline["notation"]


def test_paper_resume_path_completes_notation_too(registry):
    """检查点里的骨架是总编原始产出：续写时同样按方案符号表补齐（幂等，不重复追加）。"""
    from omm_agent_skills.nodes import _inputs_hash

    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    prior = paper_prior_with_tables()
    services.extras["paper_resume"] = lambda: {
        "inputs_hash": _inputs_hash(node.build_variables(make_ctx(TaskState.PAPER_WRITING, prior=prior))),
        "outline": PAPER_OUTLINE_OK,
        "sections": [
            {"index": 1, "heading": "1 问题重述", "content": "检查点里的第一章。", "digest": "第一章摘要", "truncated": False},
        ],
    }

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=prior), services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.metrics["resumed_chapters"] == 1 and result.metrics["notation_filled"] == 3
    assert llm.calls[0].prompt_id == "paper_section.default"
    assert llm.calls[0].variables["notation"].count("方案阶段符号表补充") == 1


def test_paper_single_call_fallback_receives_both_tables(registry):
    llm = ScriptedLlmPort({
        "paper_outline.default": ["不是 JSON", "还是不是 JSON"],
        "paper_writing.default": [stub_response(PAPER_OK)],
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior_with_tables()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW and result.metrics["fallback"] == "single_call"
    fallback_call = next(c for c in llm.calls if c.prompt_id == "paper_writing.default")
    assert "- A1【重点验证｜影响高｜方案 A】预算约束为硬约束（依据：题面）" in fallback_call.variables["model_assumptions"]
    assert "- z（目标函数｜方案 A）＝总成本［单位：万元］" in fallback_call.variables["model_symbols"]
    rendered = registry.get("paper_writing.default").render(fallback_call.variables)
    assert "## 模型符号表" in rendered and "以「模型符号表」为底稿" in rendered


# -- PaperWritingNode ----------------------------------------------------------


def paper_prior():
    prior = prior_through_planning()
    prior[TaskState.EXPERIMENTING.value] = {
        "experiment_summary": "贪心近似 rmse=0.12",
        "metrics": {"rmse": 0.12},
    }
    prior[TaskState.VALIDATING.value] = dict(VALIDATION_OK)
    return prior


def test_paper_writing_multipass_publishes_markdown_artifact(registry):
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    progress = []
    services.extras["progress"] = progress.append
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    # H5 起论文发布后必停 G4（草稿是交付物，定稿前过人的眼）；产出照常齐全
    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_meta["gate"] == "G4"
    assert result.outputs["title"] == PAPER_OUTLINE_OK["title"]
    # 章节顺序与总编规划一致；摘要与关键词来自统稿调用
    assert [s["heading"] for s in result.outputs["sections"]] == [
        "1 问题重述", "2 模型建立与求解", "3 结果分析与检验",
    ]
    assert result.outputs["abstract"] == PAPER_FINALIZE_OK["abstract"]
    assert result.outputs["keywords"] == PAPER_FINALIZE_OK["keywords"]
    assert result.outputs["progress_note"] == PAPER_FINALIZE_OK["progress_note"]
    assert result.metrics["chapters"] == 3
    # 调用序列：总编 → 三章 → 统稿
    assert [call.prompt_id for call in llm.calls] == [
        "paper_outline.default",
        "paper_section.default",
        "paper_section.default",
        "paper_section.default",
        "paper_finalize.default",
    ]
    # 进度事件：骨架一条 + 每章一条 + 发布标记一条（断点续写的读取器据此作废本趟检查点）
    assert [event["kind"] for event in progress] == [
        "paper_outline", "paper_section", "paper_section", "paper_section", "paper_published",
    ]
    assert progress[-1]["chapters"] == 3 and progress[-1]["audit_findings"] == 0
    assert progress[0]["total"] == 3
    assert [event["index"] for event in progress[1:4]] == [1, 2, 3]
    assert progress[2]["heading"] == "2 模型建立与求解"
    # 产物：markdown 草稿
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert ref.kind == "paper"
    assert ref.media_type == "text/markdown"
    stored = services.artifacts.blobs[ref.uri].decode("utf-8")
    assert "# 基于整数规划的门店选址优化" in stored
    assert "## 2 模型建立与求解" in stored
    assert "**关键词**：整数规划；选址；0-1 规划" in stored


def test_paper_writing_sections_receive_rolling_digests_and_materials(registry):
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    section_calls = [call for call in llm.calls if call.prompt_id == "paper_section.default"]
    # 滚动摘要：第一章无前文，第三章能看到前两章摘要
    assert section_calls[0].variables["previous_digests"] == "无（本章是全文第一章）"
    assert "第1章《1 问题重述》" in section_calls[2].variables["previous_digests"]
    assert "第2章《2 模型建立与求解》" in section_calls[2].variables["previous_digests"]
    # 材料路由：第一章只带 problem_analysis；第三章未指定 source_keys → 全量材料
    assert "问题分析结果" in section_calls[0].variables["materials"]
    assert "已确认的建模方案" not in section_calls[0].variables["materials"]
    for label in ("问题分析结果", "已确认的建模方案", "实验过程摘要", "检验结论"):
        assert label in section_calls[2].variables["materials"]
    # 数字冻结清单不受路由影响：第一章只要了 problem_analysis 也照样带上
    for call in section_calls:
        assert "数字冻结清单" in call.variables["materials"]
        assert "metrics.rmse" in call.variables["materials"]
    # 总编与统稿同样看到清单
    outline_call = next(c for c in llm.calls if c.prompt_id == "paper_outline.default")
    assert "| metrics.rmse | 0.12 | 实验指标 rmse | EXPERIMENTING.metrics.rmse |" in (
        outline_call.variables["frozen_numbers"]
    )
    finalize_call = next(c for c in llm.calls if c.prompt_id == "paper_finalize.default")
    assert "metrics.rmse" in finalize_call.variables["frozen_numbers"]
    # 符号表逐章注入
    assert "$x_i$" in section_calls[1].variables["notation"]


def test_paper_writing_outline_failure_falls_back_to_single_call(registry):
    llm = ScriptedLlmPort({
        "paper_outline.default": ["不是 JSON", "还是不是 JSON"],
        "paper_writing.default": [stub_response(PAPER_OK)],
    })
    node = PaperWritingNode(registry)
    services = make_services(llm)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    # 回退路径同样必停 G4，且终稿审计照做（这条路没有章级重写，全靠这一道）
    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.review_meta["gate"] == "G4"
    assert result.outputs["audit_findings"] == []
    assert result.outputs["frozen_numbers"][0]["id"] == "metrics.rmse"
    assert result.outputs["title"] == PAPER_OK["title"]
    assert result.metrics["fallback"] == "single_call"
    assert len(result.artifacts) == 1
    # 总编两次尝试（含修复）后才回退
    outline_calls = [c for c in llm.calls if c.prompt_id == "paper_outline.default"]
    assert len(outline_calls) == 2


def test_paper_writing_degenerate_outline_falls_back(registry):
    # 章数低于带宽下限（3）：结构准入不通过，同样回退单次调用
    degenerate = dict(PAPER_OUTLINE_OK, chapters=PAPER_OUTLINE_OK["chapters"][:1])
    llm = StubLlmPort({
        "paper_outline.default": stub_response(degenerate),
        "paper_writing.default": stub_response(PAPER_OK),
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.metrics["fallback"] == "single_call"
    assert "章节数" in result.metrics["fallback_reason"]


def test_paper_writing_section_failure_names_chapter(registry):
    llm = ScriptedLlmPort({
        "paper_outline.default": [stub_response(PAPER_OUTLINE_OK)],
        "paper_section.default": ["坏输出"],  # 反复返回坏输出：第一章修复后仍失败
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "第 1/3 章" in result.error


def test_paper_writing_finalize_failure_assembles_abstract(registry):
    def section_reply(variables):
        return stub_response({
            "content": f"正文。（{variables['chapter_heading']}）",
            "digest": f"{variables['chapter_heading']}摘要",
        })

    llm = StubLlmPort({
        "paper_outline.default": stub_response(PAPER_OUTLINE_OK),
        "paper_section.default": section_reply,
        "paper_finalize.default": "不是 JSON",  # 统稿两次尝试均失败
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.outputs["abstract"]  # 摘要由各章摘要拼接，不弃全文
    assert result.outputs["keywords"] == PAPER_OUTLINE_OK["keywords"]
    assert any("统稿调用失败" in w for w in result.metrics["quality_warnings"])


def test_paper_writing_without_artifact_store_fails(registry):
    llm = StubLlmPort({})
    node = PaperWritingNode(registry)
    services = NodeServices(
        clock=FixedClock(), ids=SequentialIdGenerator(), artifacts=None, llm=llm
    )
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    assert result.status == NodeResult.FAILED
    assert "artifact" in result.error
    assert llm.calls == []  # 提前失败：一次模型调用都不该发生


def _paper_inputs_hash(registry):
    """与节点相同的输入指纹算法（tests 直接取模块内实现，避免复刻漂移）。"""
    from omm_agent_skills.nodes import _inputs_hash

    node = PaperWritingNode(registry)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())
    return _inputs_hash(node.build_variables(ctx))


def test_paper_writing_resumes_from_event_checkpoint(registry):
    """断点续写：输入未变时跳过总编调用与已完成章节，只写剩余章节。"""
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    services.extras["paper_resume"] = lambda: {
        "inputs_hash": _paper_inputs_hash(registry),
        "outline": PAPER_OUTLINE_OK,
        "sections": [
            {
                "index": 1,
                "heading": "1 问题重述",
                "content": "第一次尝试已写完的第一章正文。",
                "digest": "第一章摘要（来自检查点）",
                "truncated": False,
            },
        ],
    }
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert result.metrics["resumed_chapters"] == 1
    # 总编不再调用；章节只写第 2、3 章
    assert [call.prompt_id for call in llm.calls] == [
        "paper_section.default",
        "paper_section.default",
        "paper_finalize.default",
    ]
    assert llm.calls[0].variables["chapter_heading"] == "2 模型建立与求解"
    # 续写章节能看到检查点章节的滚动摘要
    assert "第一章摘要（来自检查点）" in llm.calls[0].variables["previous_digests"]
    # 检查点正文原样进入最终成稿
    assert result.outputs["sections"][0]["content"] == "第一次尝试已写完的第一章正文。"


def test_paper_writing_stale_checkpoint_regenerates(registry):
    """输入指纹对不上（题面/方案变了）：作废检查点，整篇重来。"""
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    services.extras["paper_resume"] = lambda: {
        "inputs_hash": "0" * 64,
        "outline": PAPER_OUTLINE_OK,
        "sections": [],
    }

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "resumed_chapters" not in result.metrics
    assert [call.prompt_id for call in llm.calls][0] == "paper_outline.default"


def test_paper_writing_length_revision_is_bounded(registry):
    """字数带宽越界触发一次有界重写；全文额度用完后只记警告不再加调用。"""
    good = "字" * 600  # 恰为第一章目标 600 的带宽中心
    llm = ScriptedLlmPort({
        "paper_outline.default": [stub_response(PAPER_OUTLINE_OK)],
        "paper_section.default": [
            stub_response({"content": "太短。", "digest": "d1"}),  # 第 1 章首稿越界
            stub_response({"content": good, "digest": "d2"}),      # 重写后达标；之后重复
        ],
        "paper_finalize.default": [stub_response(PAPER_FINALIZE_OK)],
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    # 第 1 章重写并被采纳；第 2 章（目标 1200）600 字仍越界，用掉第二次额度但
    # 重写结果没有更接近目标 → 保留原稿并记警告；第 3 章（目标 700）600 字在带宽内。
    assert result.metrics["length_revisions"] == 2
    assert result.outputs["sections"][0]["content"] == good
    section_calls = [c for c in llm.calls if c.prompt_id == "paper_section.default"]
    assert len(section_calls) == 5  # 3 章正稿 + 2 次重写
    # 重写调用带上了字数偏差反馈
    assert "超出目标带宽" in section_calls[1].variables["__repair_error"]
    assert any("字数" in w for w in result.metrics["quality_warnings"])


def test_render_paper_markdown_skips_blank_sections():
    markdown = render_paper_markdown(
        {"title": "题", "sections": [{"heading": "", "content": ""}, "not-a-dict"]}
    )
    assert markdown == "# 题\n"


# -- 数字冻结清单 + G4 定稿闸门（H5 切片 1）--------------------------------------


def frozen_prior():
    """四类来源齐备的上游产出：指标 / 稳健性（含文字阈值）/ 清洗统计 / 方案文本数值。"""
    prior = paper_prior()
    prior[TaskState.EXPERIMENTING.value] = {
        "experiment_summary": "贪心近似 rmse=0.12，mae=1.5",
        "metrics": {"rmse": 0.12, "mae": 1.5, "note": "not-a-number", "flag": True},
    }
    prior[TaskState.VALIDATING.value] = {
        **VALIDATION_OK,
        "robustness": {
            "executed": True,
            "status": "passed",
            "checks": [
                {
                    "id": "sensitivity", "name": "需求率扰动", "passed": True,
                    "value": 0.031, "threshold": 0.05,
                },
                {
                    "id": "bootstrap", "name": "bootstrap 稳定性", "passed": False,
                    "value": 0.31, "threshold": "≤ 0.2",
                },
                {
                    "id": "baseline", "name": "基线对比", "passed": True,
                    "value": None, "threshold": None,
                },
            ],
            "checks_total": 3,
            "checks_failed": 1,
            "summary_text": "三项检查通过两项",
        },
    }
    prior[TaskState.DATA_PREPARATION.value] = {
        "profile_summary": "两张表",
        "cleaning": {
            "executed": True,
            "status": "passed",
            "rows_before": 1200,
            "rows_after": 1180,
            "rows_deleted_ratio": 0.0167,
            "imputed_columns": ["demand"],
        },
    }
    prior[TaskState.MODEL_PLANNING.value] = {
        "plans": [
            {
                "id": "A",
                "name": "整数规划",
                "approach": "MILP 建模，分支定界求解，求解时限 300 秒",
                "steps": ["定义决策变量", "构建 3 类约束", "多次重启 20 轮对比"],
                "risks": ["规模超过 10 万变量时求解超时"],
            }
        ],
        "recommended_plan_id": "A",
    }
    return prior


def test_build_frozen_numbers_covers_four_sources_in_fixed_order():
    entries = build_frozen_numbers(frozen_prior())

    assert [(e["id"], e["value"]) for e in entries] == [
        ("metrics.rmse", 0.12),
        ("metrics.mae", 1.5),
        ("robustness.sensitivity.value", 0.031),
        ("robustness.sensitivity.threshold", 0.05),
        ("robustness.bootstrap.value", 0.31),
        ("robustness.bootstrap.threshold.0", 0.2),  # 文字阈值「≤ 0.2」里的数值
        ("cleaning.rows_before", 1200),
        ("cleaning.rows_after", 1180),
        ("cleaning.rows_deleted_ratio", 0.0167),
        ("plan.A.approach.0", 300),
        ("plan.A.steps[2].1", 20),  # 「3 类约束」的一位数不计；risks 不算方案参数
    ]
    by_id = {e["id"]: e for e in entries}
    assert by_id["metrics.rmse"]["source_stage"] == "EXPERIMENTING"
    assert by_id["robustness.bootstrap.value"]["source_path"] == "robustness.checks[1].value"
    assert by_id["robustness.bootstrap.value"]["label"] == "稳健性检查「bootstrap 稳定性」实测值"
    assert by_id["cleaning.rows_after"]["source_stage"] == "DATA_PREPARATION"
    assert by_id["plan.A.approach.0"]["source_stage"] == "MODEL_PLANNING"
    assert "300 秒" in by_id["plan.A.approach.0"]["label"]
    # 表格渲染：每条一行，编号 / 数值 / 含义 / 出处
    table = render_frozen_numbers(entries)
    assert "| metrics.mae | 1.5 | 实验指标 mae | EXPERIMENTING.metrics.mae |" in table
    assert "不换算、不四舍五入" in table


def test_build_frozen_numbers_skips_unexecuted_and_missing_sources():
    prior = frozen_prior()
    prior[TaskState.VALIDATING.value]["robustness"]["executed"] = False
    prior[TaskState.DATA_PREPARATION.value]["cleaning"] = {
        "executed": False, "reason": "无数据文件",
    }
    del prior[TaskState.MODEL_PLANNING.value]

    ids = [e["id"] for e in build_frozen_numbers(prior)]

    assert ids == ["metrics.rmse", "metrics.mae"], "未执行的沙盒结果与缺席阶段一律不冻结"
    assert render_frozen_numbers([]).startswith("（无")


def test_unsourced_numbers_normalizes_tokens_and_samples():
    allowed = allowed_number_tokens(
        [{"value": 0.5}, {"value": 1200}], "题面给定预算 100 万，周期 3 天"
    )
    # 0.50 ≡ 0.5、1200.0 ≡ 1200；100 来自材料；一位数 3 不计；同一数值只报一次；取样上限 8
    text = "预算 100 万内，0.50 的比例，1200.0 行，第 3 问；编造 0.87、0.87、42" + "".join(
        f"、{n}" for n in range(60, 80)
    )
    assert unsourced_numbers(text, allowed)[:3] == ["0.87", "42", "60"]
    assert len(unsourced_numbers(text, allowed)) == 8


def test_paper_writing_stops_at_g4_and_recommends_confirm_when_audit_is_clean(registry):
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    progress = []
    services.extras["progress"] = progress.append

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=frozen_prior()), services)

    assert result.status == NodeResult.NEEDS_REVIEW
    assert "论文草稿已生成（3 章" in result.review_reason
    assert "冻结数字 11 项全部对账通过" in result.review_reason
    meta = result.review_meta
    assert meta["gate"] == "G4" and meta["decision_type"] == "generic"
    assert [o["id"] for o in meta["options"]] == ["confirm_delivery", "redo:PAPER_WRITING"]
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["confirm_delivery"]
    assert meta["impact"]["audit_findings_total"] == 0
    assert meta["impact"]["frozen_numbers_total"] == 11
    assert meta["impact"]["recommended"] == "confirm_delivery"
    # 清单与审计发现随产出进 StageOutput（DocumentDraft 契约的两个新字段）
    frozen_ids = [e["id"] for e in result.outputs["frozen_numbers"]]
    assert frozen_ids[:2] == ["metrics.rmse", "metrics.mae"]
    assert result.outputs["audit_findings"] == []
    assert result.metrics.get("audit_rewrites") is None
    assert progress[-1]["kind"] == "paper_published"


def test_paper_writing_unsourced_numbers_get_one_bounded_rewrite_then_audit_findings(registry):
    """章级数字审计：对不上账 → 带违规清单重写一次（与字数重写共享 2 次额度）；
    重写仍有剩余 / 额度用尽 → 审计发现进 G4 卡片，推荐「退回修改」。"""
    pad = lambda text, target: text + "析" * (target - len(text))  # noqa: E731
    ch1_bad = pad("rmse=0.12，文献常见值 0.87。", 600)
    ch1_fixed = pad("rmse=0.12，与文献量级一致。", 600)
    ch2_bad = pad("rmse=0.12；另取 300 组对照。", 1200)     # 300 编造
    ch2_still_bad = pad("rmse=0.12；对照 300 组不变。", 1200)  # 重写没改好 → 保留原稿
    ch3_bad = pad("最终 rmse=0.12，提升 99 个百分点。", 800)   # 额度已尽 → 直接进发现
    llm = ScriptedLlmPort({
        "paper_outline.default": [stub_response(PAPER_OUTLINE_OK)],
        "paper_section.default": [
            stub_response({"content": ch1_bad, "digest": "d1"}),
            stub_response({"content": ch1_fixed, "digest": "d1"}),
            stub_response({"content": ch2_bad, "digest": "d2"}),
            stub_response({"content": ch2_still_bad, "digest": "d2"}),
            stub_response({"content": ch3_bad, "digest": "d3"}),
        ],
        "paper_finalize.default": [stub_response({
            "abstract": "rmse=0.12，样本 1180 行。", "keywords": ["k"],
        })],
    })
    node = PaperWritingNode(registry)

    result = node.run(make_ctx(TaskState.PAPER_WRITING, prior=paper_prior()), make_services(llm))

    assert result.status == NodeResult.NEEDS_REVIEW
    section_calls = [c for c in llm.calls if c.prompt_id == "paper_section.default"]
    assert len(section_calls) == 5  # 3 章正稿 + 2 次审计重写
    assert "0.87" in section_calls[1].variables["__repair_error"]
    assert "找不到出处" in section_calls[1].variables["__repair_error"]
    assert result.metrics["audit_rewrites"] == 2
    assert result.metrics["length_revisions"] == 2, "审计重写与字数重写共享同一额度"
    assert result.outputs["sections"][0]["content"] == ch1_fixed
    assert result.outputs["sections"][1]["content"] == ch2_bad, "重写没有减少违规就保留原稿"
    findings = result.outputs["audit_findings"]
    assert [f["scope"] for f in findings] == [
        "第2章《2 模型建立与求解》", "第3章《3 结果分析与检验》", "摘要",
    ]
    assert findings[0]["numbers"] == ["300"] and findings[1]["numbers"] == ["99"]
    assert findings[0]["kind"] == "unsourced_number"
    # 摘要里的 1180 不在材料（paper_prior 无清洗统计）也不在各章摘要 → 同样记发现
    assert findings[2]["numbers"] == ["1180"]
    meta = result.review_meta
    assert meta["impact"]["audit_findings_total"] == 3
    assert [o["id"] for o in meta["options"] if o.get("recommended")] == ["redo:PAPER_WRITING"]
    assert "数字审计发现 3 处" in result.review_reason
    assert "第2章《2 模型建立与求解》有 1 个数值" in result.review_reason
    assert any("未在冻结清单与材料中找到出处" in w for w in result.metrics["quality_warnings"])
