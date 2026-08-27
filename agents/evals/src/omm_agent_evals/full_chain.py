"""Full-chain scenario: all six REAL LLM skill nodes driven by the engine.

Companion to ``scenario.py`` (the worker-queue golden run, which mixes real
and simplified nodes but executes real Python in the sandbox). This scenario
flips the trade-off: every node is the real implementation from
``agents/skills`` — including DataPreparationNode, ExperimentExecutionNode,
ValidationNode and PaperWritingNode — while the two nondeterministic ports
are substituted:

- LLM: ``StubLlmPort`` with schema-valid canned answers per prompt_id;
- python_run tool: a scripted ``FakeToolInvoker`` (below) that mimics the
  sandbox contract — publishes produced files to the artifact store and
  records a TOOL_CALLED event through ``engine.record_external`` exactly
  like the production ``RecordingInvoker`` does.

The run is driven directly through ``TaskRunEngine`` (run_until_blocked →
resolve_review → run_until_blocked), so this covers the review gate, the
experiment repair round, review rejection + retry, and failure + retry
recovery on the REAL node chain. Output conventions follow scenario.py:
canned constants + build functions + a golden event-type trajectory.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from omm_agent_core import (
    ArtifactRef,
    ArtifactStore,
    EventType,
    FixedClock,
    InMemoryArtifactStore,
    InMemoryEventSink,
    NodeServices,
    SequentialIdGenerator,
    TaskRunEngine,
    TaskRunSnapshot,
    TaskState,
    ToolResult,
    replay_events,
)
from omm_agent_skills import (
    DataPreparationNode,
    ExperimentExecutionNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    StubLlmPort,
    ValidationNode,
    load_default_registry,
    stub_response,
)
from omm_agent_tools import summarize

from .scenario import CANNED_ANALYSIS, CANNED_PLANNING, PROBLEM_STATEMENT

# -- canned answers for the four stages scenario.py does not stub -------------
# Same problem domain as CANNED_ANALYSIS/CANNED_PLANNING (freight-volume
# forecasting), each shaped to pass its prompt's output_schema.

CANNED_PREPARATION = {
    "profile_summary": "历史运量为 8 个季度的时间序列，无缺失，趋势近线性，可直接用于建模",
    "datasets": [
        {
            "name": "历史运量",
            "source": "需构造",
            "fields": ["quarter 季度序号", "volume 运量（万吨）"],
            "quality_risks": ["样本量小", "节假日效应未标注"],
        },
        {
            "name": "车辆成本表",
            "source": "需构造",
            "fields": ["vehicle_type 车型", "cost 单车成本（万元）"],
            "quality_risks": ["成本口径可能随油价波动"],
        },
    ],
    "preparation_steps": ["固定随机种子构造合成运量序列", "划分训练/验证窗口"],
    "missing_value_strategy": "线性插值",
    "outlier_strategy": "3σ 截断",
    "derived_features": ["季度移动平均"],
}

#: Metrics the scripted sandbox run prints on its OMM_METRICS_JSON line.
FULL_CHAIN_METRICS = {"rmse": 0.042, "baseline_rmse": 0.31}

CANNED_EXPERIMENT = {
    "approach_summary": "固定种子构造运量序列，最小二乘拟合线性趋势并与均值基线对比 RMSE",
    "code": (
        "import json\n"
        "metrics = " + json.dumps(FULL_CHAIN_METRICS) + "\n"
        "print('OMM_METRICS_JSON: ' + json.dumps(metrics))\n"
    ),
}

CANNED_VALIDATION = {
    "verdict": "pass",
    "checks": [
        {"name": "结果合理性", "result": "pass", "note": "拟合斜率与历史增速同量级"},
        {"name": "稳健性", "result": "warn", "note": "样本仅 8 期，外推区间不宜过长"},
    ],
    "risks": ["长周期外推失真"],
    "validation_summary": "线性趋势拟合显著优于均值基线，结论可信，但样本较短，外推需谨慎",
}

CANNED_PAPER = {
    "title": "基于线性回归与整数规划的运量预测与车辆配置优化",
    "abstract": (
        "本文以最小二乘拟合运量线性趋势，再以整数规划求解车辆配置，"
        "实验 RMSE 显著优于均值基线。"
    ),
    "keywords": ["运量预测", "线性回归", "整数规划"],
    "sections": [
        {"heading": "问题重述", "content": "预测下季度运量并优化车辆配置。"},
        {"heading": "模型建立与求解", "content": "最小二乘拟合趋势，MILP 求最优配置。"},
        {"heading": "模型检验", "content": "结论可信，但样本较短，外推需谨慎。"},
    ],
}

#: results.csv content the scripted sandbox publishes on success.
FULL_CHAIN_RESULTS_CSV = b"quarter,volume_pred\n9,108.4\n"

_SUCCESS_STDOUT = "OMM_METRICS_JSON: " + json.dumps(FULL_CHAIN_METRICS) + "\n"
_FAILURE_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "main.py", line 3, in <module>\n'
    "NameError: name 'xs' is not defined"
)


# -- scripted python_run tool --------------------------------------------------


@dataclass(frozen=True)
class ScriptedRun:
    """One scripted sandbox execution consumed by :class:`FakeToolInvoker`."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    #: (file name, content) pairs published to the artifact store on success.
    files: tuple[tuple[str, bytes], ...] = ()


