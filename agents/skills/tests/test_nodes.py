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
from omm_agent_harness import SubagentSupervisor
from omm_agent_skills import (
    CLEANING_PROMPT_ID,
    DEFAULT_HARDWARE_NOTE,
    PYTHON_TOOL_NAME,
    DataPreparationNode,
    LlmCall,
    ExperimentExecutionNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    ScriptedLlmPort,
    StubLlmPort,
    ValidationNode,
    chosen_plan,
    extract_json,
    gpu_hardware_note,
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
    先跑后有才是诚实的时序。
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

    def invoke(self, run_id, step_id, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == PYTHON_TOOL_NAME:
            self.python_calls.append((run_id, step_id, tool_name, dict(arguments)))
            self._ran = True
            return self._runs.pop(0) if len(self._runs) > 1 else self._runs[0]
        if tool_name == "ws_list":
            files = self._files + (self._files_after_run if self._ran else [])
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


def cleaning_llm(plan=None, summary="按方案清洗完成"):
    """数据阶段的双通道端口：模板调用出方案，会话调用跑清洗。"""
    return StubLlmPort(
        {"data_preparation.default": stub_response(plan or PREPARATION_OK)},
        chat_scripts={
            CLEANING_PROMPT_ID: sandbox_script({"summary": summary}, code=CLEANING_CODE)
        },
    )


def cleaning_services(llm, tools):
    services = make_services(llm, tools)
    services.extras["subagents"] = SubagentSupervisor()
    return services


def cleaning_tools(stdout=None, **kwargs):
    return SandboxToolInvoker(
        runs=[tool_success(stdout=stdout or cleaning_stdout(**kwargs))],
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


def test_paper_writing_multipass_publishes_markdown_artifact(registry):
    llm = multipass_paper_stub()
    node = PaperWritingNode(registry)
    services = make_services(llm)
    progress = []
    services.extras["progress"] = progress.append
    ctx = make_ctx(TaskState.PAPER_WRITING, prior=paper_prior())

    result = node.run(ctx, services)

    assert result.status == NodeResult.SUCCEEDED
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
    # 进度事件：骨架一条 + 每章一条，index/total 正确
    assert [event["kind"] for event in progress] == [
        "paper_outline", "paper_section", "paper_section", "paper_section",
    ]
    assert progress[0]["total"] == 3
    assert [event["index"] for event in progress[1:]] == [1, 2, 3]
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

    assert result.status == NodeResult.SUCCEEDED
    section_calls = [call for call in llm.calls if call.prompt_id == "paper_section.default"]
    # 滚动摘要：第一章无前文，第三章能看到前两章摘要
    assert section_calls[0].variables["previous_digests"] == "无（本章是全文第一章）"
    assert "第1章《1 问题重述》" in section_calls[2].variables["previous_digests"]
    assert "第2章《2 模型建立与求解》" in section_calls[2].variables["previous_digests"]
    # 材料路由：第一章只带 problem_analysis；第三章未指定 source_keys → 全量四份
    assert "问题分析结果" in section_calls[0].variables["materials"]
    assert "已确认的建模方案" not in section_calls[0].variables["materials"]
    for label in ("问题分析结果", "已确认的建模方案", "实验过程摘要", "检验结论"):
        assert label in section_calls[2].variables["materials"]
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

    assert result.status == NodeResult.SUCCEEDED
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

    assert result.status == NodeResult.SUCCEEDED
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

    assert result.status == NodeResult.SUCCEEDED
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

    assert result.status == NodeResult.SUCCEEDED
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

    assert result.status == NodeResult.SUCCEEDED
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

    assert result.status == NodeResult.SUCCEEDED
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
