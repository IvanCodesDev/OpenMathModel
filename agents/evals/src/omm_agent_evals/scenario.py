"""Golden end-to-end scenario: one full modeling TaskRun on the real stack.

Composition (nothing mocked except the LLM, by design):

- PROBLEM_ANALYSIS / MODEL_PLANNING: real skill nodes from agents/skills,
  driven by a StubLlmPort with canned-but-schema-valid answers; planning
  raises the human confirmation gate.
- EXPERIMENTING: executes REAL Python in the subprocess sandbox (least
  squares fit), producing a metrics.json artifact.
- VALIDATING: checks the experiment metrics against a threshold.
- PAPER_WRITING: renders a Markdown report artifact from accumulated outputs.

The whole thing is driven through the worker queue exactly like production
will be, so the golden trajectory covers queue → lease → recover → advance.
"""

from __future__ import annotations

import json
from pathlib import Path

from omm_agent_core import (
    EventType,
    FixedClock,
    NodeResult,
    SequentialIdGenerator,
    TaskState,
)
from omm_agent_skills import (
    ModelPlanningNode,
    ProblemAnalysisNode,
    StubLlmPort,
    load_default_registry,
    stub_response,
)
from omm_worker import WorkerConfig, WorkerRuntime

PROBLEM_STATEMENT = "某物流公司需要根据历史运量预测下季度运量并优化车辆配置……"

CANNED_ANALYSIS = {
    "viability": "ok",
    "missing_info": [],
    "title": "运量预测与车辆配置优化",
    "problem_type": "预测+优化",
    "objectives": ["预测下季度运量", "给出车辆配置方案"],
    "constraints": ["车辆总数不超过 50", "预算上限 200 万"],
    "data_requirements": ["历史运量时间序列", "车辆成本表"],
    "key_assumptions": ["运量趋势短期线性"],
    "subquestions": [
        {"id": "q1", "text": "基于历史序列预测下季度运量", "depends_on": []},
        {"id": "q2", "text": "按预测运量求最优车辆配置", "depends_on": ["q1"]},
    ],
}

CANNED_PLANNING = {
    "plans": [
        {
            "id": "A",
            "name": "线性回归 + 整数规划",
            "approach": "最小二乘拟合运量趋势，再以 MILP 求最优车辆配置",
            "steps": ["拟合线性趋势", "构建配置模型", "灵敏度分析"],
            "risks": ["非线性冲击场景失效"],
        },
        {
            "id": "B",
            "name": "时间序列 + 启发式",
            "approach": "ARIMA 预测 + 遗传算法搜索配置",
            "steps": ["定阶建模", "编码搜索", "对比验证"],
            "risks": ["样本过短导致过拟合"],
        },
    ],
    "recommended_plan_id": "A",
    "rationale": "数据量小且趋势近线性，方案 A 可解释性与评审友好度更高",
}

#: 方案阶段（H3）：三视角 Proposer 并行各回一案，归约桩把它们收成 CANNED_PLANNING
#: 的 A/B 两案（多出归约字段 role / source_views；投影只取契约五键）。
CANNED_PROPOSALS_BY_VIEW = {
    "机理建模": {
        "name": "库存-运量动力学",
        "approach": "把运量当作受季节因子驱动的一阶动力学过程",
        "steps": ["辨识季节因子", "拟合动力学参数", "外推下季度"],
        "risks": ["外部冲击不可解释"],
        "fit": "运量趋势近线性，机理项可退化为线性趋势",
    },
    "数据驱动": {
        "name": "时间序列 + 启发式",
        "approach": "ARIMA 预测 + 遗传算法搜索配置",
        "steps": ["定阶建模", "编码搜索", "对比验证"],
        "risks": ["样本过短导致过拟合"],
        "fit": "样本较短，需谨慎定阶",
    },
    "运筹优化": {
        "name": "线性回归 + 整数规划",
        "approach": "最小二乘拟合运量趋势，再以 MILP 求最优车辆配置",
        "steps": ["拟合线性趋势", "构建配置模型", "灵敏度分析"],
        "risks": ["非线性冲击场景失效"],
        "fit": "车辆数与预算都是硬约束，整数规划直接可解",
    },
}


def canned_proposer(variables: dict) -> str:
    """提议人桩：按 view_name 回对应视角的方案（callable 桩拿到的是渲染变量）。"""
    return stub_response(CANNED_PROPOSALS_BY_VIEW[variables["view_name"]])


