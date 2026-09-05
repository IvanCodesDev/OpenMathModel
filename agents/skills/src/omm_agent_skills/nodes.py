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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from omm_agent_core import NodeContext, NodeResult, NodeServices, TaskState
from omm_agent_core.errors import AgentError
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
from .frozen_numbers import (
    AUDIT_SAMPLE_LIMIT,
    allowed_number_tokens,
    audit_document,
    build_frozen_numbers,
    number_tokens,
    render_frozen_numbers,
    unsourced_numbers,
)
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


# ── 方案阶段（H3）：三视角 Proposer 并行提议 → 归约 → G1 三选 ──────────────


@dataclass(frozen=True)
class ProposerView:
    """一路提议视角（§8.4：多样性由 SpawnSpec 固定视角保证，不靠温度）。

    ``id`` 进 SpawnSpec.kind（``proposer:<id>``）与审计事件；``name`` / ``brief``
    进提议人 prompt。
    """

    id: str
    name: str
    brief: str


#: §8.2 角色目录：Proposer ×3（机理 / 数据驱动 / 运筹）。
PROPOSER_VIEWS: tuple[ProposerView, ...] = (
    ProposerView(
        "mechanism",
        "机理建模",
        "从问题的内在机理出发：守恒关系、动力学、微分 / 差分方程、排队、博弈、"
        "元胞自动机等解释性模型；参数要有物理或经济含义，结论可被机理解释；"
        "数据用于标定参数与检验，而不是模型本身。",
    ),
    ProposerView(
        "data_driven",
        "数据驱动",
        "从数据出发：统计回归、时间序列、聚类 / 分类、树模型与神经网络等以预测"
        "精度与泛化为目标的方法；强调特征工程、交叉验证、基线对比与不确定性"
        "量化；数据量不支持时如实降级到简单统计模型。",
    ),
    ProposerView(
        "operations_research",
        "运筹优化",
        "把问题写成决策变量 + 目标函数 + 约束：线性 / 整数 / 非线性规划、多目标、"
        "动态规划、图论与网络流，以及规模过大时的启发式与元启发式；强调可解性、"
        "最优性证明或界，以及对参数的敏感性。",
    ),
)

PROPOSER_PROMPT_ID = "model_planning.proposer"
REDUCE_PROMPT_ID = "model_planning.reduce"
#: 归约后的规范化调用：假设表 + 符号表（§9.1「归约 → 假设表+符号表 → 决策卡」）。
FORMALIZE_PROMPT_ID = "model_planning.formalize"
#: 提议人 Envelope 的 output_schema_id（§8.3 协议字段；校验器在本模块）。
PROPOSAL_SCHEMA_ID = "model-planning-proposal.v1"

#: 假设表 / 符号表的枚举与上限（与 plan-proposal 契约 $defs 对齐；这里只按契约
#: 口径归一化，不 import contracts —— agents 域不依赖 contracts 包）。
GLOBAL_ASSUMPTION_SCOPE = "global"
ASSUMPTION_IMPACTS = ("low", "medium", "high")
ASSUMPTION_STATUSES = ("confirmed", "to_verify", "critical")
SYMBOL_KINDS = ("set", "parameter", "variable", "objective", "other")
MAX_ASSUMPTIONS = 12
MAX_SYMBOLS = 24

_IMPACT_ALIASES = {
    "low": "low", "低": "low", "minor": "low",
    "medium": "medium", "mid": "medium", "moderate": "medium", "中": "medium",
    "high": "high", "高": "high", "major": "high", "critical": "high",
}
_STATUS_ALIASES = {
    "confirmed": "confirmed", "已确认": "confirmed", "given": "confirmed",
    "supported": "confirmed",
    "to_verify": "to_verify", "to-verify": "to_verify", "verify": "to_verify",
    "待检验": "to_verify", "pending": "to_verify",
    "critical": "critical", "重点验证": "critical", "sensitivity": "critical",
}
_KIND_ALIASES = {
    "set": "set", "index": "set", "集合": "set", "索引": "set",
    "parameter": "parameter", "param": "parameter", "constant": "parameter",
    "input": "parameter", "参数": "parameter", "常数": "parameter",
    "variable": "variable", "decision": "variable", "decision_variable": "variable",
    "state": "variable", "变量": "variable", "决策变量": "variable",
    "objective": "objective", "目标": "objective", "目标函数": "objective",
    "other": "other", "function": "other", "其他": "other", "其它": "other",
}
#: 模型偶尔把 $…$ / \(…\) 定界一起给回来；契约要求不带定界、前端自己包。
_MATH_DELIMS = re.compile(r"^\s*(?:\$\$?|\\\(|\\\[)\s*|\s*(?:\$\$?|\\\)|\\\])\s*$")

#: quorum（§8.4）：≥2 路成功走归约；只剩 1 路降级为单案（记警告、卡片点明）；
#: 0 路成功节点失败（引擎重试一次）。
PROPOSER_QUORUM = 2

#: G1 选项 id。approve 保留（= 采用推荐案；既有 e2e / 金轨迹 / 前端 CTA 都用它），
#: 其它候选用 adopt:<plan_id>；reject 由控制面特判为「退回重做方案」。
G1_APPROVE_OPTION_ID = "approve"
G1_REJECT_OPTION_ID = "reject"
ADOPT_OPTION_PREFIX = "adopt:"

PLAN_ROLE_LABELS = {
    "primary": "主候选",
    "baseline": "可用基线",
    "fallback": "条件回退",
    "candidate": "候选",
}

_PROPOSAL_KEYS = ("view", "view_name", "name", "approach", "steps", "risks", "fit")


def _clean_strs(value: Any) -> list[str]:
    """列表项转字符串并去掉空白项（模型偶尔回空串 / 数字）。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _proposal_problems(output: Mapping[str, Any]) -> list[str]:
    """提议人 Envelope 的形状校验（Supervisor 的 output_validator，E520）。"""
    problems: list[str] = []
    for key in ("name", "approach", "fit"):
        if not str(output.get(key) or "").strip():
            problems.append(f"{key} 为空")
    steps = output.get("steps")
    if not isinstance(steps, list) or not any(str(step).strip() for step in steps):
        problems.append("steps 为空")
    if not isinstance(output.get("risks"), list):
        problems.append("risks 不是列表")
    return problems


def _plan_set_problems(reduced: Mapping[str, Any]) -> list[str]:
    """方案卡集合的不变量：id 唯一、推荐项存在、每张卡五键齐且非空。"""
    plans = reduced.get("plans")
    if not isinstance(plans, list) or not plans:
        return ["plans 为空"]
    problems: list[str] = []
    ids: list[str] = []
    for index, plan in enumerate(plans):
        if not isinstance(plan, Mapping):
            problems.append(f"plans[{index}] 不是对象")
            continue
        plan_id = str(plan.get("id") or "").strip()
        if not plan_id:
            problems.append(f"plans[{index}].id 为空")
        elif plan_id in ids:
            problems.append(f"方案 id {plan_id!r} 重复")
        ids.append(plan_id)
        for key in ("name", "approach"):
            if not str(plan.get(key) or "").strip():
                problems.append(f"plans[{index}].{key} 为空")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not any(str(step).strip() for step in steps):
            problems.append(f"plans[{index}].steps 为空")
        if not isinstance(plan.get("risks"), list):
            problems.append(f"plans[{index}].risks 不是列表")
    if reduced.get("recommended_plan_id") not in ids:
        problems.append("recommended_plan_id does not reference a returned plan")
    return problems


def _plan_blurb(plan: Mapping[str, Any]) -> str:
    """G1 选项 description：角色 + 思路首句（卡片版面有限）。"""
    role = PLAN_ROLE_LABELS.get(str(plan.get("role") or ""), PLAN_ROLE_LABELS["candidate"])
    approach = str(plan.get("approach") or "").strip()
    lead = re.split(r"(?<=[。；;.!?！？])", approach, maxsplit=1)[0].strip() or approach
    if len(lead) > 80:
        lead = lead[:79] + "…"
    condition = str(plan.get("fallback_condition") or "").strip()
    if condition:
        return f"{role}：{lead}（触发条件：{condition}）"
    return f"{role}：{lead}"


def _enum_or(value: Any, aliases: Mapping[str, str], default: str) -> str:
    """枚举归一化：大小写 / 中英文别名收敛到契约取值，认不出的给默认值。"""
    key = str(value or "").strip().lower().replace(" ", "_")
    return aliases.get(key, default)


def _optional_text(value: Any) -> str | None:
    """可选文字字段：空白 / None / 字面 null 一律 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "无", "—", "-"}:
        return None
    return text


