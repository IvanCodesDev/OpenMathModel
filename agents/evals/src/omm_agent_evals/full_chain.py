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
from collections.abc import Callable, Mapping, Sequence
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
    schedulers_for_mode,
)
from omm_agent_harness import SubagentSupervisor
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
from omm_agent_tools import failure_detail, summarize

from .scenario import (
    CANNED_ANALYSIS,
    CANNED_FORMALIZE,
    CANNED_PLANNING,
    CANNED_REDUCE,
    PROBLEM_STATEMENT,
    canned_proposer,
)

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

#: The script the sandbox agent writes and runs through ``python_run``.
CANNED_EXPERIMENT_CODE = (
    "import json\n"
    "metrics = " + json.dumps(FULL_CHAIN_METRICS) + "\n"
    "print('OMM_METRICS_JSON: ' + json.dumps(metrics))\n"
)

#: Final answer of the experiment sandbox agent (summary + the two narrative
#: keys ``ExperimentExecutionNode`` declares via ``extra_final_keys``).
CANNED_EXPERIMENT = {
    "summary": "线性趋势拟合跑通，RMSE 0.042 显著优于均值基线 0.31",
    "approach_summary": "固定种子构造运量序列，最小二乘拟合线性趋势并与均值基线对比 RMSE",
    "progress_note": "实验代码已跑通，RMSE 0.042 对比均值基线 0.31，下一步做稳健性检验。",
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

#: Marker the canned robustness script carries so the scripted tool invoker
#: can tell a validation-stage python_run from an experiment-stage one
#: without knowing step ids (both stages are sandbox agents now).
VALIDATION_CODE_MARKER = "# omm-eval: robustness checks"

#: Robustness checks the validation sandbox agent prints on its marker line
#: (three checks, all passing — the happy path stays gate-free). The slope
#: perturbation targets the global "linear trend" assumption G1 of
#: CANNED_FORMALIZE via ``assumption_id`` — the validation node insists on at
#: least one check pointing at an assumption marked critical / to_verify, and
#: a *global* one keeps the same script valid for both plan A and adopt:B.
FULL_CHAIN_ROBUSTNESS_CHECKS = [
    {
        "id": "sensitivity_slope",
        "name": "趋势斜率 ±20% 扰动",
        "passed": True,
        "value": 0.06,
        "threshold": 0.2,
        "detail": "RMSE 相对退化 6%",
        "assumption_id": "G1",
    },
    {
        "id": "bootstrap_stability",
        "name": "bootstrap 重采样稳定性",
        "passed": True,
        "value": 0.09,
        "threshold": 0.15,
        "detail": "重采样 RMSE 波动 9%",
    },
    {
        "id": "baseline_margin",
        "name": "对均值基线优势幅度",
        "passed": True,
        "value": 0.86,
        "threshold": 0.1,
        "detail": "RMSE 低于基线 86%",
    },
]

#: The script the validation sandbox agent runs (re-runs the experiment
#: logic under perturbation; scripted here, so it only prints the marker line).
#: The check list is embedded with ``repr`` (Python ``True``/``False``), not
#: ``json.dumps`` (``true``/``false`` is a NameError in a script) — the
#: published validation_checks.py artifact must itself be runnable.
CANNED_VALIDATION_CODE = (
    VALIDATION_CODE_MARKER + "\n"
    "import json\n"
    "checks = " + repr(FULL_CHAIN_ROBUSTNESS_CHECKS) + "\n"
    "print('OMM_METRICS_JSON: ' + json.dumps({'checks': checks}, ensure_ascii=False))\n"
)

#: Final answer of the validation sandbox agent.
CANNED_ROBUSTNESS = {"summary": "三项稳健性检查均在阈值内，结论稳健"}

_VALIDATION_STDOUT = (
    "OMM_METRICS_JSON: "
    + json.dumps({"checks": FULL_CHAIN_ROBUSTNESS_CHECKS}, ensure_ascii=False)
    + "\n"
)

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

# 论文阶段的分章多轮管线（doc/paper-multipass-generation-plan.md）：总编规划 →
# 逐章写作 → 统稿收口。标题与摘要沿用 CANNED_PAPER，既有断言不因此漂移；
# CANNED_PAPER 本体保留为回退单次调用（paper_writing.default）的保底答案。

CANNED_PAPER_OUTLINE = {
    "title": CANNED_PAPER["title"],
    "keywords": ["运量预测", "线性回归"],
    "notation": "| 符号 | 含义 | 单位 |\n| --- | --- | --- |\n| $y_t$ | 第 t 季度运量 | 万吨 |",
    "chapters": [
        {
            "heading": "1 问题重述",
            "brief": "背景与逐条任务要求",
            "target_chars": 600,
            "source_keys": ["problem_analysis"],
        },
        {
            "heading": "2 模型建立与求解",
            "brief": "线性趋势拟合与整数规划配置，引用 rmse=0.042",
            "target_chars": 1200,
            "source_keys": ["chosen_plan", "experiment_summary"],
        },
        {
            "heading": "3 模型检验",
            "brief": "检验结论与外推风险",
            "target_chars": 700,
            "source_keys": ["validation_summary"],
        },
    ],
}

CANNED_PAPER_FINALIZE = {
    "abstract": CANNED_PAPER["abstract"],
    "keywords": CANNED_PAPER["keywords"],
    "progress_note": "论文已按三章完成，可在论文页查看与导出。",
}


def canned_paper_section(variables: dict[str, Any]) -> str:
    """章节写作的脚本化回复：按 chapter_heading 生成，便于断言调用顺序。

    正文填充到本章目标字数：达标稿不触发节点的字数有界重写，调用序列确定。
    """
    heading = str(variables.get("chapter_heading") or "")
    lead = f"围绕 rmse=0.042 与基线 0.31 的对比展开。（{heading}）"
    target = int(variables.get("target_chars") or 600)
    return stub_response({
        "content": lead + "析" * max(target - len(lead), 0),
        "digest": f"{heading}已完成",
    })

#: results.csv content the scripted sandbox publishes on success.
FULL_CHAIN_RESULTS_CSV = b"quarter,volume_pred\n9,108.4\n"

#: Deterministic env_probe answer (sandbox-run-report reproducibility fields).
FULL_CHAIN_ENV = {
    "runtime": "python",
    "version": "3.12.0",
    "deps_hash": "sha256:evalfixture",
}

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


def robustness_success(
    checks: Sequence[dict[str, Any]] | None = None,
) -> ScriptedRun:
    """Scripted validation-stage run: prints the robustness checks marker line.

    Pass a ``checks`` list with ``passed: False`` entries to script a G3 gate.
    """
    if checks is None:
        return ScriptedRun(ok=True, stdout=_VALIDATION_STDOUT)
    stdout = "OMM_METRICS_JSON: " + json.dumps({"checks": list(checks)}, ensure_ascii=False) + "\n"
    return ScriptedRun(ok=True, stdout=stdout)


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
      payload fields as the production RecordingInvoker;
    - ``ws_write`` / ``ws_read`` / ``ws_list`` share one in-memory text store,
      so the experiment stage's ``experiment.py`` really is what the
      validation stage reads back (the production chain goes through the
      run workspace on disk the same way);
    - a ``python_run`` whose code carries :data:`VALIDATION_CODE_MARKER` is
      the validation stage's robustness script and is served from
      ``validation_run`` instead of the experiment queue, so experiment
      repair scripts (``[failure, success]`` …) keep their exact semantics.
    """

    def __init__(
        self,
        runs: Sequence[ScriptedRun],
        artifacts: ArtifactStore | None = None,
        recorder: ToolEventRecorder | None = None,
        validation_run: ScriptedRun | None = None,
    ) -> None:
        if not runs:
            raise ValueError("FakeToolInvoker needs at least one scripted run")
        self._runs = list(runs)
        self._validation_run = validation_run or robustness_success()
        self._artifacts = artifacts
        self.recorder = recorder
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []
        self.workspace_texts: dict[str, str] = {}

    def invoke(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        self.calls.append((run_id, step_id, tool_name, dict(arguments)))
        if tool_name == "ws_list":
            # 本评测无附件下发：data/ 前缀永远是空清单（数据节点如实退回摘要
            # 路径）；无前缀时列出经 ws_write 落下的文件（实验脚本）。
            prefix = str(arguments.get("prefix") or "")
            files = sorted(name for name in self.workspace_texts if name.startswith(prefix))
            result = ToolResult(status="succeeded", output={"files": files})
            self._record(step_id, tool_name, arguments, result)
            return result
        if tool_name == "ws_write":
            path, text = str(arguments.get("path") or ""), str(arguments.get("text") or "")
            self.workspace_texts[path] = text
            result = ToolResult(
                status="succeeded",
                output={"path": path, "bytes": len(text.encode("utf-8"))},
            )
            self._record(step_id, tool_name, arguments, result)
            return result
        if tool_name == "ws_read":
            path = str(arguments.get("path") or "")
            if path in self.workspace_texts:
                text = self.workspace_texts[path]
                result = ToolResult(
                    status="succeeded",
                    output={"path": path, "text": text, "truncated": False, "total_chars": len(text)},
                )
            else:
                result = ToolResult(status="failed", error=f"文件不存在：{path}")
            self._record(step_id, tool_name, arguments, result)
            return result
        if tool_name == "env_probe":
            # 沙盒执行体的环境指纹（进 sandbox-run-report 的复现面）：固定值，
            # 金轨迹才不会随本机 Python 小版本漂移。
            result = ToolResult(status="succeeded", output=dict(FULL_CHAIN_ENV))
            self._record(step_id, tool_name, arguments, result)
            return result
        if VALIDATION_CODE_MARKER in str(arguments.get("code") or ""):
            run = self._validation_run
        else:
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
        self._record(step_id, tool_name, arguments, result)
        return result

    def _record(
        self,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if self.recorder is None:
            return
        payload: dict[str, Any] = {
            "step_id": step_id,
            "tool": tool_name,
            "status": result.status,
            "duration_ms": result.duration_ms,
            "input_summary": summarize(arguments),
            "output_summary": summarize(
                result.output if result.ok else result.error
            ),
            "artifact_ids": [ref.artifact_id for ref in result.artifacts],
        }
        if not result.ok:
            detail = failure_detail(result)
            if detail:
                payload["failure_detail"] = detail
        self.recorder(EventType.TOOL_CALLED, payload)


# -- session assembly ----------------------------------------------------------


def canned_sandbox_agent(
    final: dict[str, Any], code: str
) -> Callable[[list[dict[str, str]]], str]:
    """Scripted sandbox-agent conversation: tool envelope, then final answer.

    Routing on conversation content (not call index) makes one script serve
    every repair wave — each wave assembles a fresh inner loop, so "have I
    already seen a tool observation" is exactly the wave-local question.
    """

    def reply(messages: list[dict[str, str]]) -> str:
        if any("[工具执行结果]" in message["content"] for message in messages):
            return stub_response(final)
        return json.dumps(
            {"tool": "python_run", "arguments": {"code": code}}, ensure_ascii=False
        )

    return reply


def build_full_chain_llm(
    overrides: Mapping[str, str | Callable[[dict[str, Any]], str]] | None = None,
) -> StubLlmPort:
    """Stub responses for every prompt id the six real nodes may consult.

    Two channels, mirroring production: template calls (``complete``) for the
    single-shot stages, and scripted conversations (``chat_text``) for the
    two sandbox agents — the experiment stage (writes and runs the experiment)
    and the validation stage's robustness re-run (perturbs it and prints the
    per-check verdicts the G3 gate is decided on).

    The paper stage is a multipass pipeline (outline → sections → finalize);
    ``paper_writing.default`` stays stubbed so the single-call fallback path
    remains exercisable from evals. ``overrides`` replaces individual template
    stubs (e.g. a proposer that fails for one view to drive the quorum path).
    """
    return StubLlmPort(
        {
            "problem_analysis.default": stub_response(CANNED_ANALYSIS, fenced=True),
            "data_preparation.default": stub_response(CANNED_PREPARATION),
            # 方案阶段（H3）：三路 Proposer 并行 + 一次归约 + 一次规范化（假设表 /
            # 符号表）；default 只在无监督者的装配里被消费，本会话有监督者，留着
            # 是让回落路径仍可从评测触达
            "model_planning.default": stub_response(CANNED_PLANNING),
            "model_planning.proposer": canned_proposer,
            "model_planning.reduce": stub_response(CANNED_REDUCE),
            "model_planning.formalize": stub_response(CANNED_FORMALIZE),
            "validating.default": stub_response(CANNED_VALIDATION),
            "paper_outline.default": stub_response(CANNED_PAPER_OUTLINE),
            "paper_section.default": canned_paper_section,
            "paper_finalize.default": stub_response(CANNED_PAPER_FINALIZE),
            "paper_writing.default": stub_response(CANNED_PAPER),
            **dict(overrides or {}),
        },
        chat_scripts={
            ExperimentExecutionNode.prompt_id: [
                canned_sandbox_agent(CANNED_EXPERIMENT, CANNED_EXPERIMENT_CODE)
            ],
            ValidationNode.sandbox_prompt_id: [
                canned_sandbox_agent(CANNED_ROBUSTNESS, CANNED_VALIDATION_CODE)
            ],
        },
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
    validation_run: ScriptedRun | None = None,
    graph_mode: str = "off",
) -> FullChainSession:
    """Assemble engine + all six real skill nodes over in-memory ports.

    ``validation_run`` scripts the validation stage's robustness re-run
    (default: three passing checks); script failing checks to drive the G3
    result gate.

    ``graph_mode`` is the ``OMM_GRAPH`` profile (§4.9): the eval baseline is
    ``off`` = the historical linear engine; ``linear-v1`` lets the Graph v1
    scheduler drive and ``shadow`` compares both (see ``shadow.py``).
    """
    registry = load_default_registry()
    sink = InMemoryEventSink()
    clock = FixedClock()
    ids = SequentialIdGenerator()
    artifacts = InMemoryArtifactStore()
    llm = llm or build_full_chain_llm()
    tools = FakeToolInvoker(
        tool_runs or [sandbox_success()],
        artifacts=artifacts,
        validation_run=validation_run,
    )
    services = NodeServices(
        clock=clock,
        ids=ids,
        artifacts=artifacts,
        llm=llm,
        tools=tools,
        # 与生产装配同构：数据阶段的清洗执行与验证阶段的稳健性复跑都经监督者
        # 派发。本评测不下发附件，工作区为空，清洗如实跳过（reason 说的是
        # "没有数据文件"而不是"没接线"）；复跑则真的走到（实验脚本已落工作区）。
        extras={"subagents": SubagentSupervisor()},
    )
    nodes = {
        TaskState.PROBLEM_ANALYSIS: ProblemAnalysisNode(registry),
        TaskState.DATA_PREPARATION: DataPreparationNode(registry),
        TaskState.MODEL_PLANNING: ModelPlanningNode(
            registry, require_confirmation=require_confirmation
        ),
        TaskState.EXPERIMENTING: ExperimentExecutionNode(registry),
        TaskState.VALIDATING: ValidationNode(registry),
        # G1 与 G4 同一把开关：无人值守评测两个必停门都关，只留条件门（G2/G3）
        TaskState.PAPER_WRITING: PaperWritingNode(
            registry, require_confirmation=require_confirmation
        ),
    }
    scheduler, shadow = schedulers_for_mode(graph_mode)
    engine = TaskRunEngine(
        sink=sink,
        clock=clock,
        ids=ids,
        nodes=nodes,
        services=services,
        scheduler=scheduler,
        shadow=shadow,
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


#: Template (``complete``) prompt ids in stage order. The experiment stage is
#: absent by design: it is a sandbox agent now, driven through ``chat_text``
#: conversations (see :data:`FULL_CHAIN_CHAT_SEQUENCE`). The planning stage is
#: three parallel proposers (same template id, so the recorded order is stable
#: whichever thread lands first) followed by one reduce call and one formalize
#: call (assumption table + symbol table, §9.1). The paper stage is a multipass
#: pipeline: outline → one call per chapter → finalize.
FULL_CHAIN_PROMPT_SEQUENCE = [
    "problem_analysis.default",
    "data_preparation.default",
    "model_planning.proposer",
    "model_planning.proposer",
    "model_planning.proposer",
    "model_planning.reduce",
    "model_planning.formalize",
    "validating.default",
    "paper_outline.default",
    "paper_section.default",
    "paper_section.default",
    "paper_section.default",
    "paper_finalize.default",
]

#: Conversational (``chat_text``) labels in order: one wave of a sandbox agent
#: = one tool turn + one final answer; the experiment agent first, then the
#: validation stage's robustness re-run.
FULL_CHAIN_CHAT_SEQUENCE = [
    "experiment_code.sandbox",
    "experiment_code.sandbox",
    "validating.sandbox",
    "validating.sandbox",
]

#: Exact event-type trajectory of the full-chain happy path (review gate,
#: one tool call publishing results.csv, the generated experiment.py published
#: as a code artifact and staged into the workspace, the validation stage
#: re-running it in the sandbox, paper-draft.md at the end).
FULL_CHAIN_GOLDEN_EVENT_TYPES = [
    EventType.RUN_CREATED,
    EventType.STATE_CHANGED,  # CREATED -> PROBLEM_ANALYSIS
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> DATA_PREPARATION
    EventType.STEP_STARTED,
    EventType.TOOL_CALLED,  # ws_list（画像前置：本评测无数据文件，空清单退回摘要路径）
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> MODEL_PLANNING
    EventType.STEP_STARTED,
    EventType.STEP_SUCCEEDED,
    EventType.REVIEW_REQUESTED,  # plan confirmation gate
    EventType.REVIEW_RESOLVED,  # user approves plan A
    EventType.STATE_CHANGED,  # -> EXPERIMENTING
    EventType.STEP_STARTED,
    # 实验阶段 = 沙盒 Agent 执行体：先摸清工作区与环境，再由模型自主跑码，
    # 收束前重列工作区作为断言证据。
    EventType.TOOL_CALLED,  # ws_list（任务卡里的数据文件清单）
    EventType.TOOL_CALLED,  # env_probe（报告的环境指纹）
    EventType.TOOL_CALLED,  # python_run（模型自主调用，scripted sandbox）
    EventType.TOOL_CALLED,  # ws_list（断言证据：产物清单）
    EventType.TOOL_CALLED,  # ws_write（最终脚本落工作区 experiment.py，供验证阶段复跑）
    EventType.ARTIFACT_PRODUCED,  # results.csv
    EventType.ARTIFACT_PRODUCED,  # experiment.py (generated code, reproducible)
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> VALIDATING
    EventType.STEP_STARTED,
    # 验证阶段 = LLM 判读 + 沙盒复跑：先确认工作区里有实验脚本并读回正文，
    # 再由模型在沙盒里跑稳健性检查，收束前重列工作区作为断言证据。
    EventType.TOOL_CALLED,  # ws_list（工作区清单：experiment.py 在场 + 数据文件）
    EventType.TOOL_CALLED,  # ws_read（experiment.py 正文进任务卡）
    EventType.TOOL_CALLED,  # env_probe（报告的环境指纹）
    EventType.TOOL_CALLED,  # python_run（稳健性检查脚本，scripted validation run）
    EventType.TOOL_CALLED,  # ws_list（断言证据）
    EventType.ARTIFACT_PRODUCED,  # validation_checks.py (generated code, reproducible)
    EventType.STEP_SUCCEEDED,
    EventType.STATE_CHANGED,  # -> PAPER_WRITING
    EventType.STEP_STARTED,
    EventType.ARTIFACT_PRODUCED,  # paper-draft.md
    EventType.STEP_SUCCEEDED,
    EventType.REVIEW_REQUESTED,  # G4 定稿交付闸门（必停）：审计发现进卡片
    EventType.REVIEW_RESOLVED,  # user confirms delivery
    EventType.RUN_COMPLETED,
]