CANNED_REDUCE = {
    **CANNED_PLANNING,
    "plans": [
        {**CANNED_PLANNING["plans"][0], "role": "primary", "source_views": ["operations_research"]},
        {**CANNED_PLANNING["plans"][1], "role": "baseline", "source_views": ["data_driven"]},
    ],
    "dropped": ["机理建模：动力学项退化为线性趋势，已并入方案 A"],
    "progress_note": "三路提议归约为两案：推荐线性回归 + 整数规划，时间序列作对照基线。",
}

#: 归约之后的规范化（H3 切片 2）：假设表 + 符号表随方案卡进 G1，也是论文
#: 「模型假设」「符号说明」两节的底稿。
CANNED_FORMALIZE = {
    "assumptions": [
        {"id": "G1", "text": "运量趋势在规划期内近似线性", "scope": "global", "basis": "数据画像", "impact": "high", "status": "to_verify"},
        {"id": "G2", "text": "单位运量成本为常数", "scope": "global", "basis": "简化需要", "impact": "medium", "status": "confirmed"},
        {"id": "A1", "text": "车辆数与预算均为硬约束", "scope": "A", "basis": "题面", "impact": "high", "status": "critical"},
        {"id": "B1", "text": "样本平稳可定阶", "scope": "B", "basis": "领域常识", "impact": "medium", "status": "to_verify"},
    ],
    "symbols": [
        {"symbol": "t \\in \\mathcal{T}", "kind": "set", "definition": "季度索引", "unit": None, "range": "1…T", "plan_id": None},
        {"symbol": "y_t", "kind": "parameter", "definition": "第 t 季度运量", "unit": "万吨", "range": "≥ 0", "plan_id": None},
        {"symbol": "n_k", "kind": "variable", "definition": "k 型车辆配置数", "unit": "辆", "range": "非负整数", "plan_id": "A"},
        {"symbol": "\\hat{y}_{t+1}", "kind": "variable", "definition": "下季度运量预测", "unit": "万吨", "range": "≥ 0", "plan_id": "B"},
    ],
}

#: Real python executed in the sandbox: closed-form least squares on y≈2x+1.
EXPERIMENT_CODE = """\
import json
xs = [0.0, 1.0, 2.0, 3.0, 4.0]
ys = [1.02, 2.98, 5.01, 7.02, 8.97]
n = len(xs)
sx, sy = sum(xs), sum(ys)
sxx = sum(x * x for x in xs)
sxy = sum(x * y for x, y in zip(xs, ys))
a = (n * sxy - sx * sy) / (n * sxx - sx * sx)
b = (sy - a * sx) / n
rmse = (sum((a * x + b - y) ** 2 for x, y in zip(xs, ys)) / n) ** 0.5
metrics = {"a": round(a, 4), "b": round(b, 4), "rmse": round(rmse, 4)}
with open("metrics.json", "w", encoding="utf-8") as handle:
    json.dump(metrics, handle)
print(json.dumps(metrics))
"""


class DataPreparationNode:
    def run(self, ctx, services):
        return NodeResult.succeeded(
            outputs={"profile_summary": "5 行历史运量样例，无缺失值，已确认单位为万吨"}
        )


class ExperimentingNode:
    """Runs the recommended plan's first experiment inside the sandbox."""

    def run(self, ctx, services):
        if services.tools is None:
            return NodeResult.failed("no tool invoker configured")
        result = services.tools.invoke(
            ctx.run_id, ctx.step_id, "python_run", {"code": EXPERIMENT_CODE}
        )
        if not result.ok:
            return NodeResult.failed(f"experiment failed: {result.error}")
        metrics = json.loads(result.output["stdout"].strip().splitlines()[-1])
        return NodeResult.succeeded(
            outputs={"metrics": metrics},
            metrics={"sandbox_exit_code": result.output["exit_code"]},
            artifacts=result.artifacts,
        )


class ValidatingNode:
    RMSE_THRESHOLD = 0.1

    def run(self, ctx, services):
        metrics = ctx.prior_outputs.get(TaskState.EXPERIMENTING.value, {}).get("metrics")
        if not metrics:
            return NodeResult.failed("no experiment metrics to validate")
        if metrics["rmse"] > self.RMSE_THRESHOLD:
            return NodeResult.failed(
                f"rmse {metrics['rmse']} above threshold {self.RMSE_THRESHOLD}"
            )
        return NodeResult.succeeded(
            outputs={"validation": "passed", "rmse": metrics["rmse"]}
        )


