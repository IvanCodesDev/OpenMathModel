"""LLM-backed skill nodes for the task state machine.

Model output is untrusted data: every response is parsed and validated
against the prompt's output schema before it may become step outputs. On a
violation the node makes exactly ONE repair attempt (feeding the error back),
then fails the step — silent acceptance of malformed structure is how bad
plans reach experiments.

Since v3.10 the "one try + one repair" discipline is DRIVEN BY the harness
inner-loop engine (``run_inner_loop``, single-shot profile ``max_turns=1,
repairs=1``) instead of a private for-loop here: every LLM call in the system
now exits through the §5.3 mapping (schema violation → E120, identical
retries → E331 no-progress guard). The ``LlmPort.complete(prompt_id,
variables)`` protocol is unchanged — an adapter folds the loop's
conversational repair increment back into the D4 repair variables
(``__repair_error`` / ``__previous_output``), so Stub/Scripted/EngineLlmPort
implementations and the final repair prompt stay byte-identical to the
pre-migration behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from omm_agent_core import NodeContext, NodeResult, NodeServices, TaskState
from omm_agent_core.models import ToolResult
from omm_agent_harness import (
    LoopBudget,
    LoopTask,
    Message,
    Reply,
    ResultEnvelope,
    RunBudget,
    SandboxAssertion,
    SandboxTask,
    SpawnSpec,
    Usage,
    run_inner_loop,
    run_sandbox_task,
)

from .chat_adapter import supports_chat, text_protocol_chat, tool_protocol_note
from .prompt_registry import PromptRegistry, PromptTemplate
from .schema import validate

_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$", re.MULTILINE)

#: Tool name the experiment node invokes. skills and tools are sibling
#: packages with no dependency edge, so the name is an assembly-time contract
#: with omm_agent_tools.PythonSandbox.TOOL_NAME rather than an import.
PYTHON_TOOL_NAME = "python_run"

#: What the experiment prompt says about third-party packages when the
#: runtime does not report a detected list. Runtimes that share an
#: interpreter with the sandbox should pass their real detection result.
DEFAULT_AVAILABLE_PACKAGES = "无（仅 Python 标准库）"

#: What the experiment prompt says about local hardware when the runtime does
#: not report a probe result. Conservative default: CPU-only wording keeps
#: generated code runnable anywhere; runtimes that probed a real GPU (see
#: engine_glue._sandbox_hardware) pass the GPU-first wording instead.
DEFAULT_HARDWARE_NOTE = "未检测到可用 GPU：请用 CPU 实现并控制计算规模。"


def gpu_hardware_note(gpu_descriptor: str) -> str:
    """Prompt wording once the runtime probed a CUDA GPU the sandbox can use.

    Counterpart of :data:`DEFAULT_HARDWARE_NOTE`; both phrasings live here so
    the prompt's hardware vocabulary has a single owner. ``gpu_descriptor``
    is the probe's factual device string (e.g. "NVIDIA GeForce RTX 4090,
    24.0 GB VRAM").
    """
    return (
        f"检测到可用 GPU：{gpu_descriptor}，PyTorch CUDA 可用。"
        "计算密集的核心计算（大规模矩阵运算、迭代求解、模型训练）应优先放到 GPU 上执行；"
        "设备选择必须自适应并保留 CPU 回退，禁止硬编码 cuda。"
    )

#: Marker line experiment scripts must print for structured metrics capture.
#: Anchored to a full line so prose that merely mentions the marker (or a
#: brace later on the same line) cannot produce a bogus capture.
_METRICS_LINE = re.compile(r"^OMM_METRICS_JSON:\s*(\{.*\})\s*$", re.MULTILINE)

_STDOUT_TAIL_CHARS = 2000


def extract_json(raw: str) -> Any:
    """Parse a JSON object out of an LLM answer.

    Tolerates markdown fences and stray prose around the outermost object —
    and nothing more exotic than that.
    """
    candidate = raw.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    candidate = _FENCE.sub("", candidate).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        return json.loads(candidate[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", raw, 0)


#: Extracts the error line the loop engine writes into its repair message
#: (fixed D4 format, owned by harness ``loops._repair_message``).
_REPAIR_ERROR_LINE = re.compile(r"^__repair_error: (.*)$", re.MULTILINE)


def _port_chat(llm: Any, template: PromptTemplate, variables: dict[str, Any]):
    """Adapt ``LlmPort.complete(prompt_id, variables)`` to the loop's ChatFn.

    The loop repairs conversationally (assistant echo + user repair message);
    the port protocol is "template id + variables". This adapter folds the
    conversation increment back into the D4 repair variables, so every port
    implementation — and the final prompt the provider sees — behaves exactly
    as before the migration. Token usage is accounted inside the port
    (EngineLlmPort → usage records + budget governor), so replies carry zero
    usage here; the loop's tally is not the billing source on this path.
    """

    def chat(messages: Sequence[Message]) -> Reply:
        payload = dict(variables)
        last = messages[-1]
        if last.role == "user":
            match = _REPAIR_ERROR_LINE.search(last.content)
            if match:
                payload["__repair_error"] = match.group(1)
                for message in reversed(messages):
                    if message.role == "assistant":
                        payload["__previous_output"] = message.content[:2000]
                        break
        raw = llm.complete(template.id, payload)
        return Reply(content=raw, tool_calls=(), usage=Usage(0, 0, 0), model="llm-port")

    return chat


def complete_validated(
    services: NodeServices,
    template: PromptTemplate,
    variables: dict[str, Any],
) -> tuple[dict[str, Any] | None, int, str | None]:
    """One normal try + one repair try, gated by the template's output schema.

    Shared by the template-method base AND multi-call orchestrations (paper
    pipeline). Driven by the harness inner loop in its single-shot profile:
    the (parsed, attempts, error) contract is unchanged, attempts = actual
    LLM calls (1 on first-try success, 2 when the repair ran).
    """
    task = LoopTask(
        task_id=template.id,
        # The port protocol renders the template itself at transport time;
        # this message only seeds the conversation structure the loop appends
        # its repair increment to — the adapter never reads it.
        messages=(Message(role="user", content=f"[template:{template.id}]"),),
        validator=lambda value: validate(value, template.output_schema),
        parser=extract_json,
        budget=LoopBudget(max_turns=1, repairs=1),
    )
    outcome = run_inner_loop(
        task, chat=_port_chat(services.llm, template, variables)
    )
    if outcome.ok:
        return outcome.value, outcome.llm_calls, None
    return None, outcome.llm_calls, outcome.last_error


class LlmSkillNode:
    """Template-method base: build variables → complete → parse → validate."""

    prompt_id: str = ""
    state: TaskState | None = None

    def __init__(self, registry: PromptRegistry) -> None:
        self._registry = registry

    # -- per-skill hooks ---------------------------------------------------

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        raise NotImplementedError

    def to_result(self, parsed: dict[str, Any], attempts: int) -> NodeResult:
        return NodeResult.succeeded(outputs=parsed, metrics={"llm_attempts": attempts})

    # -- node protocol -------------------------------------------------------

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        if services.llm is None:
            return NodeResult.failed("no LLM port configured for this run")
        template = self._registry.get(self.prompt_id)

        try:
            variables = self.build_variables(ctx)
        except KeyError as exc:
            return NodeResult.failed(f"missing required input: {exc}")

        input_problems = validate(variables, template.input_schema)
        if input_problems:
            return NodeResult.failed(
                "prompt input invalid: " + "; ".join(input_problems)
            )

        parsed, attempts, error = self._complete_validated(template, variables, services)
        if parsed is None:
            return NodeResult.failed(
                f"model output failed validation after {attempts} attempts: {error}"
            )
        return self.to_result(parsed, attempts)

    def _complete_validated(
        self,
        template: PromptTemplate,
        variables: dict[str, Any],
        services: NodeServices,
    ) -> tuple[dict[str, Any] | None, int, str | None]:
        return complete_validated(services, template, variables)


class ProblemAnalysisNode(LlmSkillNode):
    prompt_id = "problem_analysis.default"
    state = TaskState.PROBLEM_ANALYSIS

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        return {
            "problem_statement": ctx.inputs["problem_statement"],
            "attachments_summary": str(ctx.inputs.get("attachments_summary", "无")),
        }

    def to_result(self, parsed: dict[str, Any], attempts: int) -> NodeResult:
        # 准入门（prompt v4 的 viability 判定）：输入不构成可建模问题时在
        # 第一阶段就停止，绝不带着编造的题面把后续阶段跑完烧掉用户的调用费。
        if str(parsed.get("viability") or "ok") == "insufficient":
            missing = [
                str(item).strip()
                for item in parsed.get("missing_info") or []
                if str(item).strip()
            ]
            detail = "、".join(missing) if missing else "题目正文与求解目标"
            return NodeResult.failed(
                f"题目信息不足，无法启动建模：缺少{detail}。"
                "请在新任务中提供完整题面（可附题目文档与数据文件）后重新发起。",
                metrics={"llm_attempts": attempts, "viability": "insufficient"},
            )
        return super().to_result(parsed, attempts)


class ModelPlanningNode(LlmSkillNode):
    prompt_id = "model_planning.default"
    state = TaskState.MODEL_PLANNING

    def __init__(self, registry: PromptRegistry, require_confirmation: bool = True) -> None:
        super().__init__(registry)
        # Plan confirmation is the product's human gate (roadmap: 方案 A/B 生成、
        # 用户确认). Evals/automation may disable it explicitly.
        self._require_confirmation = require_confirmation

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = ctx.prior_outputs.get(TaskState.PROBLEM_ANALYSIS.value)
        if not analysis:
            raise KeyError("'PROBLEM_ANALYSIS outputs'")
        data_profile = str(
            ctx.prior_outputs.get(TaskState.DATA_PREPARATION.value, {}).get(
                "profile_summary", "无数据画像"
            )
        )
        # G2 决策台账透传：用户选了「改用原始数据」时方案阶段必须知情，
        # 否则方案会默认建立在清洗后数据上。
        if ctx.review_decisions.get(TaskState.DATA_PREPARATION.value) == "use_raw":
            data_profile += "（用户已确认改用原始数据，清洗产物不采用）"
        return {
            "problem_analysis": json.dumps(analysis, ensure_ascii=False),
            "data_profile": data_profile,
        }

    def to_result(self, parsed: dict[str, Any], attempts: int) -> NodeResult:
        plan_ids = [plan.get("id") for plan in parsed.get("plans", [])]
        if parsed.get("recommended_plan_id") not in plan_ids:
            return NodeResult.failed(
                "recommended_plan_id does not reference a returned plan"
            )
        if self._require_confirmation:
            return NodeResult.needs_review(
                reason="请确认建模方案（A/B）后继续实验",
                outputs={**parsed, "llm_attempts": attempts},
            )
        return NodeResult.succeeded(
            outputs=parsed, metrics={"llm_attempts": attempts}
        )


def _require_outputs(ctx: NodeContext, state: TaskState) -> Mapping[str, Any]:
    outputs = ctx.prior_outputs.get(state.value)
    if not outputs:
        raise KeyError(f"'{state.value} outputs'")
    return outputs


def chosen_plan(planning: Mapping[str, Any]) -> dict[str, Any]:
    """The plan the run proceeds with: recommended if present, else the first.

    The product's approval gate offers "adopt current plan" (the recommended
    one) or "redo planning" — there is no per-plan pick, so downstream stages
    resolve the plan the same way the approval card presents it.
    """
    plans = [plan for plan in planning.get("plans") or [] if isinstance(plan, Mapping)]
    recommended = planning.get("recommended_plan_id")
    for plan in plans:
        if plan.get("id") == recommended:
            return dict(plan)
    if plans:
        return dict(plans[0])
    raise KeyError("'MODEL_PLANNING outputs.plans'")


#: 数据阶段工具名（装配期契约，与 omm_agent_tools 的注册名对齐，不 import）。
TABLE_PROFILE_TOOL = "table_profile"
WS_LIST_TOOL = "ws_list"

#: 执行侧把附件数据文件下发到工作区的目录约定。
DATA_DIR_PREFIX = "data/"

#: 单次画像的表格文件上限（更多文件只会稀释判读信号）。
_PROFILE_FILE_LIMIT = 5


def _list_data_files(ctx: NodeContext, services: NodeServices) -> list[str]:
    """工作区 data/ 下的文件清单；无工具或清单失败一律给空表。

    画像与清洗派发问的是同一个问题，共用这一次 ws_list——同一步里列两遍目录
    会在活动流里留下两条重复的工具事件，而答案必然相同。
    """
    if services.tools is None:
        return []
    listing = services.tools.invoke(
        ctx.run_id, ctx.step_id, WS_LIST_TOOL, {"prefix": DATA_DIR_PREFIX}
    )
    if not listing.ok:
        return []
    return [str(path) for path in listing.output.get("files") or []]


def _profile_data_tables(
    ctx: NodeContext, services: NodeServices, data_files: Sequence[str]
) -> str:
    """工作区 data/ 下 CSV 的确定性画像 → 提示词附注段；无工具/无文件给空串。

    画像数字由 table_profile 代码统计产出（§1.3 原则 5），LLM 只判读质量与
    就绪度；工具缺席或失败时如实退回"仅附件摘要"路径，不静默编造。
    """
    if services.tools is None:
        return ""
    tables = [
        path for path in data_files if str(path).lower().endswith(".csv")
    ][:_PROFILE_FILE_LIMIT]
    notes: list[str] = []
    for path in tables:
        result = services.tools.invoke(
            ctx.run_id, ctx.step_id, TABLE_PROFILE_TOOL, {"path": str(path)}
        )
        if result.ok:
            notes.append(
                f"### {path}\n" + json.dumps(result.output, ensure_ascii=False)
            )
    if not notes:
        return ""
    return (
        "以下为数据文件的确定性画像（由代码统计产出，判读与引用时不得改写数字）：\n"
        + "\n".join(notes)
    )


# ── 沙盒执行体的节点侧桥（H3 前置刀）────────────────────────────────────────
#
# run_sandbox_task 的全部依赖（chat/工具执行器/工作区访问器/环境指纹）从
# NodeServices 就地装配：工具走 services.tools（RecordingInvoker 统一审计
# 与档位），会话走 LlmPort 的 chat_text 扩展（文本协议，见 chat_adapter）。

#: 沙盒任务允许的工具面（§7.1 五件套；env_probe 由节点侧预先探测，不给模型）。
SANDBOX_TOOL_NAMES = ("python_run", "ws_write", "ws_read", "ws_list")

#: 显式种子（§7.1 任务卡字段）：合成数据/抽样必须使用的固定种子。
SANDBOX_SEEDS = {"random_seed": 42}


class _SandboxCapture:
    """节点侧执行证据：最后一次 python_run 的 stdout/指标 + 全部产物。

    sandbox-run-report.v1 不含 stdout 与指标本体（那是产物与断言的事），但
    节点输出（stage_outputs 正文）需要它们——在工具执行器上就地截获，不改
    执行体的报告形状。
    """

    def __init__(self) -> None:
        self.stdout = ""
        self.metrics: dict[str, Any] = {}
        self.artifacts: list[Any] = []
        self._seen: set[str] = set()

    def observe(self, result: ToolResult) -> None:
        output = result.output or {}
        self.stdout = str(output.get("stdout") or "")
        parsed: dict[str, Any] = {}
        for match in _METRICS_LINE.finditer(self.stdout):
            try:
                candidate = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
        self.metrics = parsed
        for ref in result.artifacts:
            if ref.artifact_id not in self._seen:
                self._seen.add(ref.artifact_id)
                self.artifacts.append(ref)


def _sandbox_tool_executor(
    ctx: NodeContext,
    services: NodeServices,
    capture: _SandboxCapture,
    allowed: Sequence[str] = SANDBOX_TOOL_NAMES,
):
    """ToolExecutor：合成 ToolCall → services.tools.invoke（审计/计费在途中）。"""
    allowed_set = set(allowed)

    def execute(calls):
        results: list[ToolResult] = []
        for call in calls:
            if call.name not in allowed_set:
                results.append(
                    ToolResult(
                        status="failed",
                        error=(
                            f"工具 {call.name} 不在本任务允许清单"
                            f"（可用：{', '.join(sorted(allowed_set))}）"
                        ),
                    )
                )
                continue
            result = services.tools.invoke(
                ctx.run_id, ctx.step_id, call.name, dict(call.arguments)
            )
            if call.name == PYTHON_TOOL_NAME:
                capture.observe(result)
            results.append(result)
        return results

    return execute


def _workspace_files(ctx: NodeContext, services: NodeServices) -> list[str]:
    listing = services.tools.invoke(ctx.run_id, ctx.step_id, WS_LIST_TOOL, {})
    if not listing.ok:
        return []
    return [str(path) for path in listing.output.get("files") or []]


def _workspace_reader(ctx: NodeContext, services: NodeServices):
    def read_text(path: str) -> str:
        result = services.tools.invoke(
            ctx.run_id, ctx.step_id, "ws_read", {"path": path}
        )
        if not result.ok:
            raise FileNotFoundError(result.error or f"无法读取 {path}")
        return str(result.output.get("text") or "")

    return read_text


def _env_fingerprint(ctx: NodeContext, services: NodeServices) -> dict[str, Any]:
    result = services.tools.invoke(ctx.run_id, ctx.step_id, "env_probe", {})
    return dict(result.output) if result.ok else {}


def _publish_code_callback(
    ctx: NodeContext,
    services: NodeServices,
    capture: _SandboxCapture,
    filename: str,
):
    """publish_code 回调：脚本进内容寻址存储，产物引用并入节点产出。"""

    def publish(code: str) -> str:
        if services.artifacts is None or not code:
            return ""
        ref = services.artifacts.put(
            ctx.run_id,
            "code",
            filename,
            code.encode("utf-8"),
            "text/x-python",
            ctx.step_id,
        )
        capture.artifacts.append(ref)
        return ref.artifact_id

    return publish


def _report_shape_problems(report: dict[str, Any]) -> list[str]:
    """sandbox-run-report.v1 的形状点检（E520 校验器；契约包不在依赖边内）。"""
    if not isinstance(report, dict):
        return ["报告必须是 JSON 对象"]
    required = (
        "status", "attempts", "assertions", "produced_artifacts",
        "seeds", "env_fingerprint", "usage",
    )
    return [f"missing required key: {key}" for key in required if key not in report]


# ── 数据准备：LLM 方案 → 清洗沙盒执行 → G2 影响面闸门 ─────────────────────────

#: G2 触发阈值（§9.1/§11.1 拍板值）：删行比例超过 5% 即请人确认。
G2_ROW_DELETION_THRESHOLD = 0.05

#: G2 审批选项（控制面投影按此渲染决策卡；id 进 review_decisions 供下游读取）。
G2_OPTIONS = (
    {
        "id": "adopt_cleaned",
        "label": "采用清洗结果",
        "description": "以 cleaned/ 目录的清洗后数据继续建模（推荐）",
        "recommended": True,
    },
    {
        "id": "use_raw",
        "label": "改用原始数据",
        "description": "忽略清洗产物，后续阶段直接使用 data/ 原始文件",
    },
    {
        "id": "reject",
        "label": "退回调整",
        "description": "重新执行数据准备阶段（重做画像、方案与清洗）",
    },
)

CLEANING_PROMPT_ID = "data_cleaning.sandbox"


def _normalize_column(name: Any) -> str:
    return str(name).strip().lower()


def _cleaning_impact(
    metrics: Mapping[str, Any], target_columns: Sequence[str]
) -> dict[str, Any]:
    """影响面统计（数字来自清洗脚本的标记行，节点只做除法与求交）。"""
    try:
        rows_before = int(metrics.get("rows_before") or 0)
        rows_after = int(metrics.get("rows_after") or 0)
    except (TypeError, ValueError):
        rows_before, rows_after = 0, 0
    imputed = [
        str(column).strip()
        for column in (metrics.get("imputed_columns") or [])
        if str(column).strip()
    ]
    ratio = 0.0
    if rows_before > 0:
        ratio = max(0.0, 1.0 - rows_after / rows_before)
    normalized_targets = {_normalize_column(col) for col in target_columns}
    imputed_targets = sorted(
        {col for col in imputed if _normalize_column(col) in normalized_targets}
    )
    return {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_deleted_ratio": round(ratio, 4),
        "imputed_columns": imputed,
        "imputed_target_columns": imputed_targets,
    }


class DataPreparationNode(LlmSkillNode):
    """数据画像判读 + 准备方案（LLM）→ 清洗沙盒执行（子代理）→ G2 条件闸门。

    清洗执行是尽力而为的增强：监督者/会话端口/数据文件任一缺席都**如实降级**
    为「仅方案」（cleaning.executed=false + 原因），不阻塞数据阶段——但绝不
    假装执行过。G2 只在清洗真实执行且影响面超阈值（删行 >5% 或目标列被插补）
    时触发（§9.1），选项与决策台账见 G2_OPTIONS。
    """

    prompt_id = "data_preparation.default"
    state = TaskState.DATA_PREPARATION

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        summary = str(ctx.inputs.get("attachments_summary") or "无")
        profile_note = str(ctx.inputs.get("table_profile_note") or "")
        if profile_note:
            summary = profile_note if summary in ("", "无") else f"{profile_note}\n\n{summary}"
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "attachments_summary": summary,
        }

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        data_files = _list_data_files(ctx, services)
        note = _profile_data_tables(ctx, services, data_files)
        if note:
            ctx = replace(ctx, inputs={**dict(ctx.inputs), "table_profile_note": note})
        base = super().run(ctx, services)
        if base.status != NodeResult.SUCCEEDED:
            return base

        capture = _SandboxCapture()
        cleaning = self._execute_cleaning(
            ctx, services, base.outputs, capture, data_files
        )
        outputs = {**base.outputs, "cleaning": cleaning}
        artifacts = base.artifacts + tuple(capture.artifacts)

        gate = self._g2_review(cleaning)
        if gate is not None:
            reason, meta = gate
            return NodeResult.needs_review(
                reason=reason,
                outputs=outputs,
                review_meta=meta,
                metrics=base.metrics,
                artifacts=artifacts,
            )
        return NodeResult.succeeded(
            outputs=outputs, metrics=base.metrics, artifacts=artifacts
        )

    # -- cleaning execution ---------------------------------------------------

    def _execute_cleaning(
        self,
        ctx: NodeContext,
        services: NodeServices,
        parsed: Mapping[str, Any],
        capture: _SandboxCapture,
        data_files: Sequence[str],
    ) -> dict[str, Any]:
        def skipped(reason: str) -> dict[str, Any]:
            return {"executed": False, "reason": reason}

        if services.tools is None:
            return skipped("未配置工具端口，跳过清洗执行")
        supervisor = (services.extras or {}).get("subagents")
        if supervisor is None:
            return skipped("未配置子代理监督者，跳过清洗执行")
        if not supports_chat(services.llm):
            return skipped("模型端口不支持会话式调用，跳过清洗执行")
        data_files = list(data_files)
        if not data_files:
            return skipped("工作区没有已下发的数据文件，无需清洗")

        governor = (services.extras or {}).get("budget_governor")
        budgets: RunBudget = (
            governor.subagent_slice() if governor is not None else RunBudget()
        )
        if budgets.max_sandbox_runs < 1 or budgets.max_llm_calls < 2:
            return skipped("剩余预算不足以派发清洗子代理")

        template = self._registry.get(CLEANING_PROMPT_ID)
        plan_slice = {
            key: parsed.get(key)
            for key in (
                "profile_summary",
                "preparation_steps",
                "missing_value_strategy",
                "outlier_strategy",
                "target_columns",
            )
            if parsed.get(key) is not None
        }
        system_prompt = template.render({
            "preparation_plan": json.dumps(plan_slice, ensure_ascii=False),
            "data_files": "\n".join(f"- {path}" for path in data_files),
        })

        final_answer: dict[str, Any] = {}
        task = SandboxTask(
            task_id=f"{ctx.step_id}:cleaning",
            goal="按数据准备方案清洗 data/ 数据文件，产出 cleaned/ 数据与影响面统计",
            system_prompt=system_prompt,
            task_brief=tool_protocol_note(SANDBOX_TOOL_NAMES),
            assertions=(
                SandboxAssertion(
                    id="cleaned_files_exist",
                    description="cleaned/ 目录产出至少一个清洗后数据文件",
                    check=lambda evidence: (
                        any(name.startswith("cleaned/") for name in evidence.files),
                        "；".join(
                            name for name in evidence.files if name.startswith("cleaned/")
                        ) or "工作区没有 cleaned/ 下的文件",
                    ),
                ),
                SandboxAssertion(
                    id="impact_stats_reported",
                    description=(
                        "打印影响面统计标记行 OMM_METRICS_JSON"
                        "（rows_before/rows_after/imputed_columns）"
                    ),
                    check=_impact_stats_check,
                ),
            ),
            seeds=dict(SANDBOX_SEEDS),
            max_runs=max(1, min(SandboxTask.max_runs, budgets.max_sandbox_runs)),
            extra_final_keys=(),
        )

        llm_calls = {"count": 0}
        chat = text_protocol_chat(
            services.llm,
            label=CLEANING_PROMPT_ID,
            on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
        )
        executor = _sandbox_tool_executor(ctx, services, capture)
        fingerprint = _env_fingerprint(ctx, services)

        def runner(_spec: SpawnSpec) -> ResultEnvelope:
            report = run_sandbox_task(
                task,
                chat=chat,
                execute_tools=executor,
                workspace_files=lambda: _workspace_files(ctx, services),
                read_text=_workspace_reader(ctx, services),
                env_fingerprint=fingerprint,
                publish_code=_publish_code_callback(
                    ctx, services, capture, "cleaning.py"
                ),
                on_final_answer=final_answer.update,
            )
            return ResultEnvelope(
                status="done",
                output=report,
                usage=Usage(0, 0, int(report["usage"]["duration_ms"])),
            )

        envelope = supervisor.spawn(
            SpawnSpec(
                kind="sandbox",
                goal=task.goal,
                context_slice={
                    "preparation_plan": plan_slice,
                    "data_files": data_files,
                },
                toolset=tuple(SANDBOX_TOOL_NAMES),
                tool_tier="execute",
                budgets=budgets,
                output_schema_id="sandbox-run-report.v1",
            ),
            runner,
            parent_tier="execute",
            output_validator=_report_shape_problems,
        )

        if not envelope.ok or envelope.output is None:
            return {
                "executed": False,
                "reason": f"清洗子代理未完成（{envelope.status}"
                + (f"，{envelope.error_code}" if envelope.error_code else "")
                + "）；后续阶段按原始数据继续",
            }

        report = envelope.output
        target_columns = [
            str(col).strip()
            for col in (parsed.get("target_columns") or [])
            if str(col).strip()
        ]
        impact = _cleaning_impact(capture.metrics, target_columns)
        return {
            "executed": True,
            "status": str(report.get("status") or "failed"),
            "attempts": int(report.get("attempts") or 0),
            "llm_calls": llm_calls["count"],
            "summary": str(final_answer.get("summary") or ""),
            "target_columns": target_columns,
            "final_code_artifact": str(report.get("final_code_artifact") or ""),
            "produced_artifacts": list(report.get("produced_artifacts") or []),
            **impact,
        }

    # -- G2 gate ----------------------------------------------------------------

    @staticmethod
    def _g2_review(
        cleaning: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        if not cleaning.get("executed") or cleaning.get("status") != "passed":
            return None
        triggers: list[str] = []
        ratio = float(cleaning.get("rows_deleted_ratio") or 0.0)
        if ratio > G2_ROW_DELETION_THRESHOLD:
            triggers.append(f"删除了 {ratio:.1%} 的数据行（阈值 5%）")
        imputed_targets = list(cleaning.get("imputed_target_columns") or [])
        if imputed_targets:
            triggers.append(f"目标列被插补（{'、'.join(imputed_targets)}）")
        if not triggers:
            return None
        reason = "数据清洗影响面较大：" + "；".join(triggers) + "。请确认数据处理方式"
        meta = {
            "gate": "G2",
            "decision_type": "generic",
            "title": reason,
            "options": [dict(option) for option in G2_OPTIONS],
            "impact": {
                "rows_before": cleaning.get("rows_before"),
                "rows_after": cleaning.get("rows_after"),
                "rows_deleted_ratio": cleaning.get("rows_deleted_ratio"),
                "imputed_columns": cleaning.get("imputed_columns"),
                "imputed_target_columns": imputed_targets,
            },
        }
        return reason, meta


def _impact_stats_check(evidence) -> tuple[bool, str]:
    metrics = evidence.metrics
    problems: list[str] = []
    for key in ("rows_before", "rows_after"):
        value = metrics.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"{key} 缺失或不是非负整数")
    if not isinstance(metrics.get("imputed_columns"), list):
        problems.append("imputed_columns 缺失或不是数组")
    if problems:
        return False, "影响面统计不合格：" + "；".join(problems)
    return True, (
        f"rows_before={metrics['rows_before']}，rows_after={metrics['rows_after']}，"
        f"imputed_columns={metrics['imputed_columns']}"
    )


class ExperimentExecutionNode(LlmSkillNode):
    """实验阶段 = 沙盒 Agent 执行体（H3 前置刀：迁移自单发生成+私有重试环）。

    模型在多轮会话里写码/跑码/读反馈直到验收断言通过或 R2 预算（6 次运行，
    §5.4）耗尽；验收以确定性断言为准（最后一次运行成功 + 指标标记行在场），
    模型自述完成无效。输出形状与迁移前一致（approach_summary/metrics/
    stdout_tail/experiment_summary/progress_note），下游（验证/论文）零改动；
    新增 sandbox_report（sandbox-run-report.v1 形状）供复现与评测。
    """

    prompt_id = "experiment_code.sandbox"
    state = TaskState.EXPERIMENTING

    #: R2 运行预算（§4.7/§5.4 拍板值）：整个实验步骤最多 6 次沙箱运行。
    max_sandbox_runs = 6

    def __init__(
        self,
        registry: PromptRegistry,
        available_packages: str = DEFAULT_AVAILABLE_PACKAGES,
        hardware_note: str = DEFAULT_HARDWARE_NOTE,
    ) -> None:
        super().__init__(registry)
        # Which third-party packages the sandbox interpreter really offers:
        # the prompt whitelists imports against this, so code quality scales
        # with the environment instead of being pinned to stdlib-only.
        self._available_packages = available_packages
        # Sandbox hardware facts (GPU availability): the prompt steers heavy
        # computation onto the local GPU when the runtime probed one, and
        # stays CPU-conservative otherwise.
        self._hardware_note = hardware_note

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        preparation = dict(
            ctx.prior_outputs.get(TaskState.DATA_PREPARATION.value) or {}
        )
        decision = ctx.review_decisions.get(TaskState.DATA_PREPARATION.value)
        if decision:
            # G2 决策台账进任务卡：模型据此选 cleaned/ 或 data/（用户理由
            # AI 不得改写，这里只透传选项 id 与含义）。
            preparation["user_decision"] = {
                "option_id": decision,
                "meaning": {
                    "adopt_cleaned": "用户已确认采用清洗后的数据（cleaned/）",
                    "use_raw": "用户已选择改用原始数据（data/），忽略清洗产物",
                }.get(decision, decision),
            }
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(chosen_plan(planning), ensure_ascii=False),
            "data_preparation": (
                json.dumps(preparation, ensure_ascii=False) if preparation else "无"
            ),
            "available_packages": self._available_packages,
            "hardware_note": self._hardware_note,
        }

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        if services.llm is None:
            return NodeResult.failed("no LLM port configured for this run")
        if services.tools is None:
            return NodeResult.failed("no tool invoker configured for this run")
        if not supports_chat(services.llm):
            return NodeResult.failed(
                "LLM 端口不支持会话式调用（缺 chat_text）：实验执行体需要多轮"
                "写码/跑码会话，装配缺陷请检查运行时接线"
            )
        template = self._registry.get(self.prompt_id)

        try:
            variables = self.build_variables(ctx)
        except KeyError as exc:
            return NodeResult.failed(f"missing required input: {exc}")
        data_files = [
            path
            for path in _workspace_files(ctx, services)
            if path.startswith(DATA_DIR_PREFIX) or path.startswith("cleaned/")
        ]
        variables["data_files"] = (
            "\n".join(f"- {path}" for path in data_files)
            if data_files
            else "无（按数据准备方案构造合成数据）"
        )
        input_problems = validate(variables, template.input_schema)
        if input_problems:
            return NodeResult.failed(
                "prompt input invalid: " + "; ".join(input_problems)
            )

        plan = chosen_plan(_require_outputs(ctx, TaskState.MODEL_PLANNING))
        capture = _SandboxCapture()
        final_answer: dict[str, Any] = {}
        llm_calls = {"count": 0}
        task = SandboxTask(
            task_id=f"{ctx.step_id}:experiment",
            goal=(
                f"实现并运行方案「{plan.get('name') or plan.get('id') or '选定方案'}」"
                "的实验代码，产出真实指标与结果表"
            ),
            system_prompt=template.render(variables),
            task_brief=tool_protocol_note(SANDBOX_TOOL_NAMES),
            assertions=(
                SandboxAssertion(
                    id="run_ok",
                    description="实验脚本经 python_run 成功运行（退出码 0）",
                    check=_experiment_run_ok_check,
                ),
                SandboxAssertion(
                    id="metrics_reported",
                    description="打印核心指标标记行 OMM_METRICS_JSON（含基线对比）",
                    check=_experiment_metrics_check,
                ),
            ),
            seeds=dict(SANDBOX_SEEDS),
            max_runs=self.max_sandbox_runs,
            extra_final_keys=(
                (
                    "approach_summary",
                    "实现思路摘要：算法、数据来源/构造方式、评估口径与基线设置，不超过 200 字",
                ),
                (
                    "progress_note",
                    "两三句面向用户的进度汇报（实现了什么、对比基线看什么指标、下一步验证什么），口语化",
                ),
            ),
        )
        chat = text_protocol_chat(
            services.llm,
            label=self.prompt_id,
            on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
        )
        report = run_sandbox_task(
            task,
            chat=chat,
            execute_tools=_sandbox_tool_executor(ctx, services, capture),
            workspace_files=lambda: _workspace_files(ctx, services),
            read_text=_workspace_reader(ctx, services),
            env_fingerprint=_env_fingerprint(ctx, services),
            publish_code=_publish_code_callback(ctx, services, capture, "experiment.py"),
            on_final_answer=final_answer.update,
        )

        node_metrics = {
            "llm_attempts": llm_calls["count"],
            "code_rounds": int(report["usage"]["runs"]),
            "waves": int(report["attempts"]),
        }
        if report["status"] != "passed":
            failed_assertions = [
                f"[{item['id']}] {item['detail']}"
                for item in report["assertions"]
                if not item["passed"]
            ]
            detail = "；".join(failed_assertions) or "沙盒执行未通过验收"
            return NodeResult.failed(
                f"experiment sandbox failed after {report['attempts']} wave(s), "
                f"{report['usage']['runs']} run(s): {detail}",
                metrics=node_metrics,
            )

        approach = str(final_answer.get("approach_summary") or "")
        metrics = dict(capture.metrics)
        summary_bits = [approach]
        if metrics:
            summary_bits.append("核心指标：" + json.dumps(metrics, ensure_ascii=False))
        if capture.artifacts:
            names = [
                ref.uri.rstrip("/").rsplit("/", 1)[-1] for ref in capture.artifacts
            ]
            summary_bits.append("产物文件：" + "、".join(names))

        return NodeResult.succeeded(
            outputs={
                "approach_summary": approach,
                "metrics": metrics,
                "stdout_tail": capture.stdout[-_STDOUT_TAIL_CHARS:],
                "experiment_summary": "\n".join(bit for bit in summary_bits if bit),
                # 面向用户的进度叙述：执行轨迹的正文段
                "progress_note": str(final_answer.get("progress_note") or ""),
                # 复现与评测面：断言逐条结果/种子/环境指纹/预算用量
                "sandbox_report": report,
            },
            metrics=node_metrics,
            artifacts=tuple(capture.artifacts),
        )


def _experiment_run_ok_check(evidence) -> tuple[bool, str]:
    last = evidence.last_run
    if last is None:
        return False, "尚未用 python_run 运行任何代码"
    if not last.ok:
        output = last.output or {}
        stderr = str(output.get("stderr") or "").strip()
        detail = str(last.error or last.status)
        if stderr:
            detail += "\nstderr（尾部）：" + stderr[-1500:]
        return False, detail
    return True, "最后一次运行成功"


def _experiment_metrics_check(evidence) -> tuple[bool, str]:
    if not evidence.metrics:
        return False, (
            "未捕获核心指标：脚本必须原样打印一行 "
            'OMM_METRICS_JSON: {"指标名": 数值, ...}（独占一行）'
        )
    return True, "指标标记行已捕获：" + json.dumps(
        dict(evidence.metrics), ensure_ascii=False
    )


class ValidationNode(LlmSkillNode):
    prompt_id = "validating.default"
    state = TaskState.VALIDATING

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        experiment = _require_outputs(ctx, TaskState.EXPERIMENTING)
        return {
            "chosen_plan": json.dumps(chosen_plan(planning), ensure_ascii=False),
            "experiment_summary": str(
                experiment.get("experiment_summary")
                or experiment.get("stdout_tail")
                or "无"
            ),
            "metrics": json.dumps(dict(experiment.get("metrics") or {}), ensure_ascii=False),
        }


def render_paper_markdown(document: Mapping[str, Any]) -> str:
    """Structured paper draft → the markdown artifact users download."""
    lines = [f"# {str(document.get('title') or '建模论文草稿').strip()}", ""]
    abstract = str(document.get("abstract") or "").strip()
    if abstract:
        lines += ["## 摘要", "", abstract, ""]
    keywords = [str(k).strip() for k in document.get("keywords") or [] if str(k).strip()]
    if keywords:
        lines += ["**关键词**：" + "；".join(keywords), ""]
    for section in document.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        heading = str(section.get("heading") or "").strip()
        content = str(section.get("content") or "").strip()
        if heading:
            lines += [f"## {heading}", ""]
        if content:
            lines += [content, ""]
    return "\n".join(lines).strip() + "\n"


# ── 论文分章多轮生成（设计文档：doc/paper-multipass-generation-plan.md） ──────
#
# harness 式编排：总编规划 → 逐章写作 → 统稿收口。控制流在代码、判断力在模型；
# 每次调用共用同一套「解析 → 校验 → 一次修复」纪律，失败可归因到章节号。

PAPER_OUTLINE_PROMPT = "paper_outline.default"
PAPER_SECTION_PROMPT = "paper_section.default"
PAPER_FINALIZE_PROMPT = "paper_finalize.default"

#: 章节数带宽：低于下限说明骨架退化（回退单次调用），高于上限说明规划失控。
_CHAPTER_COUNT_MIN = 3
_CHAPTER_COUNT_MAX = 12
#: 进度事件里章节正文的上限。事件同时是断点续写的检查点（engine_glue 从事件
#: 日志重建已完成章节），必须容纳完整正文；超限章节标记 truncated，续写时重做。
_SECTION_EVENT_MAX_CHARS = 20_000
#: 滚动摘要：单章摘要与拼接总量的字符上限（控 token，同时保住跨章承接）。
_DIGEST_CHARS = 150
_DIGESTS_TOTAL_CHARS = 1200
#: 章节字数带宽（软校验）：偏离目标 ±30% 触发一次有界重写，仍越界则记警告。
_SECTION_LENGTH_TOLERANCE = 0.3
#: 全文的字数重写总额度（控成本：不给多话的模型每章都加一次调用）。
_MAX_LENGTH_REVISIONS = 2
#: source_keys 的合法取值与材料标题（总编给每章指定材料，缺失时给全量）。
_MATERIAL_LABELS = {
    "problem_analysis": "问题分析结果（JSON）",
    "chosen_plan": "已确认的建模方案（JSON）",
    "experiment_summary": "实验过程摘要",
    "validation_summary": "检验结论",
}

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def _emit_progress(services: NodeServices, payload: dict[str, Any]) -> None:
    """节点内进度事件（run.log 旁路观测通道）：装配缺失或回调抛错绝不影响执行。"""
    callback = (services.extras or {}).get("progress")
    if not callable(callback):
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001 - 过程展示绝不允许拖垮任务本身
        pass


def _unsourced_numbers(text: str, sources: str) -> list[str]:
    """正文里在材料中找不到的数值（防编造的软校验）。

    一位数不计（章节号/序号会大量误报），同一数值只报一次，最多取样 8 个。
    """
    missing: list[str] = []
    seen: set[str] = set()
    for token in _NUMBER_PATTERN.findall(text):
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        if token not in sources:
            missing.append(token)
        if len(missing) >= 8:
            break
    return missing


def _inputs_hash(variables: Mapping[str, str]) -> str:
    """四份输入材料的指纹：断点续写只在输入未变时生效（变了就整篇重来）。"""
    canonical = json.dumps(
        {key: variables[key] for key in sorted(variables)}, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PaperWritingNode(LlmSkillNode):
    """论文撰写：分章多轮生成，总编失败自动回退单次调用（不比旧链路差）。"""

    prompt_id = "paper_writing.default"
    state = TaskState.PAPER_WRITING

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        experiment = ctx.prior_outputs.get(TaskState.EXPERIMENTING.value) or {}
        validation = ctx.prior_outputs.get(TaskState.VALIDATING.value) or {}
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(chosen_plan(planning), ensure_ascii=False),
            "experiment_summary": str(experiment.get("experiment_summary") or "无"),
            "validation_summary": str(validation.get("validation_summary") or "无"),
        }

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        if services.llm is None:
            return NodeResult.failed("no LLM port configured for this run")
        if services.artifacts is None:
            # 提前失败：不能等 10 次调用烧完才发现草稿发布不出去。
            return NodeResult.failed("artifact 存储端口未装配，无法发布论文草稿")
        try:
            variables = self.build_variables(ctx)
        except KeyError as exc:
            return NodeResult.failed(f"missing required input: {exc}")

        outline_template = self._registry.get(PAPER_OUTLINE_PROMPT)
        input_problems = validate(variables, outline_template.input_schema)
        if input_problems:
            return NodeResult.failed(
                "prompt input invalid: " + "; ".join(input_problems)
            )

        attempts_total = 0
        inputs_hash = _inputs_hash(variables)

        # ── ① 总编规划：骨架 + 符号表 + 每章写作指令。重试且输入未变时从
        #    事件检查点恢复（跳过总编调用与已完成章节），输入变了整篇重来。 ──
        resume = self._load_resume(services, inputs_hash)
        if resume is not None:
            outline = resume["outline"]
        else:
            outline, attempts, error = complete_validated(services, outline_template, variables)
            attempts_total += attempts
            if outline is not None and (structural := self._outline_problems(outline)):
                outline, error = None, structural
            if outline is None:
                return self._run_single_call(ctx, services, variables, attempts_total, str(error))

        chapters: list[Mapping[str, Any]] = outline["chapters"]
        total = len(chapters)
        title = str(outline.get("title") or "建模论文草稿").strip()
        notation = str(outline.get("notation") or "")
        if resume is None:
            # 骨架事件同时是断点续写的检查点：携带输入指纹与完整骨架
            _emit_progress(services, {
                "kind": "paper_outline",
                "total": total,
                "headings": [str(chapter.get("heading") or "") for chapter in chapters],
                "inputs_hash": inputs_hash,
                "outline": dict(outline),
            })

        # ── ② 逐章写作：滚动摘要承接前文，符号表注入保证全文记号一致 ────────
        section_template = self._registry.get(PAPER_SECTION_PROMPT)
        sections: list[dict[str, str]] = []
        digests: list[str] = []
        warnings: list[str] = []
        revisions_used = 0
        if resume is not None:
            for done_index, entry in enumerate(resume["completed"], start=1):
                sections.append({"heading": entry["heading"], "content": entry["content"]})
                digests.append(
                    f"第{done_index}章《{entry['heading']}》：{entry['digest'] or '（无摘要）'}"
                )
        for index in range(len(sections) + 1, total + 1):
            chapter = chapters[index - 1]
            heading = str(chapter.get("heading") or f"第 {index} 章").strip()
            raw_target = chapter.get("target_chars")
            target = raw_target if isinstance(raw_target, int) and raw_target > 0 else 1200
            materials = self._materials(variables, chapter.get("source_keys"))
            section_vars = {
                "title": title,
                "notation": notation,
                "chapter_heading": heading,
                "chapter_brief": str(chapter.get("brief") or ""),
                "target_chars": str(target),
                "materials": materials,
                "previous_digests": self._joined_digests(digests),
            }
            section, attempts, error = complete_validated(services, section_template, section_vars)
            attempts_total += attempts
            if section is None:
                return NodeResult.failed(
                    f"第 {index}/{total} 章「{heading}」生成失败：{error}"
                )
            content = str(section.get("content") or "").strip()
            # 字数带宽越界 → 一次有界重写（全文共 _MAX_LENGTH_REVISIONS 次额度）：
            # 带上偏差反馈重写本章，只有更接近目标才采纳，仍越界则如实记警告。
            lower = int(target * (1 - _SECTION_LENGTH_TOLERANCE))
            upper = int(target * (1 + _SECTION_LENGTH_TOLERANCE))
            if content and not lower <= len(content) <= upper and revisions_used < _MAX_LENGTH_REVISIONS:
                revisions_used += 1
                revised, extra_attempts, _revise_error = complete_validated(
                    services,
                    section_template,
                    {
                        **section_vars,
                        "__repair_error": (
                            f"content 字数 {len(content)} 超出目标带宽 {lower}-{upper} 字"
                            f"（目标 {target}±30%）。请把本章正文调整到带宽内：保持小节结构、"
                            "公式与全部数值不变，只输出同格式 JSON。"
                        ),
                        "__previous_output": content[:2000],
                    },
                )
                attempts_total += extra_attempts
                if revised is not None:
                    candidate = str(revised.get("content") or "").strip()
                    if candidate and abs(len(candidate) - target) < abs(len(content) - target):
                        section = revised
                        content = candidate
            digest = str(section.get("digest") or "").strip()[:_DIGEST_CHARS]
            sections.append({"heading": heading, "content": content})
            digests.append(f"第{index}章《{heading}》：{digest or '（无摘要）'}")
            warnings.extend(self._section_warnings(index, heading, content, target, materials))
            truncated = len(content) > _SECTION_EVENT_MAX_CHARS
            _emit_progress(services, {
                "kind": "paper_section",
                "index": index,
                "total": total,
                "heading": heading,
                "chars": len(content),
                "content": content[:_SECTION_EVENT_MAX_CHARS],
                "digest": digest,
                "truncated": truncated,
            })

        # ── ③ 统稿收口：终版摘要与关键词；失败不弃全文，机械拼接并如实记警告 ──
        experiment = ctx.prior_outputs.get(TaskState.EXPERIMENTING.value) or {}
        metrics_json = json.dumps(dict(experiment.get("metrics") or {}), ensure_ascii=False)
        finalize_vars = {
            "title": title,
            "digests": "\n".join(digests),
            "metrics": metrics_json,
            "validation_summary": variables["validation_summary"],
        }
        finalize, attempts, error = complete_validated(
            services, self._registry.get(PAPER_FINALIZE_PROMPT), finalize_vars
        )
        attempts_total += attempts
        if finalize is None:
            warnings.append(f"统稿调用失败（{error}），摘要由各章摘要拼接")
            finalize = {
                "abstract": "；".join(item.split("：", 1)[-1] for item in digests)[:480],
                "keywords": outline.get("keywords") or [],
            }

        abstract = str(finalize.get("abstract") or "").strip()
        unsourced = _unsourced_numbers(abstract, metrics_json + "\n".join(digests))
        if unsourced:
            warnings.append(
                f"摘要有 {len(unsourced)} 个数值未在指标与各章摘要中找到出处（如 {'、'.join(unsourced[:3])}）"
            )

        keywords = [
            str(keyword).strip()
            for keyword in (finalize.get("keywords") or outline.get("keywords") or [])
            if str(keyword).strip()
        ]
        outputs: dict[str, Any] = {
            "title": title,
            "abstract": abstract,
            "keywords": keywords,
            "sections": sections,
            "progress_note": str(
                finalize.get("progress_note")
                or f"论文已按 {total} 章完成撰写，可在论文页查看、编辑与导出。"
            ),
        }
        metrics_payload: dict[str, Any] = {"llm_attempts": attempts_total, "chapters": total}
        if resume is not None:
            metrics_payload["resumed_chapters"] = len(resume["completed"])
        if revisions_used:
            metrics_payload["length_revisions"] = revisions_used
        if warnings:
            metrics_payload["quality_warnings"] = warnings
        return self._publish(ctx, services, outputs, metrics_payload)

    # -- helpers -------------------------------------------------------------

    def _load_resume(
        self, services: NodeServices, inputs_hash: str
    ) -> dict[str, Any] | None:
        """事件检查点 → 可复用的（骨架, 已完成章节前缀）。

        读取器由执行侧经 extras["paper_resume"] 注入（数据源是 run.log 事件）；
        任何一环对不上（输入指纹、骨架结构、章节前缀连续性、标题匹配、正文被
        截断）都放弃续写、整篇重来——宁可多花调用也不接受不一致的半成品。
        """
        reader = (services.extras or {}).get("paper_resume")
        if not callable(reader):
            return None
        try:
            data = reader()
        except Exception:  # noqa: BLE001 - 检查点读取失败等价于没有检查点
            return None
        if not isinstance(data, dict) or data.get("inputs_hash") != inputs_hash:
            return None
        outline = data.get("outline")
        if not isinstance(outline, dict) or self._outline_problems(outline) is not None:
            return None
        chapters = outline["chapters"]
        completed: list[dict[str, str]] = []
        expected = 1
        for entry in data.get("sections") or []:
            if not isinstance(entry, Mapping) or entry.get("index") != expected:
                break
            if entry.get("truncated"):
                break
            if expected > len(chapters):
                break
            heading = str(entry.get("heading") or "")
            content = str(entry.get("content") or "")
            if not content or heading != str(chapters[expected - 1].get("heading") or ""):
                break
            completed.append({
                "heading": heading,
                "content": content,
                "digest": str(entry.get("digest") or "").strip()[:_DIGEST_CHARS],
            })
            expected += 1
        return {"outline": outline, "completed": completed}

    @staticmethod
    def _outline_problems(outline: Mapping[str, Any]) -> str | None:
        """骨架准入校验：章数在带宽内且每章有标题与写作指令，否则触发回退。"""
        chapters = outline.get("chapters")
        if not isinstance(chapters, list):
            return "章节规划缺失"
        if not _CHAPTER_COUNT_MIN <= len(chapters) <= _CHAPTER_COUNT_MAX:
            return f"章节数 {len(chapters)} 超出带宽 [{_CHAPTER_COUNT_MIN}, {_CHAPTER_COUNT_MAX}]"
        for index, chapter in enumerate(chapters, start=1):
            if not isinstance(chapter, Mapping):
                return f"第 {index} 章规划不是对象"
            if not str(chapter.get("heading") or "").strip():
                return f"第 {index} 章缺少标题"
            if not str(chapter.get("brief") or "").strip():
                return f"第 {index} 章缺少写作指令"
        return None

    @staticmethod
    def _materials(variables: Mapping[str, str], source_keys: Any) -> str:
        """按总编指定的 source_keys 组装本章材料；无有效指定时给全量。"""
        keys = [
            key for key in (source_keys if isinstance(source_keys, list) else [])
            if key in _MATERIAL_LABELS
        ]
        if not keys:
            keys = list(_MATERIAL_LABELS)
        return "\n\n".join(
            f"### {_MATERIAL_LABELS[key]}\n{variables[key]}" for key in keys
        )

    @staticmethod
    def _joined_digests(digests: list[str]) -> str:
        if not digests:
            return "无（本章是全文第一章）"
        joined = "\n".join(digests)
        if len(joined) > _DIGESTS_TOTAL_CHARS:
            joined = "……" + joined[-_DIGESTS_TOTAL_CHARS:]
        return joined

    @staticmethod
    def _section_warnings(
        index: int, heading: str, content: str, target: int, materials: str
    ) -> list[str]:
        """章级软校验：只记警告不阻断（执行事实如实上报，人来裁量）。"""
        warnings: list[str] = []
        lower = int(target * (1 - _SECTION_LENGTH_TOLERANCE))
        upper = int(target * (1 + _SECTION_LENGTH_TOLERANCE))
        if content and not lower <= len(content) <= upper:
            warnings.append(
                f"第{index}章《{heading}》字数 {len(content)} 偏离目标 {target}（±30%）"
            )
        unsourced = _unsourced_numbers(content, materials)
        if unsourced:
            warnings.append(
                f"第{index}章《{heading}》有 {len(unsourced)} 个数值未在材料中找到出处（如 {'、'.join(unsourced[:3])}）"
            )
        return warnings

    def _run_single_call(
        self,
        ctx: NodeContext,
        services: NodeServices,
        variables: dict[str, Any],
        attempts_before: int,
        fallback_reason: str,
    ) -> NodeResult:
        """回退路径：总编规划失败时整篇单次生成（paper_writing.default v4）。"""
        template = self._registry.get(self.prompt_id)
        parsed, attempts, error = complete_validated(services, template, variables)
        if parsed is None:
            return NodeResult.failed(
                f"论文骨架规划失败（{fallback_reason}），回退整篇生成同样失败："
                f"model output failed validation after {attempts} attempts: {error}"
            )
        metrics_payload: dict[str, Any] = {
            "llm_attempts": attempts_before + attempts,
            "fallback": "single_call",
            "fallback_reason": fallback_reason,
        }
        return self._publish(ctx, services, parsed, metrics_payload)

    def _publish(
        self,
        ctx: NodeContext,
        services: NodeServices,
        outputs: dict[str, Any],
        metrics: dict[str, Any],
    ) -> NodeResult:
        markdown = render_paper_markdown(outputs)
        ref = services.artifacts.put(
            ctx.run_id,
            "paper",
            "paper-draft.md",
            markdown.encode("utf-8"),
            "text/markdown",
            ctx.step_id,
        )
        return NodeResult.succeeded(outputs=outputs, metrics=metrics, artifacts=(ref,))