def _match_plan_id(value: Any, plan_ids: Sequence[str]) -> str | None:
    """把模型写的方案标识对回方案 id（容忍大小写与「方案 A」这类写法）；对不上 → None。"""
    text = _optional_text(value)
    if text is None:
        return None
    candidate = re.sub(r"^(?:方案|plan)\s*", "", text, flags=re.IGNORECASE).strip()
    for plan_id in plan_ids:
        if candidate.lower() == plan_id.lower():
            return plan_id
    return None


def normalize_assumptions(raw: Any, plan_ids: Sequence[str]) -> list[dict[str, Any]]:
    """规范化调用输出的假设表 → 契约 assumption[]（确定性）。

    - scope 不是 "global" 也不是任一方案 id → 归为全局（内容保留，不因方案 id
      写错丢掉一条假设）；
    - id 一律重编号：全局 G1…、方案 X1…（模型给的 id 常重复 / 混用，重编号
      比校验再修复省一次调用）；
    - 顺序：全局在前，其后按 plan_ids 顺序分组；同组内保持模型给出的顺序；
    - 空 text 剔除；超过 MAX_ASSUMPTIONS 截断（先保全局，再按方案顺序）。
    """
    if not isinstance(raw, list):
        return []
    known = [str(plan_id) for plan_id in plan_ids]
    buckets: dict[str, list[dict[str, Any]]] = {GLOBAL_ASSUMPTION_SCOPE: []}
    for plan_id in known:
        buckets.setdefault(plan_id, [])
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        scope = _match_plan_id(item.get("scope"), known) or GLOBAL_ASSUMPTION_SCOPE
        buckets[scope].append({
            "text": text,
            "scope": scope,
            "basis": str(item.get("basis") or "").strip(),
            "impact": _enum_or(item.get("impact"), _IMPACT_ALIASES, "medium"),
            "status": _enum_or(item.get("status"), _STATUS_ALIASES, "to_verify"),
        })
    ordered: list[dict[str, Any]] = []
    for scope in [GLOBAL_ASSUMPTION_SCOPE, *known]:
        prefix = "G" if scope == GLOBAL_ASSUMPTION_SCOPE else scope
        for index, entry in enumerate(buckets[scope], start=1):
            ordered.append({"id": f"{prefix}{index}", **entry})
    return ordered[:MAX_ASSUMPTIONS]


def normalize_symbols(raw: Any, plan_ids: Sequence[str]) -> list[dict[str, Any]]:
    """规范化调用输出的符号表 → 契约 symbol[]（确定性）。

    - symbol 去掉模型多给的 $ / \\( \\) 定界（契约要求不带，前端统一包）；
    - kind 别名收敛，认不出 → other；plan_id 不是任一方案 → null（共享）；
    - 顺序：共享在前，其后按 plan_ids 顺序分组；空 symbol / definition 剔除；
      超过 MAX_SYMBOLS 截断。
    """
    if not isinstance(raw, list):
        return []
    known = [str(plan_id) for plan_id in plan_ids]
    buckets: dict[str | None, list[dict[str, Any]]] = {None: []}
    for plan_id in known:
        buckets.setdefault(plan_id, [])
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        symbol = _MATH_DELIMS.sub("", str(item.get("symbol") or "")).strip()
        definition = str(item.get("definition") or "").strip()
        if not symbol or not definition:
            continue
        plan_id = _match_plan_id(item.get("plan_id"), known)
        buckets[plan_id].append({
            "symbol": symbol,
            "kind": _enum_or(item.get("kind"), _KIND_ALIASES, "other"),
            "definition": definition,
            "unit": _optional_text(item.get("unit")),
            "range": _optional_text(item.get("range")),
            "plan_id": plan_id,
        })
    ordered: list[dict[str, Any]] = []
    for scope in [None, *known]:
        ordered.extend(buckets[scope])
    return ordered[:MAX_SYMBOLS]