class PaperWritingNode:
    def run(self, ctx, services):
        if services.artifacts is None:
            return NodeResult.failed("no artifact store configured")
        analysis = ctx.prior_outputs[TaskState.PROBLEM_ANALYSIS.value]
        planning = ctx.prior_outputs[TaskState.MODEL_PLANNING.value]
        metrics = ctx.prior_outputs[TaskState.EXPERIMENTING.value]["metrics"]
        report = "\n".join(
            [
                "# 建模报告（自动生成草稿）",
                "",
                f"## 问题类型\n\n{analysis['problem_type']}",
                f"## 推荐方案\n\n{planning['recommended_plan_id']}：{planning['rationale']}",
                "## 实验指标",
                "",
                f"- a = {metrics['a']}",
                f"- b = {metrics['b']}",
                f"- RMSE = {metrics['rmse']}",
            ]
        )
        ref = services.artifacts.put(
            run_id=ctx.run_id,
            kind="report",
            name="report.md",
            content=report.encode("utf-8"),
            media_type="text/markdown",
            producer_step=ctx.step_id,
        )
        return NodeResult.succeeded(
            outputs={"report_uri": ref.uri, "report_sha256": ref.sha256},
            artifacts=(ref,),
        )


def build_llm() -> StubLlmPort:
    return StubLlmPort(
        {
            "problem_analysis.default": stub_response(CANNED_ANALYSIS, fenced=True),
            # worker 运行时注入了子代理监督者：方案阶段走三路提议 + 归约 + 规范化
            "model_planning.proposer": canned_proposer,
            "model_planning.reduce": stub_response(CANNED_REDUCE),
            "model_planning.formalize": stub_response(CANNED_FORMALIZE),
        }
    )


def build_runtime(root: Path, require_confirmation: bool = True) -> WorkerRuntime:
    registry = load_default_registry()
    nodes = {
        TaskState.PROBLEM_ANALYSIS: ProblemAnalysisNode(registry),
        TaskState.DATA_PREPARATION: DataPreparationNode(),
        TaskState.MODEL_PLANNING: ModelPlanningNode(
            registry, require_confirmation=require_confirmation
        ),
        TaskState.EXPERIMENTING: ExperimentingNode(),
        TaskState.VALIDATING: ValidatingNode(),
        TaskState.PAPER_WRITING: PaperWritingNode(),
    }
    return WorkerRuntime(
        WorkerConfig(root=root, python_timeout_s=30.0),
        nodes=nodes,
        llm=build_llm(),
        worker_id="worker_eval",
        clock=FixedClock(),
        ids=SequentialIdGenerator(),
    )


#: The exact event-type trajectory of the golden run (review gate included).
GOLDEN_EVENT_TYPES = [
    EventType.RUN_CREATED,
    EventType.STATE_CHANGED,  # CREATED -> PROBLEM_ANALYSIS
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> DATA_PREPARATION
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> MODEL_PLANNING
    EventType.STEP_STARTED,
    # 三路 Proposer 子代理并行：每路 spawn + result 两条监督者审计（六条 TOOL_CALLED，
    # 三路的先后随线程调度而变，事件类型序列不变）
    EventType.TOOL_CALLED,
    EventType.TOOL_CALLED,
    EventType.TOOL_CALLED,
    EventType.TOOL_CALLED,
    EventType.TOOL_CALLED,
    EventType.TOOL_CALLED,
    EventType.STEP_SUCCEEDED,
    EventType.REVIEW_REQUESTED,
    EventType.REVIEW_RESOLVED,  # user approves plan A
    EventType.STATE_CHANGED,  # -> EXPERIMENTING
    EventType.STEP_STARTED,
    EventType.TOOL_CALLED,  # python_run in the sandbox
    EventType.ARTIFACT_PRODUCED,  # metrics.json
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> VALIDATING
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> PAPER_WRITING
    EventType.STEP_STARTED,
    EventType.ARTIFACT_PRODUCED,  # report.md
    EventType.STEP_SUCCEEDED,
    EventType.RUN_COMPLETED,
]
