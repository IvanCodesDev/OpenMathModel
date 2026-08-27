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
from omm_agent_skills import (
    PYTHON_TOOL_NAME,
    DataPreparationNode,
    ExperimentExecutionNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    ScriptedLlmPort,
    StubLlmPort,
    ValidationNode,
    chosen_plan,
    extract_json,
    load_default_registry,
    render_paper_markdown,
    stub_response,
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


def make_services(llm):
    return NodeServices(
        clock=FixedClock(),
        ids=SequentialIdGenerator(),
        artifacts=InMemoryArtifactStore(),
        llm=llm,
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

EXPERIMENT_OK = {
    "approach_summary": "构造泊松需求数据，MILP 简化为贪心近似并与随机基线对比",
    "code": "print('OMM_METRICS_JSON: {\"rmse\": 0.12}')",
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


def test_data_preparation_requires_prior_analysis(registry):
    llm = StubLlmPort({"data_preparation.default": stub_response(PREPARATION_OK)})
    node = DataPreparationNode(registry)
    ctx = make_ctx(TaskState.DATA_PREPARATION, prior={})

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


# -- ExperimentExecutionNode ---------------------------------------------------


def test_experiment_generates_code_and_runs_tool(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    tools = FakeToolInvoker([tool_success(artifacts=(artifact(),))])
    node = ExperimentExecutionNode(registry)
    services = make_full_services(llm, tools)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED
    run_id, step_id, tool_name, arguments = tools.calls[0]
    assert (run_id, step_id, tool_name) == ("run_1", "step_1", PYTHON_TOOL_NAME)
    assert arguments["code"] == EXPERIMENT_OK["code"]
    assert result.outputs["metrics"] == {"rmse": 0.12}
    assert "核心指标" in result.outputs["experiment_summary"]
    assert "results.csv" in result.outputs["experiment_summary"]
    assert result.metrics == {"llm_attempts": 1, "code_rounds": 1}
    # 工具产物之外，生成的实验脚本本身也发布为 code 产物（可复现）
    assert [ref.kind for ref in result.artifacts] == ["table", "code"]
    code_ref = result.artifacts[-1]
    assert code_ref.uri.endswith("experiment.py")
    assert services.artifacts.blobs[code_ref.uri].decode("utf-8") == EXPERIMENT_OK["code"]
    # 首轮提示词的失败反馈为占位「无」，可用库为默认口径
    assert llm.calls[0].variables["error_feedback"] == "无"
    assert llm.calls[0].variables["available_packages"] == "无（仅 Python 标准库）"
    # 选中的方案（recommended A）进入提示词
    assert "整数规划" in llm.calls[0].variables["chosen_plan"]


def test_experiment_passes_detected_packages_to_prompt(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    tools = FakeToolInvoker([tool_success()])
    node = ExperimentExecutionNode(registry, available_packages="numpy、pandas")
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert llm.calls[0].variables["available_packages"] == "numpy、pandas"


def test_experiment_feeds_runtime_error_back_and_regenerates_once(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    tools = FakeToolInvoker([tool_failure(), tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.SUCCEEDED
    assert result.metrics["code_rounds"] == 2
    assert len(tools.calls) == 2
    # 第二轮的提示词变量必须携带第一轮的运行时错误与代码
    retry_vars = llm.calls[1].variables
    assert "NameError" in retry_vars["error_feedback"]
    assert retry_vars["previous_code"] == EXPERIMENT_OK["code"]


def test_experiment_fails_after_rounds_exhausted(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    tools = FakeToolInvoker([tool_failure()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.FAILED
    assert "after 2 rounds" in result.error
    assert "NameError" in result.error
    assert len(tools.calls) == 2


def test_experiment_without_tool_invoker_fails_cleanly(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_through_planning())

    result = node.run(ctx, make_full_services(llm, tools=None))

    assert result.status == NodeResult.FAILED
    assert "no tool invoker" in result.error
    assert llm.calls == []


def test_experiment_requires_prior_planning(registry):
    llm = StubLlmPort({"experiment_code.default": stub_response(EXPERIMENT_OK)})
    tools = FakeToolInvoker([tool_success()])
    node = ExperimentExecutionNode(registry)
    ctx = make_ctx(TaskState.EXPERIMENTING, prior=prior_with_analysis())

    result = node.run(ctx, make_full_services(llm, tools))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert tools.calls == []


# -- ValidationNode ------------------------------------------------------------


def test_validation_happy_path(registry):
    llm = StubLlmPort({"validating.default": stub_response(VALIDATION_OK)})
    node = ValidationNode(registry)
    prior = prior_through_planning()
    prior[TaskState.EXPERIMENTING.value] = {
        "experiment_summary": "贪心近似 rmse=0.12",
        "metrics": {"rmse": 0.12},
        "stdout_tail": "OMM_METRICS_JSON: ...",
    }
    ctx = make_ctx(TaskState.VALIDATING, prior=prior)

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["verdict"] == "concerns"
    assert "需求率参数敏感" in result.outputs["validation_summary"]
    assert json.loads(llm.calls[0].variables["metrics"]) == {"rmse": 0.12}


def test_validation_requires_prior_experiment(registry):
    llm = StubLlmPort({"validating.default": stub_response(VALIDATION_OK)})
    node = ValidationNode(registry)
    ctx = make_ctx(TaskState.VALIDATING, prior=prior_through_planning())

    result = node.run(ctx, make_services(llm))

    assert result.status == NodeResult.FAILED
    assert "missing required input" in result.error
    assert llm.calls == []


# -- PaperWritingNode ----------------------------------------------------------


def paper_prior():
    prior = prior_through_planning()
    prior[TaskState.EXPERIMENTING.value] = {
        "experiment_summary": "贪心近似 rmse=0.12",
        "metrics": {"rmse": 0.12},
    }
    prior[TaskState.VALIDATING.value] = dict(VALIDATION_OK)
    return prior


def test_paper_writing_publishes_markdown_artifact(registry):
    llm = StubLlmPort({"paper_writing.default": stub_response(PAPER_OK)})
    node = PaperWritingNode(registry)
    services = make_services(llm)
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED
    assert result.outputs["title"] == PAPER_OK["title"]
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert ref.kind == "paper"
    assert ref.media_type == "text/markdown"
    stored = services.artifacts.blobs[ref.uri].decode("utf-8")
    assert "# 基于整数规划的门店选址优化" in stored
    assert "## 模型检验" in stored
    assert "**关键词**：整数规划；选址" in stored
    # 检验结论进入了提示词变量
    assert "需求率参数敏感" in llm.calls[0].variables["validation_summary"]


def test_paper_writing_without_artifact_store_fails(registry):
    llm = StubLlmPort({"paper_writing.default": stub_response(PAPER_OK)})
    node = PaperWritingNode(registry)
    services = NodeServices(
        clock=FixedClock(), ids=SequentialIdGenerator(), artifacts=None, llm=llm
    )
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    assert result.status == NodeResult.FAILED
    assert "artifact" in result.error


def test_render_paper_markdown_skips_blank_sections():
    markdown = render_paper_markdown(
        {"title": "题", "sections": [{"heading": "", "content": ""}, "not-a-dict"]}
    )
    assert markdown == "# 题\n"