class ModelPlanningNode(LlmSkillNode):
    """方案阶段：三视角 Proposer 并行提议 → 归约 → G1 必停（三选）。

    - 有 ``subagents`` 监督者时：每个视角一个 ``proposer:<view>`` 子代理（readonly、
      预算切片、spawn / 结果双审计），线程并行 fan-out；≥2 路成功走一次归约调用
      （去重、定主候选 / 基线 / 条件回退）；只剩 1 路降级为单案并记警告；0 路失败。
    - 无监督者（旧装配 / 单节点测试）或 ``proposer_views`` 为空：走 v3.21 的单次
      调用路径，行为与载荷逐字节不变（fan-out 是配置不是架构，§17）。
    """

    prompt_id = "model_planning.default"
    state = TaskState.MODEL_PLANNING

    def __init__(
        self,
        registry: PromptRegistry,
        require_confirmation: bool = True,
        proposer_views: Sequence[ProposerView] = PROPOSER_VIEWS,
    ) -> None:
        super().__init__(registry)
        # Plan confirmation is the product's human gate (roadmap: 方案 A/B 生成、
        # 用户确认). Evals/automation may disable it explicitly.
        self._require_confirmation = require_confirmation
        self._views = tuple(proposer_views)

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

    # -- fan-out path ------------------------------------------------------------

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        if services.llm is None:
            return NodeResult.failed("no LLM port configured for this run")
        supervisor = (services.extras or {}).get("subagents")
        if not self._views or supervisor is None:
            return super().run(ctx, services)
        try:
            variables = self.build_variables(ctx)
        except KeyError as exc:
            return NodeResult.failed(f"missing required input: {exc}")

        proposals, failures, llm_calls = self._propose(ctx, services, supervisor, variables)
        if not proposals:
            return NodeResult.failed(
                "全部视角的方案提议都未成功：" + "；".join(failures),
                metrics={"llm_attempts": llm_calls},
            )

        warnings = list(failures)
        reduced: dict[str, Any] | None = None
        if len(proposals) >= PROPOSER_QUORUM:
            reduced, attempts, error = self._reduce(services, variables, proposals, failures)
            llm_calls += attempts
            if reduced is None:
                warnings.append(f"方案归约未成功（{error}），按视角顺序直接列为候选")
        else:
            warnings.append(
                f"仅「{proposals[0]['view_name']}」一路视角成功，未做归约：只有一套候选方案"
            )
        if reduced is None:
            reduced = self._direct_plans(proposals)
        problems = _plan_set_problems(reduced)
        if problems:
            return NodeResult.failed(
                "方案归约结果不合法：" + "；".join(problems),
                metrics={"llm_attempts": llm_calls},
            )

        # 归约 → 假设表 + 符号表 → 决策卡（§9.1）。两表是方案页与论文的材料，
        # 不是闸门依据：生成失败只记警告、字段留 null，G1 照常挂出。
        assumptions, symbols, attempts, error = self._formalize(services, variables, reduced)
        llm_calls += attempts
        if error:
            warnings.append(f"模型假设表与符号表未生成（{error}）")

        outputs: dict[str, Any] = {
            "plans": [dict(plan) for plan in reduced["plans"]],
            "recommended_plan_id": reduced["recommended_plan_id"],
            "rationale": reduced.get("rationale"),
            "progress_note": reduced.get("progress_note"),
            "dropped": list(reduced.get("dropped") or []),
            "assumptions": assumptions,
            "symbols": symbols,
            # 三路提议的原样留档（去重前）：论文 / 复盘可回看被合并或舍弃的思路
            "proposals": [
                {key: proposal[key] for key in _PROPOSAL_KEYS} for proposal in proposals
            ],
            "proposer_failures": list(failures),
            "quality_warnings": warnings,
            "llm_attempts": llm_calls,
        }
        if not self._require_confirmation:
            return NodeResult.succeeded(outputs=outputs, metrics={"llm_attempts": llm_calls})
        reason, meta = self._g1_review(reduced, proposals, failures)
        return NodeResult.needs_review(reason=reason, outputs=outputs, review_meta=meta)

    def _propose(
        self,
        ctx: NodeContext,
        services: NodeServices,
        supervisor: Any,
        variables: Mapping[str, str],
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """三路视角并行提议（每路一个子代理），按视角顺序返回成功的提案。"""
        governor = (services.extras or {}).get("budget_governor")
        budgets: RunBudget = (
            governor.subagent_slice() if governor is not None else RunBudget()
        )
        template = self._registry.get(PROPOSER_PROMPT_ID)
        analysis = ctx.prior_outputs.get(TaskState.PROBLEM_ANALYSIS.value) or {}

        def spawn_one(
            view: ProposerView,
        ) -> tuple[ProposerView, ResultEnvelope | None, int, str, AgentError | None]:
            trace: dict[str, Any] = {"attempts": 0, "error": "", "agent_error": None}

            def runner(_spec: SpawnSpec) -> ResultEnvelope:
                try:
                    parsed, attempts, error = complete_validated(
                        services,
                        template,
                        {**variables, "view_name": view.name, "view_brief": view.brief},
                    )
                except AgentError as exc:
                    # 预算硬停（E31x/E32x）是运行级事实，不是这一路的软失败：
                    # 留住异常对象，fan-out 收束后按原样抛给节点外层（与单次调用
                    # 路径同一条 _BudgetGuardedNode 出口）；Supervisor 照常收割审计
                    trace["agent_error"] = exc
                    raise
                trace["attempts"] = attempts
                if parsed is None:
                    trace["error"] = str(error or "模型输出未通过校验")
                    return ResultEnvelope(status="failed")
                return ResultEnvelope(status="done", output=dict(parsed))

            try:
                envelope = supervisor.spawn(
                    SpawnSpec(
                        kind=f"proposer:{view.id}",
                        goal=f"从「{view.name}」视角提出一套可执行的建模方案",
                        context_slice={
                            "view": view.id,
                            "problem_analysis": analysis,
                            "data_profile": variables["data_profile"],
                        },
                        toolset=(),
                        tool_tier="readonly",
                        budgets=budgets,
                        output_schema_id=PROPOSAL_SCHEMA_ID,
                    ),
                    runner,
                    parent_tier="readonly",
                    output_validator=lambda output: _proposal_problems(output),
                )
            except AgentError as exc:
                # 装配 / 切片缺陷（E510 等）：这一路按未成功计，不炸整个节点
                return view, None, trace["attempts"], f"{exc.code.value}：{exc}", None
            return view, envelope, trace["attempts"], trace["error"], trace["agent_error"]

        with ThreadPoolExecutor(max_workers=max(1, len(self._views))) as pool:
            outcomes = list(pool.map(spawn_one, self._views))

        for _view, _envelope, _attempts, _error, agent_error in outcomes:
            if agent_error is not None and agent_error.code.value.startswith("E3"):
                raise agent_error

        proposals: list[dict[str, Any]] = []
        failures: list[str] = []
        llm_calls = 0
        for view, envelope, attempts, error, _agent_error in outcomes:
            llm_calls += attempts
            if envelope is not None and envelope.ok and envelope.output is not None:
                proposals.append({
                    "view": view.id,
                    "view_name": view.name,
                    "name": str(envelope.output.get("name") or "").strip(),
                    "approach": str(envelope.output.get("approach") or "").strip(),
                    "steps": _clean_strs(envelope.output.get("steps")),
                    "risks": _clean_strs(envelope.output.get("risks")),
                    "fit": str(envelope.output.get("fit") or "").strip(),
                })
                continue
            detail = envelope.status if envelope is not None else "spawn 被拒绝"
            if envelope is not None and envelope.error_code:
                detail += f" {envelope.error_code}"
            if error:
                detail += f"，{error}"
            failures.append(f"视角「{view.name}」未成功（{detail}）")
        return proposals, failures, llm_calls

    def _reduce(
        self,
        services: NodeServices,
        variables: Mapping[str, str],
        proposals: Sequence[Mapping[str, Any]],
        failures: Sequence[str],
    ) -> tuple[dict[str, Any] | None, int, str | None]:
        """一次归约调用：去重、定角色（主候选 / 基线 / 条件回退）、给推荐理由。"""
        template = self._registry.get(REDUCE_PROMPT_ID)
        payload: list[dict[str, Any]] = [dict(proposal) for proposal in proposals]
        for failure in failures:
            payload.append({"status": "failed", "note": failure})
        return complete_validated(
            services,
            template,
            {
                "proposals": json.dumps(payload, ensure_ascii=False),
                "problem_analysis": variables["problem_analysis"],
                "data_profile": variables["data_profile"],
            },
        )

    def _formalize(
        self,
        services: NodeServices,
        variables: Mapping[str, str],
        reduced: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None, int, str | None]:
        """一次规范化调用：为归约后的方案卡整理假设表与符号表。

        返回 (assumptions, symbols, llm_calls, error)。调用 / 校验失败 → 两表 None
        + error 文案（调用方记警告）；预算硬停（AgentError E3xx）原样上抛，与
        ``_reduce`` 同一口径。
        """
        template = self._registry.get(FORMALIZE_PROMPT_ID)
        plans = [dict(plan) for plan in reduced["plans"]]
        parsed, attempts, error = complete_validated(
            services,
            template,
            {
                "plans": json.dumps(plans, ensure_ascii=False),
                "problem_analysis": variables["problem_analysis"],
                "data_profile": variables["data_profile"],
            },
        )
        if parsed is None:
            return None, None, attempts, str(error or "模型输出未通过校验")
        plan_ids = [str(plan.get("id")) for plan in plans]
        return (
            normalize_assumptions(parsed.get("assumptions"), plan_ids),
            normalize_symbols(parsed.get("symbols"), plan_ids),
            attempts,
            None,
        )

    @staticmethod
    def _direct_plans(proposals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """归约不可用时的降级：按视角顺序直接列为候选，第一路为推荐。"""
        plans: list[dict[str, Any]] = []
        for index, proposal in enumerate(proposals[:3]):
            plans.append({
                "id": "ABC"[index],
                "name": proposal["name"],
                "approach": proposal["approach"],
                "steps": list(proposal["steps"]),
                "risks": list(proposal["risks"]),
                "role": "primary" if index == 0 else "candidate",
                "source_views": [proposal["view"]],
            })
        lead = proposals[0]
        rationale = f"按「{lead['view_name']}」视角的提议作为推荐方案：{lead['fit']}"
        if len(proposals) > 1:
            rationale += "；其余视角的提议未经归约、按顺序列为候选，请对照取舍"
        return {
            "plans": plans,
            "recommended_plan_id": "A",
            "rationale": rationale,
            "progress_note": None,
            "dropped": [],
        }

    @staticmethod
    def _g1_review(
        reduced: Mapping[str, Any],
        proposals: Sequence[Mapping[str, Any]],
        failures: Sequence[str],
    ) -> tuple[str, dict[str, Any]]:
        """G1 卡片元数据：推荐案 approve + 其余 adopt:<id> + reject。"""
        plans = [dict(plan) for plan in reduced["plans"]]
        recommended_id = str(reduced["recommended_plan_id"])
        recommended = next(plan for plan in plans if plan.get("id") == recommended_id)
        others = [plan for plan in plans if plan.get("id") != recommended_id]

        title = f"请确认建模方案：推荐 {recommended_id}「{recommended['name']}」"
        if others:
            title += "；备选 " + " / ".join(f"{plan['id']}「{plan['name']}」" for plan in others)
        if failures:
            title += f"；{len(failures)} 路视角提议未成功"

        options: list[dict[str, Any]] = [
            {
                "id": G1_APPROVE_OPTION_ID,
                "label": f"采用推荐方案 {recommended_id}（{recommended['name']}）",
                "description": _plan_blurb(recommended),
                "recommended": True,
            }
        ]
        for plan in others:
            options.append({
                "id": f"{ADOPT_OPTION_PREFIX}{plan['id']}",
                "label": f"改用方案 {plan['id']}（{plan['name']}）",
                "description": _plan_blurb(plan),
            })
        options.append({
            "id": G1_REJECT_OPTION_ID,
            "label": "退回重做方案",
            "description": "重新执行建模方案阶段（三路提议与归约重来一遍）并再次确认",
        })
        meta = {
            "gate": "G1",
            "decision_type": "confirm_plan",
            "title": title[:200],
            "options": options,
            "impact": {
                "plans": [
                    {
                        "id": plan.get("id"),
                        "name": plan.get("name"),
                        "role": plan.get("role"),
                        "source_views": list(plan.get("source_views") or []),
                    }
                    for plan in plans
                ],
                "proposers": {
                    "succeeded": [str(proposal["view"]) for proposal in proposals],
                    "failed": list(failures),
                },
                "dropped": list(reduced.get("dropped") or []),
            },
        }
        return title, meta


def _require_outputs(ctx: NodeContext, state: TaskState) -> Mapping[str, Any]:
    outputs = ctx.prior_outputs.get(state.value)
    if not outputs:
        raise KeyError(f"'{state.value} outputs'")
    return outputs


def chosen_plan(
    planning: Mapping[str, Any],
    review_decisions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The plan the run proceeds with.

    G1 的决策台账优先：用户选了 ``adopt:<id>`` 就用那一案；``approve``（采用推荐案）
    或没有台账（无人值守 / 旧运行）回到 recommended；都没有则取第一案。下游各阶段
    与审批卡呈现的是同一套选择规则。
    """
    plans = [plan for plan in planning.get("plans") or [] if isinstance(plan, Mapping)]
    decision = str((review_decisions or {}).get(TaskState.MODEL_PLANNING.value) or "")
    if decision.startswith(ADOPT_OPTION_PREFIX):
        wanted = decision[len(ADOPT_OPTION_PREFIX):]
        for plan in plans:
            if plan.get("id") == wanted:
                return dict(plan)
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

WS_WRITE_TOOL = "ws_write"

#: 实验节点收束后把通过验收的最终脚本落到工作区的固定路径。沙箱每次运行的
#: 即时副本在 steps/<step_id>/main.py（按步骤 id 命名，下游节点不知道那个 id），
#: 这里只放最终版：验证阶段据此复跑，论文阶段（figure_render，H5）也按此路径取。
EXPERIMENT_SCRIPT_PATH = "experiment.py"

#: 显式种子（§7.1 任务卡字段）：合成数据/抽样必须使用的固定种子。
SANDBOX_SEEDS = {"random_seed": 42}


class _SandboxCapture:
    """节点侧执行证据：最后一次 python_run 的 stdout/指标 + 全部产物 + 最终代码。

    sandbox-run-report.v1 不含 stdout 与指标本体（那是产物与断言的事），但
    节点输出（stage_outputs 正文）需要它们——在工具执行器上就地截获，不改
    执行体的报告形状。``code`` 由 publish 回调顺手记下：执行体只回传产物 id，
    节点要把脚本落到工作区固定路径还得有正文。
    """

    def __init__(self) -> None:
        self.stdout = ""
        self.metrics: dict[str, Any] = {}
        self.artifacts: list[Any] = []
        self.code = ""
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
        capture.code = code
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


def _stage_final_script(ctx: NodeContext, services: NodeServices, code: str) -> str:
    """把通过验收的最终脚本写到工作区固定路径，返回路径；写不进去如实给空串。

    只影响下游复跑（验证阶段找不到脚本会如实降级为「仅判读」），不影响本
    步骤的成败——脚本本身已作为 code 产物发布，可复现性不靠这一份副本。
    """
    if not code or services.tools is None:
        return ""
    result = services.tools.invoke(
        ctx.run_id,
        ctx.step_id,
        WS_WRITE_TOOL,
        {"path": EXPERIMENT_SCRIPT_PATH, "text": code},
    )
    return EXPERIMENT_SCRIPT_PATH if result.ok else ""


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
            "chosen_plan": json.dumps(
                chosen_plan(planning, ctx.review_decisions), ensure_ascii=False
            ),
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

        plan = chosen_plan(
            _require_outputs(ctx, TaskState.MODEL_PLANNING), ctx.review_decisions
        )
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
        # 最终脚本落工作区固定路径：验证阶段据此复跑（steps/<id>/main.py 的
        # 即时副本下游拿不到 id）。
        script_path = _stage_final_script(ctx, services, capture.code)

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
                # 工作区里的最终脚本路径（写入失败为空串，下游据此判断能否复跑）
                "script_path": script_path,
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


# ── 验证：LLM 判读 → 稳健性检查沙盒复跑 → G3 结果采用闸门 ─────────────────────

ROBUSTNESS_PROMPT_ID = "validating.sandbox"

#: G3 的「接受并记录局限」选项 id：进 review_decisions，论文阶段据此把未通过
#: 的检查项写进局限性。
G3_ACCEPT_OPTION_ID = "accept_with_limitations"

#: G3 审批选项（§11.1）。重做与回退两项直接复用修订门的 ``redo:<STATE>`` 选项
#: id：控制面 resolve_approval 据此回退并丢弃下游产出（ADR-0013 已落地的引擎
#: 语义），G3 不新增任何引擎分支。推荐项按未通过比例在运行时标注。
G3_OPTIONS = (
    {
        "id": G3_ACCEPT_OPTION_ID,
        "label": "接受并记录局限",
        "description": "采用当前实验结果继续撰写论文，未通过的检查项作为局限性如实写入论文",
    },
    {
        "id": "redo:EXPERIMENTING",
        "label": "重做实验",
        "description": "回到实验阶段重新实现与运行，随后重新检验",
    },
    {
        "id": "redo:MODEL_PLANNING",
        "label": "回退方案阶段",
        "description": "重新制定建模方案（需再次确认），之后重做实验与检验",
    },
)

#: 推荐项口径：未通过项占比不到一半 → 推荐「接受并记录局限」（结论主体成立，
#: 局限如实写进论文即可）；一半及以上 → 推荐「重做实验」。只是 CTA 预选，
#: 最终由人拍板。
G3_REDO_RECOMMEND_RATIO = 0.5

#: 检查项下限：只有一项不算稳健性检验——单项太容易挑一个必过的。
MIN_ROBUSTNESS_CHECKS = 2

#: 任务卡里实验脚本正文的上限（超长截断并标注；模型可 ws_read 全文）。
_EXPERIMENT_CODE_CARD_CHARS = 12_000


def _clip_code(code: str) -> str:
    if len(code) <= _EXPERIMENT_CODE_CARD_CHARS:
        return code
    return (
        code[:_EXPERIMENT_CODE_CARD_CHARS]
        + f"\n# …（脚本共 {len(code)} 字符，此处截断；完整内容请 ws_read {EXPERIMENT_SCRIPT_PATH}）"
    )


def _risk_points(plan: Mapping[str, Any], judgement: Mapping[str, Any]) -> str:
    """检验任务卡的风险点段：方案自报风险 + 评审判读的保留意见与风险。"""
    lines: list[str] = []
    for risk in plan.get("risks") or []:
        if str(risk).strip():
            lines.append(f"- 方案风险：{str(risk).strip()}")
    for check in judgement.get("checks") or []:
        if isinstance(check, Mapping) and check.get("result") in ("warn", "fail"):
            lines.append(
                f"- 评审保留（{check.get('result')}）：{check.get('name')}——{check.get('note')}"
            )
    for risk in judgement.get("risks") or []:
        if str(risk).strip():
            lines.append(f"- 评审风险：{str(risk).strip()}")
    return "\n".join(lines) or "- 无特别风险点：三类检查各做一项"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_checks(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """标记行里的 checks → 节点产出形状（只收合法项；校验在断言里做）。"""
    checks: list[dict[str, Any]] = []
    for entry in metrics.get("checks") or []:
        if not isinstance(entry, Mapping):
            continue
        check_id = str(entry.get("id") or "").strip()
        if not check_id or not isinstance(entry.get("passed"), bool):
            continue
        checks.append(
            {
                "id": check_id,
                "name": str(entry.get("name") or check_id),
                "passed": bool(entry["passed"]),
                "value": entry.get("value"),
                "threshold": entry.get("threshold"),
                "detail": str(entry.get("detail") or ""),
            }
        )
    return checks


def _robustness_checks_check(evidence) -> tuple[bool, str]:
    """断言：标记行含 ≥2 项结构完整的检查（id/name/passed/value/threshold）。"""
    raw = evidence.metrics.get("checks") if evidence.metrics else None
    if not isinstance(raw, list) or not raw:
        return False, (
            "未捕获检验结果：脚本必须原样打印一行 "
            'OMM_METRICS_JSON: {"checks": [{"id": ..., "name": ..., "passed": true/false, '
            '"value": 数值, "threshold": 数值, "detail": ...}, ...]}（独占一行）'
        )
    problems: list[str] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"第 {index} 项不是对象")
            continue
        if not str(entry.get("id") or "").strip():
            problems.append(f"第 {index} 项缺 id")
        if not str(entry.get("name") or "").strip():
            problems.append(f"第 {index} 项缺 name")
        if not isinstance(entry.get("passed"), bool):
            problems.append(f"第 {index} 项 passed 不是布尔值")
        if not _is_number(entry.get("value")):
            problems.append(f"第 {index} 项 value 不是数值")
        if not (_is_number(entry.get("threshold")) or str(entry.get("threshold") or "").strip()):
            problems.append(f"第 {index} 项缺 threshold")
    if len(raw) < MIN_ROBUSTNESS_CHECKS:
        problems.append(f"检查项只有 {len(raw)} 项，至少 {MIN_ROBUSTNESS_CHECKS} 项")
    if problems:
        return False, "检验结果不合格：" + "；".join(problems)
    passed = sum(1 for entry in raw if entry.get("passed") is True)
    return True, f"检查 {len(raw)} 项，通过 {passed} 项"


def _robustness_summary_text(status: str, checks: Sequence[Mapping[str, Any]]) -> str:
    """供论文引用的一句话稳健性结论：数字只来自检验脚本的标记行。"""
    if status != "passed":
        return f"稳健性检查沙盒复跑未完成（{status}），检验结论仅来自评审判读。"
    failed = [check for check in checks if not check.get("passed")]
    text = f"沙盒复跑稳健性检查 {len(checks)} 项，通过 {len(checks) - len(failed)} 项"
    if not failed:
        return text + "，全部达标。"
    detail = "；".join(
        f"{check.get('name')}（{check.get('id')}：value {check.get('value')}，阈值 {check.get('threshold')}）"
        for check in failed
    )
    return text + f"；未通过：{detail}。"


class ValidationNode(LlmSkillNode):
    """验证 = LLM 判读（单轮）→ 稳健性检查沙盒复跑（子代理）→ G3 条件闸门。

    沙盒复跑是尽力而为的增强（与数据阶段清洗同一纪律）：监督者 / 会话出口 /
    工作区实验脚本任一缺席都**如实降级**为「仅判读」（robustness.executed=false
    + 原因），绝不假装跑过。G3 只在检查真实执行、脚本跑通且至少一项未通过时
    触发（§9.1）：判定数字来自检验脚本的标记行，节点只做计数与比例。
    """

    prompt_id = "validating.default"
    sandbox_prompt_id = ROBUSTNESS_PROMPT_ID
    state = TaskState.VALIDATING

    #: R2 运行预算：检验脚本比实验脚本简单，4 次足够（§4.7 loop 行的节点内额度）。
    max_sandbox_runs = 4

    def __init__(
        self,
        registry: PromptRegistry,
        available_packages: str = DEFAULT_AVAILABLE_PACKAGES,
    ) -> None:
        super().__init__(registry)
        self._available_packages = available_packages

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        experiment = _require_outputs(ctx, TaskState.EXPERIMENTING)
        return {
            "chosen_plan": json.dumps(
                chosen_plan(planning, ctx.review_decisions), ensure_ascii=False
            ),
            "experiment_summary": str(
                experiment.get("experiment_summary")
                or experiment.get("stdout_tail")
                or "无"
            ),
            "metrics": json.dumps(dict(experiment.get("metrics") or {}), ensure_ascii=False),
        }

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        base = super().run(ctx, services)
        if base.status != NodeResult.SUCCEEDED:
            return base

        capture = _SandboxCapture()
        robustness = self._execute_checks(ctx, services, base.outputs, capture)
        outputs = {**base.outputs, "robustness": robustness}
        artifacts = base.artifacts + tuple(capture.artifacts)

        gate = self._g3_review(robustness)
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

    # -- robustness checks in the sandbox ---------------------------------------

    def _execute_checks(
        self,
        ctx: NodeContext,
        services: NodeServices,
        judgement: Mapping[str, Any],
        capture: _SandboxCapture,
    ) -> dict[str, Any]:
        def skipped(reason: str) -> dict[str, Any]:
            return {"executed": False, "reason": reason}

        if services.tools is None:
            return skipped("未配置工具端口，跳过稳健性复跑")
        supervisor = (services.extras or {}).get("subagents")
        if supervisor is None:
            return skipped("未配置子代理监督者，跳过稳健性复跑")
        if not supports_chat(services.llm):
            return skipped("模型端口不支持会话式调用，跳过稳健性复跑")
        files = _workspace_files(ctx, services)
        if EXPERIMENT_SCRIPT_PATH not in files:
            return skipped(f"工作区没有实验脚本 {EXPERIMENT_SCRIPT_PATH}，无法复跑")

        governor = (services.extras or {}).get("budget_governor")
        budgets: RunBudget = (
            governor.subagent_slice() if governor is not None else RunBudget()
        )
        if budgets.max_sandbox_runs < 1 or budgets.max_llm_calls < 2:
            return skipped("剩余预算不足以派发检验子代理")

        try:
            code = _workspace_reader(ctx, services)(EXPERIMENT_SCRIPT_PATH)
        except FileNotFoundError as exc:
            return skipped(f"读取实验脚本失败：{exc}")
        if not code.strip():
            return skipped(f"实验脚本 {EXPERIMENT_SCRIPT_PATH} 为空，无法复跑")

        plan = chosen_plan(
            _require_outputs(ctx, TaskState.MODEL_PLANNING), ctx.review_decisions
        )
        experiment = dict(ctx.prior_outputs.get(TaskState.EXPERIMENTING.value) or {})
        metrics = dict(experiment.get("metrics") or {})
        risk_points = _risk_points(plan, judgement)
        data_files = [
            path
            for path in files
            if path.startswith(DATA_DIR_PREFIX) or path.startswith("cleaned/")
        ]
        template = self._registry.get(self.sandbox_prompt_id)
        system_prompt = template.render({
            "chosen_plan": json.dumps(plan, ensure_ascii=False),
            "experiment_summary": str(
                experiment.get("experiment_summary") or experiment.get("stdout_tail") or "无"
            ),
            "metrics": json.dumps(metrics, ensure_ascii=False),
            "experiment_code": _clip_code(code),
            "risk_points": risk_points,
            "data_files": "\n".join(f"- {path}" for path in data_files) or "无",
            "available_packages": self._available_packages,
        })

        final_answer: dict[str, Any] = {}
        task = SandboxTask(
            task_id=f"{ctx.step_id}:robustness",
            goal="复跑实验逻辑，在受控扰动下检验结论的稳健性并逐项给出判定",
            system_prompt=system_prompt,
            task_brief=tool_protocol_note(SANDBOX_TOOL_NAMES),
            assertions=(
                SandboxAssertion(
                    id="run_ok",
                    description="检验脚本经 python_run 成功运行（退出码 0）",
                    check=_experiment_run_ok_check,
                ),
                SandboxAssertion(
                    id="checks_reported",
                    description=(
                        f"打印检验结果标记行 OMM_METRICS_JSON（checks 列表 ≥ {MIN_ROBUSTNESS_CHECKS} 项，"
                        "每项含 id/name/passed/value/threshold）"
                    ),
                    check=_robustness_checks_check,
                ),
            ),
            seeds=dict(SANDBOX_SEEDS),
            max_runs=max(1, min(self.max_sandbox_runs, budgets.max_sandbox_runs)),
        )

        llm_calls = {"count": 0}
        chat = text_protocol_chat(
            services.llm,
            label=self.sandbox_prompt_id,
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
                    ctx, services, capture, "validation_checks.py"
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
                    "chosen_plan": plan,
                    "metrics": metrics,
                    "risk_points": risk_points,
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
                "reason": f"检验子代理未完成（{envelope.status}"
                + (f"，{envelope.error_code}" if envelope.error_code else "")
                + "）；检验结论沿用评审判读",
            }

        report = envelope.output
        status = str(report.get("status") or "failed")
        checks = _normalize_checks(capture.metrics) if status == "passed" else []
        failed = [check for check in checks if not check["passed"]]
        return {
            "executed": True,
            "status": status,
            "attempts": int(report.get("attempts") or 0),
            "llm_calls": llm_calls["count"],
            "summary": str(final_answer.get("summary") or ""),
            "checks": checks,
            "checks_total": len(checks),
            "checks_failed": len(failed),
            "failed_checks": failed,
            "summary_text": _robustness_summary_text(status, checks),
            "final_code_artifact": str(report.get("final_code_artifact") or ""),
            "produced_artifacts": list(report.get("produced_artifacts") or []),
        }

    # -- G3 gate ----------------------------------------------------------------

    @staticmethod
    def _g3_review(
        robustness: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        if not robustness.get("executed") or robustness.get("status") != "passed":
            return None
        failed = [dict(check) for check in robustness.get("failed_checks") or []]
        total = int(robustness.get("checks_total") or 0)
        if not failed or total <= 0:
            return None
        names = "、".join(str(check.get("name") or check.get("id")) for check in failed)
        reason = (
            f"稳健性检查 {total} 项中 {len(failed)} 项未通过：{names}。"
            "请确认实验结果的处置方式"
        )
        recommended = (
            "redo:EXPERIMENTING"
            if len(failed) / total >= G3_REDO_RECOMMEND_RATIO
            else G3_ACCEPT_OPTION_ID
        )
        options = []
        for option in G3_OPTIONS:
            entry = dict(option)
            if entry["id"] == recommended:
                entry["recommended"] = True
            options.append(entry)
        meta = {
            "gate": "G3",
            "decision_type": "generic",
            "title": reason,
            "options": options,
            "impact": {
                "checks_total": total,
                "checks_failed": len(failed),
                "failed": failed,
                "recommended": recommended,
            },
        }
        return reason, meta


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
#: 全文的有界重写总额度（字数越界与无出处数字共享；控成本：不给多话的模型每章都加一次调用）。
_MAX_LENGTH_REVISIONS = 2
#: source_keys 的合法取值与材料标题（总编给每章指定材料，缺失时给全量）。
_MATERIAL_LABELS = {
    "problem_analysis": "问题分析结果（JSON）",
    "chosen_plan": "已确认的建模方案（JSON）",
    "experiment_summary": "实验过程摘要",
    "validation_summary": "检验结论",
    "frozen_numbers": "数字冻结清单（正文数值只准引用此表与上述材料中的数字）",
}
#: 冻结清单不受总编的 source_keys 路由影响：每章都必须看到它（§9 硬规则）。
_ALWAYS_MATERIAL_KEYS = ("frozen_numbers",)
#: 四份叙述材料（审计允许集的文本来源；冻结清单本身按值进允许集）。
_NARRATIVE_MATERIAL_KEYS = (
    "problem_analysis",
    "chosen_plan",
    "experiment_summary",
    "validation_summary",
)

# ── G4 定稿交付闸门（§11.1「必停」）────────────────────────────────────────
#
# 论文草稿发布后不直接 succeeded：草稿是产品交付物，定稿前必须过人的眼。
# 「确认交付」走正向推进（末节点 → COMPLETED）；「退回修改」复用修订门的
# ``redo:<STATE>`` 选项 id（引擎零改动）——用户先在聊天框写明修改要求（§11.3
# 运行中备注会作为「用户补充要求」注入重写的每一次调用），再选它。

G4_CONFIRM_OPTION_ID = "confirm_delivery"
G4_REDO_OPTION_ID = "redo:PAPER_WRITING"
G4_OPTIONS = (
    {
        "id": G4_CONFIRM_OPTION_ID,
        "label": "确认交付",
        "description": "接受当前论文草稿作为交付稿，任务进入已完成；之后仍可在结果页发起修改",
    },
    {
        "id": G4_REDO_OPTION_ID,
        "label": "退回修改",
        "description": (
            "重写论文（数据、方案与实验结果不变）。请先在聊天框写明修改要求，"
            "重写时会作为「用户补充要求」注入每一章"
        ),
    },
)
#: 卡片标题里最多点名几处审计发现（完整清单在 impact / DocumentDraft）。
_G4_TITLE_FINDINGS = 2


def _emit_progress(services: NodeServices, payload: dict[str, Any]) -> None:
    """节点内进度事件（run.log 旁路观测通道）：装配缺失或回调抛错绝不影响执行。"""
    callback = (services.extras or {}).get("progress")
    if not callable(callback):
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001 - 过程展示绝不允许拖垮任务本身
        pass


def _validation_material(
    validation: Mapping[str, Any], review_decisions: Mapping[str, str]
) -> str:
    """论文的检验材料：评审判读 + 沙盒复跑的稳健性结论 + G3 决策台账。

    稳健性一句话由验证节点按标记行数字生成（不是模型转述）；用户在 G3 选了
    「接受并记录局限」时把这条纪律写进材料——未通过的检查项必须进论文的局限性，
    不允许因为用户点了接受就把它们淡化掉。
    """
    summary = str(validation.get("validation_summary") or "无")
    robustness = validation.get("robustness")
    if isinstance(robustness, Mapping) and robustness.get("executed"):
        text = str(robustness.get("summary_text") or "").strip()
        if text:
            summary = f"{summary}\n{text}"
    if review_decisions.get(TaskState.VALIDATING.value) == G3_ACCEPT_OPTION_ID:
        summary += (
            "\n用户已在结果采用闸门确认「接受并记录局限」：未通过的检查项必须在"
            "模型检验与局限性部分如实说明，不得淡化。"
        )
    return summary


def _inputs_hash(variables: Mapping[str, str]) -> str:
    """四份输入材料的指纹：断点续写只在输入未变时生效（变了就整篇重来）。"""
    canonical = json.dumps(
        {key: variables[key] for key in sorted(variables)}, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PaperWritingNode(LlmSkillNode):
    """论文撰写：分章多轮生成，总编失败自动回退单次调用（不比旧链路差）。

    H5 切片 1 加了两道纪律：**数字冻结**——上游结构化产出里的数值确定性抽成清单
    进每章材料，章级审计对不上账的数值先有界重写、仍对不上记审计发现；**G4 定稿
    闸门**——草稿发布后必停，审计发现进卡片，由人「确认交付 / 退回修改」。
    """

    prompt_id = "paper_writing.default"
    state = TaskState.PAPER_WRITING

    def __init__(self, registry: PromptRegistry, require_confirmation: bool = True) -> None:
        super().__init__(registry)
        # G4 定稿闸门是产品的人工门（§11.1 必停）；与 G1 同一把开关：评测 /
        # 无人值守自动化可显式关掉，审计照做、只是不停。
        self._require_confirmation = require_confirmation

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        experiment = ctx.prior_outputs.get(TaskState.EXPERIMENTING.value) or {}
        validation = ctx.prior_outputs.get(TaskState.VALIDATING.value) or {}
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(
                chosen_plan(planning, ctx.review_decisions), ensure_ascii=False
            ),
            "experiment_summary": str(experiment.get("experiment_summary") or "无"),
            "validation_summary": _validation_material(validation, ctx.review_decisions),
            "frozen_numbers": render_frozen_numbers(build_frozen_numbers(ctx.prior_outputs)),
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

        # 数字冻结清单（值 + 出处）与审计允许集：冻结值 ∪ 四份材料里出现的数值。
        # 允许集用全部材料而非本章路由到的那几份——审计问的是「有没有出处」，
        # 不是「总编有没有把那份材料发给这一章」；题面常数就靠这里放行。
        frozen = build_frozen_numbers(ctx.prior_outputs)
        allowed = allowed_number_tokens(
            frozen, *(variables[key] for key in _NARRATIVE_MATERIAL_KEYS)
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
                return self._run_single_call(
                    ctx, services, variables, attempts_total, str(error), frozen, allowed
                )

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
        audit_rewrites = 0
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
            # 数字审计 → 一次有界重写（与字数重写共享全文额度）：把对不上账的数值
            # 逐个点名喂回去，只有重写后无出处数字更少才采纳；仍有剩余的进审计发现，
            # 由 G4 交人裁决——不硬阻断，清单本身也可能漏（单位换算、题面常数）。
            unsourced = unsourced_numbers(content, allowed)
            if unsourced and revisions_used < _MAX_LENGTH_REVISIONS:
                revisions_used += 1
                audit_rewrites += 1
                revised, extra_attempts, _audit_error = complete_validated(
                    services,
                    section_template,
                    {
                        **section_vars,
                        "__repair_error": (
                            f"content 里有 {len(unsourced)} 个数值在数字冻结清单与材料中"
                            f"找不到出处：{'、'.join(unsourced)}。请逐个改为清单/材料中的原始"
                            "数值，或删去无法溯源的数字与相应表述；其余内容、公式与结构不变，"
                            "只输出同格式 JSON。"
                        ),
                        "__previous_output": content[:2000],
                    },
                )
                attempts_total += extra_attempts
                if revised is not None:
                    candidate = str(revised.get("content") or "").strip()
                    if candidate and len(unsourced_numbers(candidate, allowed)) < len(unsourced):
                        section = revised
                        content = candidate
            digest = str(section.get("digest") or "").strip()[:_DIGEST_CHARS]
            sections.append({"heading": heading, "content": content})
            digests.append(f"第{index}章《{heading}》：{digest or '（无摘要）'}")
            warnings.extend(self._section_warnings(index, heading, content, target, allowed))
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
            "frozen_numbers": variables["frozen_numbers"],
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
        # 摘要转述各章：各章摘要里的数值也算有出处（那些章节已各自过审计）
        abstract_allowed = allowed | number_tokens(*digests)
        unsourced = unsourced_numbers(abstract, abstract_allowed)
        if unsourced:
            warnings.append(
                f"摘要有 {len(unsourced)} 个数值未在冻结清单、材料与各章摘要中找到出处"
                f"（如 {'、'.join(unsourced[:3])}）"
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
        if audit_rewrites:
            metrics_payload["audit_rewrites"] = audit_rewrites
        if warnings:
            metrics_payload["quality_warnings"] = warnings
        return self._publish(
            ctx, services, outputs, metrics_payload, frozen, allowed, abstract_allowed
        )

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
        """按总编指定的 source_keys 组装本章材料；无有效指定时给全量。

        数字冻结清单不受路由影响，每章必带（总编漏写也补上）。
        """
        keys = [
            key for key in (source_keys if isinstance(source_keys, list) else [])
            if key in _MATERIAL_LABELS
        ]
        if not keys:
            keys = list(_MATERIAL_LABELS)
        for key in _ALWAYS_MATERIAL_KEYS:
            if key not in keys:
                keys.append(key)
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
        index: int, heading: str, content: str, target: int, allowed: set[str]
    ) -> list[str]:
        """章级软校验：只记警告不阻断（执行事实如实上报，人来裁量）。"""
        warnings: list[str] = []
        lower = int(target * (1 - _SECTION_LENGTH_TOLERANCE))
        upper = int(target * (1 + _SECTION_LENGTH_TOLERANCE))
        if content and not lower <= len(content) <= upper:
            warnings.append(
                f"第{index}章《{heading}》字数 {len(content)} 偏离目标 {target}（±30%）"
            )
        unsourced = unsourced_numbers(content, allowed)
        if unsourced:
            warnings.append(
                f"第{index}章《{heading}》有 {len(unsourced)} 个数值未在冻结清单与材料中找到出处"
                f"（如 {'、'.join(unsourced[:3])}）"
            )
        return warnings

    def _run_single_call(
        self,
        ctx: NodeContext,
        services: NodeServices,
        variables: dict[str, Any],
        attempts_before: int,
        fallback_reason: str,
        frozen: list[dict[str, Any]],
        allowed: set[str],
    ) -> NodeResult:
        """回退路径：总编规划失败时整篇单次生成（paper_writing.default v5）。"""
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
        return self._publish(ctx, services, parsed, metrics_payload, frozen, allowed)

    def _publish(
        self,
        ctx: NodeContext,
        services: NodeServices,
        outputs: dict[str, Any],
        metrics: dict[str, Any],
        frozen: list[dict[str, Any]],
        allowed: set[str],
        abstract_allowed: set[str] | None = None,
    ) -> NodeResult:
        """发布草稿产物 → 终稿数字审计 → G4 必停。

        审计在这里对**终稿**做（分章路径已按章重写过一次，这里是最终对账；
        回退单次生成的路径没有章级重写，全靠这一道），结果同时进 outputs
        （DocumentDraft 契约的 frozen_numbers / audit_findings）与 G4 卡片。
        """
        sections = [
            section for section in outputs.get("sections") or [] if isinstance(section, Mapping)
        ]
        findings = audit_document(
            sections, str(outputs.get("abstract") or ""), allowed, abstract_allowed
        )
        outputs = {**outputs, "frozen_numbers": list(frozen), "audit_findings": findings}
        markdown = render_paper_markdown(outputs)
        ref = services.artifacts.put(
            ctx.run_id,
            "paper",
            "paper-draft.md",
            markdown.encode("utf-8"),
            "text/markdown",
            ctx.step_id,
        )
        chars = sum(len(str(section.get("content") or "")) for section in sections)
        # 发布标记：告诉断点续写的读取器「这一趟已经交稿」。之后若人在 G4 / 修订门
        # 要求重写，同样的输入指纹也不得复用这趟的章节——否则重做等于原样重发。
        _emit_progress(services, {
            "kind": "paper_published",
            "chapters": len(sections),
            "chars": chars,
            "audit_findings": len(findings),
        })
        if not self._require_confirmation:
            return NodeResult.succeeded(outputs=outputs, metrics=metrics, artifacts=(ref,))
        reason, meta = self._g4_review(len(sections), chars, len(frozen), findings)
        return NodeResult.needs_review(
            reason=reason,
            outputs=outputs,
            review_meta=meta,
            metrics=metrics,
            artifacts=(ref,),
        )

    # -- G4 gate ----------------------------------------------------------------

    @staticmethod
    def _g4_review(
        chapters: int,
        chars: int,
        frozen_total: int,
        findings: Sequence[Mapping[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """G4 卡片：0 审计发现推荐「确认交付」，否则推荐「退回修改」（人可改选）。"""
        if findings:
            named = "；".join(
                str(f.get("detail") or f.get("scope")) for f in findings[:_G4_TITLE_FINDINGS]
            )
            more = f" 等 {len(findings)} 处" if len(findings) > _G4_TITLE_FINDINGS else ""
            reason = (
                f"论文草稿已生成（{chapters} 章，约 {chars} 字）；数字审计发现 {len(findings)} 处"
                f"无出处数值{more}：{named}。请确认是否交付，或写明修改要求后退回修改"
            )
            recommended = G4_REDO_OPTION_ID
        else:
            reason = (
                f"论文草稿已生成（{chapters} 章，约 {chars} 字，冻结数字 {frozen_total} 项"
                "全部对账通过）。请确认是否交付"
            )
            recommended = G4_CONFIRM_OPTION_ID
        options = []
        for option in G4_OPTIONS:
            entry = dict(option)
            if entry["id"] == recommended:
                entry["recommended"] = True
            options.append(entry)
        meta = {
            "gate": "G4",
            "decision_type": "generic",
            "title": reason,
            "options": options,
            "impact": {
                "chapters": chapters,
                "chars": chars,
                "frozen_numbers_total": frozen_total,
                "audit_findings_total": len(findings),
                "audit_findings": [dict(f) for f in findings[:AUDIT_SAMPLE_LIMIT]],
                "recommended": recommended,
            },
        }
        return reason, meta