def sandbox_success(
    stdout: str = _SUCCESS_STDOUT,
    files: tuple[tuple[str, bytes], ...] = (("results.csv", FULL_CHAIN_RESULTS_CSV),),
) -> ScriptedRun:
    return ScriptedRun(ok=True, stdout=stdout, files=files)


def sandbox_failure(stderr: str = _FAILURE_STDERR) -> ScriptedRun:
    return ScriptedRun(ok=False, error="python exited with code 1", stderr=stderr)


#: Signature of the TOOL_CALLED recording callback (engine.record_external).
ToolEventRecorder = Callable[[EventType, dict[str, Any]], Any]


class FakeToolInvoker:
    """Scripted ToolInvoker mirroring the sandbox contract for evals.

    Follows the FakeToolInvoker from agents/skills tests (queued outcomes,
    the last one repeats; every call recorded), extended with the two
    behaviours the engine assembly relies on:

    - successful runs publish their files through the artifact store, so
      ArtifactRefs in outputs point at content that really exists;
    - every call emits a TOOL_CALLED event via ``recorder`` (bound to
      ``engine.record_external`` by the session builder), with the same
      payload fields as the production RecordingInvoker.
    """

    def __init__(
        self,
        runs: Sequence[ScriptedRun],
        artifacts: ArtifactStore | None = None,
        recorder: ToolEventRecorder | None = None,
    ) -> None:
        if not runs:
            raise ValueError("FakeToolInvoker needs at least one scripted run")
        self._runs = list(runs)
        self._artifacts = artifacts
        self.recorder = recorder
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def invoke(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        self.calls.append((run_id, step_id, tool_name, dict(arguments)))
        run = self._runs.pop(0) if len(self._runs) > 1 else self._runs[0]
        if run.ok:
            refs: tuple[ArtifactRef, ...] = ()
            if self._artifacts is not None:
                refs = tuple(
                    self._artifacts.put(
                        run_id=run_id,
                        kind="table",
                        name=name,
                        content=content,
                        media_type="text/csv",
                        producer_step=step_id,
                    )
                    for name, content in run.files
                )
            result = ToolResult(
                status="succeeded",
                output={"exit_code": 0, "stdout": run.stdout, "stderr": run.stderr},
                artifacts=refs,
            )
        else:
            result = ToolResult(
                status="failed",
                error=run.error or "python exited with code 1",
                output={"exit_code": 1, "stdout": run.stdout, "stderr": run.stderr},
            )
        if self.recorder is not None:
            self.recorder(
                EventType.TOOL_CALLED,
                {
                    "step_id": step_id,
                    "tool": tool_name,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "input_summary": summarize(arguments),
                    "output_summary": summarize(
                        result.output if result.ok else result.error
                    ),
                    "artifact_ids": [ref.artifact_id for ref in result.artifacts],
                },
            )
        return result


# -- session assembly ----------------------------------------------------------


def build_full_chain_llm() -> StubLlmPort:
    """Stub responses for all six prompt ids of the default registry."""
    return StubLlmPort(
        {
            "problem_analysis.default": stub_response(CANNED_ANALYSIS, fenced=True),
            "data_preparation.default": stub_response(CANNED_PREPARATION),
            "model_planning.default": stub_response(CANNED_PLANNING),
            "experiment_code.default": stub_response(CANNED_EXPERIMENT),
            "validating.default": stub_response(CANNED_VALIDATION),
            "paper_writing.default": stub_response(CANNED_PAPER),
        }
    )


@dataclass
class FullChainSession:
    """One assembled full-chain run with every observable port exposed."""

    engine: TaskRunEngine
    snapshot: TaskRunSnapshot
    sink: InMemoryEventSink
    llm: StubLlmPort
    tools: FakeToolInvoker
    artifacts: InMemoryArtifactStore
    project_id: str = field(default="proj_eval_full_chain")

    def replay(self) -> TaskRunSnapshot:
        """Rebuild the snapshot from nothing but the emitted event log."""
        return replay_events(self.snapshot.run_id, self.project_id, self.sink.events)


def build_full_chain_session(
    tool_runs: Sequence[ScriptedRun] | None = None,
    llm: StubLlmPort | None = None,
    require_confirmation: bool = True,
    record_tool_events: bool = True,
    project_id: str = "proj_eval_full_chain",
) -> FullChainSession:
    """Assemble engine + all six real skill nodes over in-memory ports."""
    registry = load_default_registry()
    sink = InMemoryEventSink()
    clock = FixedClock()
    ids = SequentialIdGenerator()
    artifacts = InMemoryArtifactStore()
    llm = llm or build_full_chain_llm()
    tools = FakeToolInvoker(tool_runs or [sandbox_success()], artifacts=artifacts)
    services = NodeServices(
        clock=clock, ids=ids, artifacts=artifacts, llm=llm, tools=tools
    )
    nodes = {
        TaskState.PROBLEM_ANALYSIS: ProblemAnalysisNode(registry),
        TaskState.DATA_PREPARATION: DataPreparationNode(registry),
        TaskState.MODEL_PLANNING: ModelPlanningNode(
            registry, require_confirmation=require_confirmation
        ),
        TaskState.EXPERIMENTING: ExperimentExecutionNode(registry),
        TaskState.VALIDATING: ValidationNode(registry),
        TaskState.PAPER_WRITING: PaperWritingNode(registry),
    }
    engine = TaskRunEngine(
        sink=sink, clock=clock, ids=ids, nodes=nodes, services=services
    )
    snapshot, _ = engine.create_run(
        project_id, inputs={"problem_statement": PROBLEM_STATEMENT}
    )
    if record_tool_events:
        # Same wiring as the production worker: tool calls are recorded on the
        # single emit→apply path so sequence numbers stay engine-owned.
        tools.recorder = lambda event_type, payload: engine.record_external(
            snapshot, event_type, payload
        )
    return FullChainSession(
        engine=engine,
        snapshot=snapshot,
        sink=sink,
        llm=llm,
        tools=tools,
        artifacts=artifacts,
        project_id=project_id,
    )


#: Prompt ids in stage order — the happy path calls each exactly once.
FULL_CHAIN_PROMPT_SEQUENCE = [
    "problem_analysis.default",
    "data_preparation.default",
    "model_planning.default",
    "experiment_code.default",
    "validating.default",
    "paper_writing.default",
]

#: Exact event-type trajectory of the full-chain happy path (review gate,
#: one tool call publishing results.csv, the generated experiment.py published
#: as a code artifact, paper-draft.md at the end).
FULL_CHAIN_GOLDEN_EVENT_TYPES = [
    EventType.RUN_CREATED,
    EventType.STATE_CHANGED,  # CREATED -> PROBLEM_ANALYSIS
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> DATA_PREPARATION
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> MODEL_PLANNING
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.REVIEW_REQUESTED,  # plan confirmation gate
    EventType.REVIEW_RESOLVED,  # user approves plan A
    EventType.STATE_CHANGED,  # -> EXPERIMENTING
    EventType.STEP_STARTED,
    EventType.TOOL_CALLED,  # python_run (scripted sandbox)
    EventType.ARTIFACT_PRODUCED,  # results.csv
    EventType.ARTIFACT_PRODUCED,  # experiment.py (generated code, reproducible)
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> VALIDATING
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> PAPER_WRITING
    EventType.STEP_STARTED,
    EventType.ARTIFACT_PRODUCED,  # paper-draft.md
    EventType.STEP_SUCCEEDED,
    EventType.RUN_COMPLETED,
]
