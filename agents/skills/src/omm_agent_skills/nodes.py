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
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from omm_agent_core import KnowledgePort, NodeContext, NodeResult, NodeServices, TaskState
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
from .review import (
    CLEANING_REVIEW_FOCUS,
    CLEANING_REVIEW_PROMPT_ID,
    EXPERIMENT_REVIEW_FOCUS,
    REVIEW_MAX_ROUNDS,
    REVIEW_PROMPT_ID,
    REVIEWER_KNOWLEDGE_TOOL_NAMES,
    REVIEWER_LOOP_BUDGET,
    REVIEWER_MAX_TOOL_ROUNDS,
    REVIEWER_TOOL_NAMES,
    ROBUSTNESS_REVIEW_FOCUS,
    ROBUSTNESS_REVIEW_PROMPT_ID,
    compare_metrics,
    findings_material,
    normalize_verdict,
    rerun_material,
    review_feedback,
    review_material,
    reviewer_tool_brief,
    verdict_summary_text,
)
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

# ── 提议人自主检索（§10.3 切片二）：知识库两个只读工具 + 多轮文本协议会话 ────────
#: 提议人子代理可用的工具名（装配期契约：与 omm_agent_tools 的 knowledge_search /
#: knowledge_read 注册名一致；skills 与 tools 无依赖边，按名字对齐）。
PROPOSER_TOOL_NAMES: tuple[str, ...] = ("knowledge_search", "knowledge_read")
#: 每路提议人至多几轮工具信封（文本协议一轮一个信封）：三轮够「专名精确查 →
#: 概念词兜底 → 顺链读全卡」；超出后执行器回失败观察请模型直接终答。
PROPOSER_MAX_TOOL_ROUNDS = 3
#: 提议人内环预算：工具轮上限 + 一轮「已用完」观察 + 终答轮；一次结构修复；
#: 同一信封连发两次 / 同一工具连败两次即判无进展（检索是配菜，不许在这里耗预算）。
PROPOSER_LOOP_BUDGET = LoopBudget(
    max_turns=PROPOSER_MAX_TOOL_ROUNDS + 2, repairs=1, no_progress_k=2, tool_fail_m=2
)


def proposer_tool_brief() -> str:
    """提议人会话的开场消息：工具协议（单一出处 chat_adapter）+ 检索策略。

    模板 ``model_planning.proposer`` 保持不变——先例材料段仍由节点预检索渲染；
    这里只告诉模型「还可以自己查」以及怎么查才不浪费轮次。
    """
    return (
        tool_protocol_note(PROPOSER_TOOL_NAMES, final_hint="按「输出要求」输出终答 JSON（终答不含 tool 键）")
        + "\n\n检索策略：预检索的先例材料已在角色卡里；只有它不够用时才检索，"
        f"每次一个信封、全程至多 {PROPOSER_MAX_TOOL_ROUNDS} 次。先用题面里的专名 / 题号 / "
        "竞赛名精确查，未命中再退到建模方向、方法名等概念词；命中后用 knowledge_read "
        "顺链读全卡再决定借鉴什么。借鉴到的卡片按「输出要求」用卡片 id 标出处，"
        "没读到的卡片不得标；不需要检索就直接输出终答。"
    )


def _bounded_tool_executor(
    ctx: NodeContext,
    services: NodeServices,
    allowed: Sequence[str],
    max_rounds: int,
):
    """ToolExecutor：允许清单 + 轮数限额；每次调用经 services.tools 留 TOOL_CALLED。

    超出限额不抛异常——回一条 failed 观察让模型转入终答；内环的 tool_fail_m
    闸门保证它最多再赖一轮。越出允许清单同样只回观察（与沙盒执行器同款）。
    """
    allowed_set = set(allowed)
    rounds = {"used": 0}

    def execute(calls):
        rounds["used"] += 1
        results: list[ToolResult] = []
        for call in calls:
            if rounds["used"] > max_rounds:
                results.append(
                    ToolResult(
                        status="failed",
                        error=f"检索次数已用完（至多 {max_rounds} 次），请直接输出终答 JSON",
                    )
                )
                continue
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
            results.append(
                services.tools.invoke(ctx.run_id, ctx.step_id, call.name, dict(call.arguments))
            )
        return results

    return execute

# ── 知识库预检索材料（§10.3 薄版一）：fan-out 前节点用题面确定性检索一次 ────────
#: 「相似赛题与获奖论文方法」材料的三种「无」：端口未接 / 没命中 / 检索抛错，
#: 提示词按「无」忽略本节；节点永不因知识库而失败。
NO_KNOWLEDGE_NOTE = "无（知识库不可用）"
NO_KNOWLEDGE_HITS_NOTE = "无（知识库未命中相似赛题或获奖论文）"
KNOWLEDGE_PROBLEM_HITS = 4
KNOWLEDGE_PAPER_HITS = 3
#: 检索 query 的字数上限：题名 + 题型 + 目标 + 子问题已足够定位相似赛题，
#: 再长只会把 BM25 稀释到通用词上。
KNOWLEDGE_QUERY_CHARS = 600
#: 材料整段上限（进 Proposer prompt，三路各一份，必须有界）。
KNOWLEDGE_MATERIAL_CHARS = 1500
_KNOWLEDGE_CARD_REF = re.compile(r"\[((?:problem|paper):[^\]\s]+)\]")


def knowledge_query(analysis: Mapping[str, Any]) -> str:
    """问题分析产出 → 检索 query（题名 / 题型 / 目标 / 子问题文本；截断到上限）。"""
    parts: list[str] = [
        str(analysis.get("title") or "").strip(),
        str(analysis.get("problem_type") or "").strip(),
    ]
    parts.extend(_clean_strs(analysis.get("objectives")))
    for item in analysis.get("subquestions") or []:
        if isinstance(item, Mapping):
            parts.append(str(item.get("text") or "").strip())
    query = " ".join(part for part in parts if part)
    return query[:KNOWLEDGE_QUERY_CHARS]


def _knowledge_problem_line(hit: Mapping[str, Any]) -> str:
    facets = [
        str(hit.get("problem_type") or "").strip(),
        "、".join(_clean_strs(hit.get("modeling_directions"))),
        "、".join(_clean_strs(hit.get("keywords"))),
    ]
    facet_text = "｜".join(facet for facet in facets if facet)
    head = " ".join(
        str(part).strip()
        for part in (hit.get("year"), hit.get("competition"), hit.get("code"))
        if part not in (None, "")
    )
    line = f"- [{hit['id']}] {head}「{hit.get('title', '')}」"
    if facet_text:
        line += f"（{facet_text}）"
    models = [
        f"{item['model']} ×{item['count']}" if int(item.get("count") or 0) > 1 else str(item["model"])
        for item in hit.get("linked_paper_models") or []
        if isinstance(item, Mapping) and item.get("model")
    ]
    if models:
        line += "——获奖论文用过：" + "、".join(models)
    else:
        line += "——暂无挂接的获奖论文模型记录"
    return line


def _knowledge_paper_line(hit: Mapping[str, Any]) -> str:
    head = " ".join(
        str(part).strip() for part in (hit.get("year"), hit.get("competition")) if part not in (None, "")
    )
    award = str(hit.get("award") or "").strip()
    line = f"- [{hit['id']}] {head}「{hit.get('title', '')}」"
    if award:
        line += f"（{award}）"
    return line + "：模型 = " + "、".join(_clean_strs(hit.get("models")))


def knowledge_material(port: KnowledgePort | None, analysis: Mapping[str, Any]) -> str:
    """「相似赛题与获奖论文方法」材料：top-N 赛题卡（附挂接论文的模型）+ 有模型记录的论文卡。

    确定性、无 LLM 调用；端口缺席 / 无命中 / 检索抛错分别落三种「无」文案，
    节点照常推进。整段有界（``KNOWLEDGE_MATERIAL_CHARS``，按整行裁）。
    """
    if port is None:
        return NO_KNOWLEDGE_NOTE
    query = knowledge_query(analysis)
    if not query:
        return NO_KNOWLEDGE_HITS_NOTE
    try:
        problems = port.search(query, kind="problem", limit=KNOWLEDGE_PROBLEM_HITS)
        papers = port.search(query, kind="paper", limit=KNOWLEDGE_PAPER_HITS * 4)
    except Exception as exc:  # noqa: BLE001 — 知识库是材料不是闸门：检索失败只记「无」
        return f"无（知识库检索失败：{exc}）"

    lines: list[str] = []
    shown_problems: set[str] = set()
    for hit in problems:
        if not isinstance(hit, Mapping) or not hit.get("id"):
            continue
        shown_problems.add(str(hit["id"]))
        lines.append(_knowledge_problem_line(hit))
    paper_lines = 0
    for hit in papers:
        if paper_lines >= KNOWLEDGE_PAPER_HITS:
            break
        if not isinstance(hit, Mapping) or not hit.get("id") or not _clean_strs(hit.get("models")):
            continue
        # 已展示赛题的挂接论文：模型已在赛题行聚合过，不再单列
        if str(hit.get("problem_id") or "") in shown_problems:
            continue
        lines.append(_knowledge_paper_line(hit))
        paper_lines += 1
    if not lines:
        return NO_KNOWLEDGE_HITS_NOTE

    kept: list[str] = []
    total = 0
    for line in lines:
        if kept and total + len(line) + 1 > KNOWLEDGE_MATERIAL_CHARS:
            break
        kept.append(line[:KNOWLEDGE_MATERIAL_CHARS])
        total += len(kept[-1]) + 1
    return "\n".join(kept)


def knowledge_hit_ids(material: str) -> list[str]:
    """材料文本里引用的卡片 id（去重、保序）：进 spawn 的 context_slice 供审计。"""
    seen: dict[str, None] = {}
    for match in _KNOWLEDGE_CARD_REF.finditer(material):
        seen.setdefault(match.group(1), None)
    return list(seen)

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

#: 方案卡「实现语言」（§7.4：实现语言随 G1 确认并决定执行器路由）。当前唯一执行器是
#: Python 沙箱；装配方按 ExecutorProfile（H7）传入更多语言时，归约人才有得选。
IMPLEMENTATION_LANGUAGES: tuple[str, ...] = ("python",)

#: 模型常见写法 → 契约小写标识。认不出的原样小写后交给可用列表判定。
_LANGUAGE_ALIASES = {
    "python": "python", "python3": "python", "py": "python", "cpython": "python",
    "r": "r", "rscript": "r", "r language": "r", "r 语言": "r",
    "matlab": "matlab", "octave": "octave", "gnu octave": "octave",
    "julia": "julia", "baltamatica": "baltamatica", "北太天元": "baltamatica",
}


def normalize_language(value: Any) -> str:
    """单个语言值 → 小写标识（空值 → 空串）。"""
    text = str(value or "").strip().lower()
    return _LANGUAGE_ALIASES.get(text, text)


def normalize_plan_languages(
    plans: Sequence[dict[str, Any]], allowed: Sequence[str] = IMPLEMENTATION_LANGUAGES
) -> list[str]:
    """就地把每张方案卡的 ``language`` 收敛到可用语言，返回警告文案。

    缺省 → 可用列表首项（只有一种语言时模型没得选，缺字段不算错）；写了但不在
    可用列表 → 也按首项记，并留一条警告（方案阶段可换、要留痕；实验阶段的
    「无匹配执行器显式失败、不静默换语言」约束的是执行器路由，那是 H7 的事）。
    """
    allowed_ids = [normalize_language(item) for item in allowed if normalize_language(item)]
    default = allowed_ids[0] if allowed_ids else IMPLEMENTATION_LANGUAGES[0]
    warnings: list[str] = []
    for plan in plans:
        wanted = normalize_language(plan.get("language"))
        if wanted and wanted not in allowed_ids:
            warnings.append(
                f"方案 {plan.get('id')} 提出的实现语言 {wanted} 当前执行器不支持，按 {default} 记"
            )
            wanted = default
        plan["language"] = wanted or default
    return warnings

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
    """G1 选项 description：角色 + 思路首句 + 实现语言（卡片版面有限）。

    语言随 G1 一并确认（§7.4），所以要出现在用户点选的那一行上。
    """
    role = PLAN_ROLE_LABELS.get(str(plan.get("role") or ""), PLAN_ROLE_LABELS["candidate"])
    approach = str(plan.get("approach") or "").strip()
    lead = re.split(r"(?<=[。；;.!?！？])", approach, maxsplit=1)[0].strip() or approach
    if len(lead) > 80:
        lead = lead[:79] + "…"
    condition = str(plan.get("fallback_condition") or "").strip()
    blurb = f"{role}：{lead}（触发条件：{condition}）" if condition else f"{role}：{lead}"
    language = normalize_language(plan.get("language"))
    if language:
        blurb += f"；实现语言 {language}"
    return blurb


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
        knowledge: KnowledgePort | None = None,
        implementation_languages: Sequence[str] = IMPLEMENTATION_LANGUAGES,
    ) -> None:
        super().__init__(registry)
        # Plan confirmation is the product's human gate (roadmap: 方案 A/B 生成、
        # 用户确认). Evals/automation may disable it explicitly.
        self._require_confirmation = require_confirmation
        self._views = tuple(proposer_views)
        # 卡片知识库端口（§10.3）：装配方注入 omm_agent_tools.KnowledgeLibrary；
        # 缺席时材料落「无（知识库不可用）」，方案阶段照常。
        self._knowledge = knowledge
        # 当前执行器能跑的实现语言（§7.4）：归约人只能在这里面给每张卡定语言；
        # 空列表退回缺省，方案卡不会没有语言。
        languages = tuple(
            dict.fromkeys(
                normalize_language(item) for item in implementation_languages if normalize_language(item)
            )
        )
        self._languages: tuple[str, ...] = languages or IMPLEMENTATION_LANGUAGES

    @property
    def knowledge(self) -> KnowledgePort | None:
        return self._knowledge

    @property
    def implementation_languages(self) -> tuple[str, ...]:
        return self._languages

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
            # 相似赛题与获奖论文方法：进提议人 prompt 与单次回落；归约 / 规范化不带
            "knowledge": knowledge_material(self._knowledge, analysis),
            # 可用实现语言：归约人 / 单次回落为每张卡定 language（提议人不选）
            "implementation_languages": "、".join(self._languages),
        }

    def to_result(self, parsed: dict[str, Any], attempts: int) -> NodeResult:
        plans = [dict(plan) for plan in parsed.get("plans", []) if isinstance(plan, Mapping)]
        plan_ids = [plan.get("id") for plan in plans]
        if parsed.get("recommended_plan_id") not in plan_ids:
            return NodeResult.failed(
                "recommended_plan_id does not reference a returned plan"
            )
        # 单次调用路径同样带实现语言：缺省补 python，越出可用列表归一并留痕
        language_warnings = normalize_plan_languages(plans, self._languages)
        outputs: dict[str, Any] = {**parsed, "plans": plans}
        if language_warnings:
            outputs["quality_warnings"] = [
                *[str(item) for item in parsed.get("quality_warnings") or []],
                *language_warnings,
            ]
        if self._require_confirmation:
            return NodeResult.needs_review(
                reason="请确认建模方案（A/B）后继续实验",
                outputs={**outputs, "llm_attempts": attempts},
            )
        return NodeResult.succeeded(
            outputs=outputs, metrics={"llm_attempts": attempts}
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
        # 每张卡的实现语言收敛到当前执行器可用的语言（§7.4：语言随 G1 确认）
        plans = [dict(plan) for plan in reduced["plans"]]
        warnings.extend(normalize_plan_languages(plans, self._languages))
        reduced = {**reduced, "plans": plans}

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
        knowledge_hits = knowledge_hit_ids(str(variables.get("knowledge") or ""))
        # 提议人自主检索（§10.3 切片二）三件齐才开：知识库端口在、模型端口支持
        # 会话、ToolBus 在（两边装配都注册了 knowledge_search / knowledge_read）；
        # 缺一件回到单次调用路径，行为与载荷逐字节不变（evals / 旧装配 / 无知识库）。
        tool_use = self._proposer_tool_use(services)
        toolset: tuple[str, ...] = PROPOSER_TOOL_NAMES if tool_use else ()

        def spawn_one(
            view: ProposerView,
        ) -> tuple[ProposerView, ResultEnvelope | None, int, str, AgentError | None]:
            trace: dict[str, Any] = {"attempts": 0, "error": "", "agent_error": None}
            view_variables = {**variables, "view_name": view.name, "view_brief": view.brief}

            def runner(_spec: SpawnSpec) -> ResultEnvelope:
                try:
                    if tool_use:
                        parsed, attempts, error = self._propose_with_tools(
                            ctx, services, template, view, view_variables
                        )
                    else:
                        parsed, attempts, error = complete_validated(
                            services, template, view_variables
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
                            # 预检索命中的卡片 id：审计「提议人看过哪些先例」
                            "knowledge_hits": knowledge_hits,
                        },
                        toolset=toolset,
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

    def _proposer_tool_use(self, services: NodeServices) -> bool:
        return (
            self._knowledge is not None
            and services.tools is not None
            and supports_chat(services.llm)
        )

    def _propose_with_tools(
        self,
        ctx: NodeContext,
        services: NodeServices,
        template: PromptTemplate,
        view: ProposerView,
        variables: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, int, str | None]:
        """一路提议人的多轮会话：可自主调用知识库两个只读工具，再交终答。

        与 ``complete_validated`` 同一份 (parsed, attempts, error) 契约与同一道
        结构校验（模板 output_schema + extract_json）；差别只在传输：system =
        模板渲染全文（与沙盒任务卡同构，用户备注由端口拼进 system），user =
        工具协议 + 检索策略。attempts = 会话里的模型调用次数（含工具轮）。
        """
        outcome = run_inner_loop(
            LoopTask(
                task_id=f"{ctx.step_id}:proposer:{view.id}",
                messages=(
                    Message(role="system", content=template.render(dict(variables))),
                    Message(role="user", content=proposer_tool_brief()),
                ),
                validator=lambda value: validate(value, template.output_schema),
                parser=extract_json,
                budget=PROPOSER_LOOP_BUDGET,
            ),
            chat=text_protocol_chat(services.llm, label=PROPOSER_PROMPT_ID),
            execute_tools=_bounded_tool_executor(
                ctx, services, PROPOSER_TOOL_NAMES, PROPOSER_MAX_TOOL_ROUNDS
            ),
        )
        if outcome.ok:
            return outcome.value, outcome.llm_calls, None
        return None, outcome.llm_calls, outcome.last_error or outcome.exit_reason

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
                "implementation_languages": variables["implementation_languages"],
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


# ── 假设表的下游消费（实验 / 验证 / 论文材料共用） ──────────────────────────────

#: 假设状态里需要在实验 / 验证阶段专门照顾的两档（§9.1「假设表 → 敏感性与稳健性
#: 检验」）：critical 排前，to_verify 其后；confirmed 只需实现时遵守。
ASSUMPTION_FOCUS_STATUSES = ("critical", "to_verify")
_ASSUMPTION_STATUS_LABELS = {
    "confirmed": "已确认",
    "to_verify": "待检验",
    "critical": "重点验证",
}
_ASSUMPTION_IMPACT_LABELS = {"low": "低", "medium": "中", "high": "高"}
NO_ASSUMPTIONS_NOTE = "无（方案阶段未生成假设表）"


def plan_assumptions(
    planning: Mapping[str, Any], plan_id: Any
) -> list[dict[str, Any]]:
    """选定方案适用的假设：全局 + 该方案专有，保持方案阶段给出的顺序。

    只收结构完整的行（id / text 非空）；旧运行与单次调用路径没有 ``assumptions``
    键 → 空表，下游一律按「未生成假设表」处理，不报错。
    """
    rows: list[dict[str, Any]] = []
    for item in planning.get("assumptions") or []:
        if not isinstance(item, Mapping):
            continue
        row_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        scope = str(item.get("scope") or GLOBAL_ASSUMPTION_SCOPE)
        if not row_id or not text:
            continue
        if scope != GLOBAL_ASSUMPTION_SCOPE and scope != str(plan_id):
            continue
        rows.append(
            {
                "id": row_id,
                "text": text,
                "scope": scope,
                "basis": str(item.get("basis") or "").strip(),
                "impact": str(item.get("impact") or "medium"),
                "status": str(item.get("status") or "to_verify"),
            }
        )
    return rows


def assumptions_to_verify(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """需要专门检验的假设：重点验证在前、待检验其后，组内保持原顺序。"""
    ordered: list[dict[str, Any]] = []
    for status in ASSUMPTION_FOCUS_STATUSES:
        ordered.extend(dict(row) for row in rows if row.get("status") == status)
    return ordered


def assumption_material(rows: Sequence[Mapping[str, Any]]) -> str:
    """假设表 → 提示词材料段（每行一条，带状态 / 影响 / 适用范围与依据）。"""
    lines: list[str] = []
    for row in rows:
        status = _ASSUMPTION_STATUS_LABELS.get(str(row.get("status")), str(row.get("status")))
        impact = _ASSUMPTION_IMPACT_LABELS.get(str(row.get("impact")), str(row.get("impact")))
        scope = row.get("scope")
        scope_label = "全局" if scope in (None, GLOBAL_ASSUMPTION_SCOPE) else f"方案 {scope}"
        line = f"- {row['id']}【{status}｜影响{impact}｜{scope_label}】{row['text']}"
        basis = str(row.get("basis") or "").strip()
        if basis:
            line += f"（依据：{basis}）"
        lines.append(line)
    return "\n".join(lines) or NO_ASSUMPTIONS_NOTE


# ── 符号表的下游消费（实验任务卡 / 论文材料共用） ──────────────────────────────

_SYMBOL_KIND_LABELS = {
    "set": "集合 / 索引",
    "parameter": "参数",
    "variable": "决策变量",
    "objective": "目标函数",
    "other": "其他",
}
NO_SYMBOLS_NOTE = "无（方案阶段未生成符号表）"
#: 符号比对时抹掉的字符：数学定界符与花括号（``x_{i}`` 与 ``x_i`` 视为同一记号）。
_SYMBOL_STRIP = re.compile(r"[${}]|\\\(|\\\)|\\\[|\\\]")


def _symbol_pattern(symbol: str) -> re.Pattern[str] | None:
    """记号 → 在符号约定文本里找它的正则：字符间容忍空白，两端不能紧贴字母 / 数字。

    边界规则挡住两类误判：单字母记号 ``z`` 不能靠「size」里的 z 算作出现；``x_i`` 不能
    靠 ``x_{ij}`` 算作出现（那是另一个量）。
    """
    core = re.sub(r"\s+", "", _SYMBOL_STRIP.sub("", symbol))
    if not core:
        return None
    body = r"\s*".join(re.escape(char) for char in core)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])")


def plan_symbols(planning: Mapping[str, Any], plan_id: Any) -> list[dict[str, Any]]:
    """选定方案适用的符号：共享（plan_id null）+ 该方案专有，保持方案阶段的顺序。

    只收 symbol / definition 非空的行；旧运行与单次调用路径没有 ``symbols`` 键 →
    空表，下游一律按「未生成符号表」处理，不报错。
    """
    rows: list[dict[str, Any]] = []
    for item in planning.get("symbols") or []:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        definition = str(item.get("definition") or "").strip()
        if not symbol or not definition:
            continue
        owner = item.get("plan_id")
        if owner not in (None, "") and str(owner) != str(plan_id):
            continue
        rows.append(
            {
                "symbol": symbol,
                "kind": str(item.get("kind") or "other"),
                "definition": definition,
                "unit": _optional_text(item.get("unit")),
                "range": _optional_text(item.get("range")),
                "plan_id": None if owner in (None, "") else str(owner),
            }
        )
    return rows


def symbol_material(rows: Sequence[Mapping[str, Any]]) -> str:
    """符号表 → 提示词材料段（每行一条：记号（类型｜范围）＝定义［单位；取值］）。

    记号按契约原样给（不带 ``$``）：实验代码照它命名变量，论文总编再包成行内 LaTeX。
    """
    lines: list[str] = []
    for row in rows:
        kind = _SYMBOL_KIND_LABELS.get(str(row.get("kind")), str(row.get("kind")))
        owner = row.get("plan_id")
        scope_label = "共享" if owner in (None, "") else f"方案 {owner}"
        line = f"- {row['symbol']}（{kind}｜{scope_label}）＝{row['definition']}"
        extras = []
        if row.get("unit"):
            extras.append(f"单位：{row['unit']}")
        if row.get("range"):
            extras.append(f"取值：{row['range']}")
        if extras:
            line += f"［{'；'.join(extras)}］"
        lines.append(line)
    return "\n".join(lines) or NO_SYMBOLS_NOTE


def missing_symbols(
    notation: str, rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """总编的符号约定里漏掉的方案符号（比对忽略定界符 / 花括号 / 字符间空白）。"""
    haystack = _SYMBOL_STRIP.sub("", notation)
    missing: list[dict[str, Any]] = []
    for row in rows:
        pattern = _symbol_pattern(str(row.get("symbol") or ""))
        if pattern is not None and pattern.search(haystack) is None:
            missing.append(dict(row))
    return missing


def complete_notation(
    notation: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """按方案符号表把总编漏掉的记号补进符号约定（确定性，不烧调用；幂等）。

    方案阶段的符号表是全文记号的底稿：总编可以补实验阶段新引入的量，但不能
    丢掉底稿里的记号——丢了就由这里补一段「方案阶段符号表补充」，各章照样看到。
    返回 (补齐后的 notation, 补进去的行)。
    """
    missing = missing_symbols(notation, rows)
    if not missing:
        return notation, []
    lines = ["方案阶段符号表补充（总编符号约定漏列，按方案符号表原样补齐）："]
    for row in missing:
        line = f"- ${row['symbol']}$：{row['definition']}"
        extras = []
        if row.get("unit"):
            extras.append(f"单位：{row['unit']}")
        if row.get("range"):
            extras.append(f"取值：{row['range']}")
        if extras:
            line += f"（{'；'.join(extras)}）"
        lines.append(line)
    base = notation.rstrip()
    completed = ("\n\n".join([base, "\n".join(lines)]) if base else "\n".join(lines))
    return completed, missing


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
    假装执行过。清洗验收通过后进生成者-评审者环（§8.4：复跑核对 + 独立审稿，
    驳回退 R2 修复、僵持记进 ``cleaning["review"]``）。G2 在清洗真实执行且影响面
    超阈值（删行 >5% 或目标列被插补）或审稿僵持时触发（§9.1），选项与决策台账见
    G2_OPTIONS。
    """

    prompt_id = "data_preparation.default"
    review_prompt_id = CLEANING_REVIEW_PROMPT_ID
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

        cleaning, produced = self._execute_cleaning(
            ctx, services, base.outputs, data_files
        )
        outputs = {**base.outputs, "cleaning": cleaning}
        artifacts = base.artifacts + tuple(produced)

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
        data_files: Sequence[str],
    ) -> tuple[dict[str, Any], list[Any]]:
        """清洗沙盒（子代理）→ 验收通过后进生成者-评审者环；返回 (cleaning, 产物引用)。

        产物引用取所有波的并集：修复波只重写 cleaned/ 同名文件，沙盒按「新建文件」
        捕获产物，首波的清洗数据引用不能因为修复而丢。
        """
        def skipped(reason: str) -> tuple[dict[str, Any], list[Any]]:
            return {"executed": False, "reason": reason}, []

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
        target_columns = [
            str(col).strip()
            for col in (parsed.get("target_columns") or [])
            if str(col).strip()
        ]
        total_runs = max(1, min(SandboxTask.max_runs, budgets.max_sandbox_runs))

        llm_calls = {"count": 0}
        chat = text_protocol_chat(
            services.llm,
            label=CLEANING_PROMPT_ID,
            on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
        )
        fingerprint = _env_fingerprint(ctx, services)
        waves: list[_SandboxCapture] = []
        last_envelope: dict[str, Any] = {}

        def sandbox_wave(
            brief_suffix: str | None, max_runs: int
        ) -> _SandboxWaveResult | None:
            """一次经监督者派发的清洗沙盒波（首波 / 按审稿意见修复）。"""
            capture = _SandboxCapture()
            final_answer: dict[str, Any] = {}
            brief = tool_protocol_note(SANDBOX_TOOL_NAMES)
            if brief_suffix:
                brief += "\n\n" + brief_suffix
            task = SandboxTask(
                task_id=f"{ctx.step_id}:cleaning",
                goal="按数据准备方案清洗 data/ 数据文件，产出 cleaned/ 数据与影响面统计",
                system_prompt=system_prompt,
                task_brief=brief,
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
                max_runs=max_runs,
                extra_final_keys=(),
            )
            executor = _sandbox_tool_executor(ctx, services, capture)

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
            waves.append(capture)
            last_envelope["value"] = envelope
            if not envelope.ok or envelope.output is None:
                return None
            return envelope.output, capture, final_answer

        first = sandbox_wave(None, total_runs)
        if first is None:
            envelope = last_envelope["value"]
            return {
                "executed": False,
                "reason": f"清洗子代理未完成（{envelope.status}"
                + (f"，{envelope.error_code}" if envelope.error_code else "")
                + "）；后续阶段按原始数据继续",
            }, _union_artifacts(waves, None)

        report, capture, final_answer = first
        usage = {"runs": int(report["usage"]["runs"]), "waves": int(report["attempts"])}
        review: dict[str, Any] | None = None
        if str(report.get("status")) == "passed":
            # 生成者-评审者（§8.4）：复跑核对 → 独立审稿 → 驳回退 R2 → 僵持进 G2
            review, (report, capture, final_answer) = _run_review_loop(
                ctx,
                services,
                supervisor,
                self._registry,
                self._review_spec(ctx, plan_slice, data_files, target_columns),
                first=first,
                sandbox_wave=sandbox_wave,
                max_runs=total_runs,
                usage=usage,
            )

        impact = _cleaning_impact(capture.metrics, target_columns)
        cleaning = {
            "executed": True,
            "status": str(report.get("status") or "failed"),
            "attempts": usage["waves"],
            "llm_calls": llm_calls["count"] + int((review or {}).get("llm_calls") or 0),
            "summary": str(final_answer.get("summary") or ""),
            "target_columns": target_columns,
            "final_code_artifact": str(report.get("final_code_artifact") or ""),
            "produced_artifacts": list(report.get("produced_artifacts") or []),
            **impact,
        }
        if review is not None:
            cleaning["review"] = review
        return cleaning, _union_artifacts(waves, capture)

    def _review_spec(
        self,
        ctx: NodeContext,
        plan_slice: Mapping[str, Any],
        data_files: Sequence[str],
        target_columns: Sequence[str],
    ) -> _ReviewSpec:
        """清洗审稿口径：材料 = 准备方案 / 数据文件 / 脚本正文 / 影响面 / 复跑 / 清洗摘要。"""
        script_path = f"steps/{ctx.step_id}/main.py"

        def materials(
            capture: _SandboxCapture,
            final_answer: Mapping[str, Any],
            rerun: Mapping[str, Any],
            files: Sequence[str],
        ) -> dict[str, Any]:
            return {
                "preparation_plan": json.dumps(dict(plan_slice), ensure_ascii=False),
                "data_files": "\n".join(f"- {path}" for path in data_files),
                "cleaning_code": _clip_code(capture.code, script_path),
                "impact": json.dumps(
                    _cleaning_impact(capture.metrics, target_columns), ensure_ascii=False
                ),
                "rerun_report": rerun_material(rerun),
                "cleaning_summary": str(final_answer.get("summary") or "无"),
                "stdout_tail": capture.stdout[-_STDOUT_TAIL_CHARS:] or "无",
                "workspace_files": "\n".join(f"- {path}" for path in files) or "无",
            }

        def context_slice(
            capture: _SandboxCapture, rerun: Mapping[str, Any], round_no: int
        ) -> dict[str, Any]:
            return {
                "data_files": list(data_files),
                "impact": _cleaning_impact(capture.metrics, target_columns),
                "rerun_consistent": bool(rerun.get("executed") and rerun.get("consistent")),
                "round": round_no,
            }

        return _ReviewSpec(
            prompt_id=self.review_prompt_id,
            output_schema_id="cleaning-review.v1",
            goal="独立核查清洗脚本是否忠实于数据准备方案、清洗产物与影响面统计是否可信",
            progress_kind="cleaning_review",
            task_label="cleaning_review",
            toolset=REVIEWER_TOOL_NAMES,
            focus=CLEANING_REVIEW_FOCUS,
            materials=materials,
            context_slice=context_slice,
        )

    # -- G2 gate ----------------------------------------------------------------

    @staticmethod
    def _g2_review(
        cleaning: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """G2 数据闸门的两个触发源（任一即挂门，可叠加）：

        - 清洗真实执行且影响面超阈值（删行 >5% / 目标列被插补）；
        - 清洗脚本的生成者-评审者环**僵持**（v3.33 起）：独立审稿的阻断性意见到
          R2 预算 / 审稿轮数用尽仍未解决——不信清洗产物时推荐「改用原始数据」。
        """
        if not cleaning.get("executed") or cleaning.get("status") != "passed":
            return None
        triggers: list[str] = []
        ratio = float(cleaning.get("rows_deleted_ratio") or 0.0)
        if ratio > G2_ROW_DELETION_THRESHOLD:
            triggers.append(f"删除了 {ratio:.1%} 的数据行（阈值 5%）")
        imputed_targets = list(cleaning.get("imputed_target_columns") or [])
        if imputed_targets:
            triggers.append(f"目标列被插补（{'、'.join(imputed_targets)}）")
        review = cleaning.get("review") or {}
        stalemate = bool(review.get("executed") and review.get("stalemate"))
        unresolved = (
            [
                dict(entry)
                for entry in review.get("findings") or []
                if isinstance(entry, Mapping) and entry.get("severity") == "blocker"
            ]
            if stalemate
            else []
        )
        if not triggers and not stalemate:
            return None

        reasons: list[str] = []
        if triggers:
            reasons.append("数据清洗影响面较大：" + "；".join(triggers))
        if stalemate:
            reasons.append(
                f"清洗脚本的独立审稿 {int(review.get('rounds') or 0)} 轮后仍有 "
                f"{len(unresolved)} 条阻断性意见未解决（{review.get('reason') or '僵持'}）"
            )
        reason = "；".join(reasons) + "。请确认数据处理方式"
        recommended = "use_raw" if stalemate else "adopt_cleaned"
        options = []
        for option in G2_OPTIONS:
            entry = dict(option)
            entry.pop("recommended", None)
            if entry["id"] == recommended:
                entry["recommended"] = True
            options.append(entry)
        meta = {
            "gate": "G2",
            "decision_type": "generic",
            "title": reason,
            "options": options,
            "impact": {
                "rows_before": cleaning.get("rows_before"),
                "rows_after": cleaning.get("rows_after"),
                "rows_deleted_ratio": cleaning.get("rows_deleted_ratio"),
                "imputed_columns": cleaning.get("imputed_columns"),
                "imputed_target_columns": imputed_targets,
                # 审稿僵持：未解决的阻断性意见逐条进卡片
                "review_stalemate": stalemate,
                "reviewer_findings": unresolved,
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


# ── 生成者-评审者（§8.4）：三个沙盒消费方共用的审稿环 ────────────────────────────

#: 一次沙盒波（首波 / 按审稿意见的修复波）的产物：验收报告、证据捕获、终答。
_SandboxWaveResult = tuple[dict[str, Any], _SandboxCapture, dict[str, Any]]


def _union_artifacts(
    waves: Sequence[_SandboxCapture], adopted: _SandboxCapture | None
) -> list[Any]:
    """所有波的产物引用并集（按出现顺序、uri 去重），未被采用那些波的脚本除外。

    沙盒按「新建文件」捕获产物：修复波只重写同名文件时不会再报一次，首波产出的
    数据 / 结果文件引用要留下来；脚本（kind=code）只留最终**采用**那一波的（僵持
    时采用的可能不是最后一波）——其余版本在各波验收报告 ``final_code_artifact``
    里仍可追溯。
    """
    seen: set[str] = set()
    merged: list[Any] = []
    for capture in waves:
        for ref in capture.artifacts:
            if ref.uri in seen or (ref.kind == "code" and capture is not adopted):
                continue
            seen.add(ref.uri)
            merged.append(ref)
    return merged


@dataclass(frozen=True)
class _ReviewSpec:
    """某个沙盒消费方的审稿口径：角色卡 id、材料拼法、派发元数据。

    机件（复跑核对 / 派发 / 驳回修复 / 僵持）三处同一份，只有这里不同。
    """

    prompt_id: str
    output_schema_id: str
    goal: str
    progress_kind: str
    task_label: str
    toolset: tuple[str, ...]
    focus: str
    #: (capture, final_answer, rerun, workspace_files) → 角色卡模板变量
    materials: Callable[
        [_SandboxCapture, Mapping[str, Any], Mapping[str, Any], Sequence[str]],
        dict[str, Any],
    ]
    #: (capture, rerun, round_no) → SpawnSpec.context_slice（审计里看得到审的是什么）
    context_slice: Callable[[_SandboxCapture, Mapping[str, Any], int], dict[str, Any]]


def _rerun_check(
    ctx: NodeContext, services: NodeServices, capture: _SandboxCapture
) -> dict[str, Any]:
    """确定性复跑核对：同一份最终脚本再跑一次，指标逐键比对首跑。

    节点自己跑、自己比——「复跑核对」不交给模型想象。预算切片不够一次运行
    或脚本正文缺失时如实 ``executed=false``，审稿照常进行（材料里写明未复跑）。
    """
    governor = (services.extras or {}).get("budget_governor")
    budgets: RunBudget = (
        governor.subagent_slice() if governor is not None else RunBudget()
    )
    if budgets.max_sandbox_runs < 1:
        return {"executed": False, "reason": "剩余预算不足以复跑核对"}
    if not capture.code.strip():
        return {"executed": False, "reason": "沙盒未回传最终脚本正文，无法复跑"}
    result = services.tools.invoke(
        ctx.run_id, ctx.step_id, PYTHON_TOOL_NAME, {"code": capture.code}
    )
    if not result.ok:
        output = result.output or {}
        stderr = str(output.get("stderr") or "").strip()
        reason = f"复跑失败：{result.error or result.status}"
        if stderr:
            reason += "；stderr（尾部）：" + stderr[-500:]
        return {
            "executed": True,
            "consistent": False,
            "metrics": {},
            "diff": [],
            "reason": reason,
        }
    scratch = _SandboxCapture()
    scratch.observe(result)
    consistent, diff = compare_metrics(capture.metrics, scratch.metrics)
    return {
        "executed": True,
        "consistent": consistent,
        "metrics": dict(scratch.metrics),
        "diff": diff,
        "reason": "" if consistent else "复跑指标与首跑不一致",
    }


def _spawn_reviewer(
    ctx: NodeContext,
    services: NodeServices,
    supervisor: Any,
    registry: PromptRegistry,
    spec: _ReviewSpec,
    capture: _SandboxCapture,
    final_answer: Mapping[str, Any],
    rerun: Mapping[str, Any],
    round_no: int,
) -> tuple[dict[str, Any] | None, int, str]:
    """派发一次审稿子代理，返回 (归一化 verdict | None, 模型调用数, 未成功原因)。

    独立上下文 = 只拿 ``spec.materials`` 拼出的结构化切片，不继承生成者会话；
    tier readonly——运行部分已由节点做完。
    """
    governor = (services.extras or {}).get("budget_governor")
    budgets: RunBudget = (
        governor.subagent_slice() if governor is not None else RunBudget()
    )
    if budgets.max_llm_calls < 1:
        return None, 0, "剩余预算不足以派发审稿子代理"
    template = registry.get(spec.prompt_id)
    files = _workspace_files(ctx, services)
    variables = spec.materials(capture, final_answer, rerun, files)
    problems = validate(variables, template.input_schema)
    if problems:
        return None, 0, "审稿任务卡输入无效：" + "; ".join(problems)
    toolset = spec.toolset
    system_prompt = template.render(variables)
    llm_calls = {"count": 0}
    trace = {"error": ""}

    def runner(_spec: SpawnSpec) -> ResultEnvelope:
        outcome = run_inner_loop(
            LoopTask(
                task_id=f"{ctx.step_id}:{spec.task_label}:{round_no}",
                messages=(
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=reviewer_tool_brief(toolset, spec.focus)),
                ),
                validator=lambda value: validate(value, template.output_schema),
                parser=extract_json,
                budget=REVIEWER_LOOP_BUDGET,
            ),
            chat=text_protocol_chat(
                services.llm,
                label=spec.prompt_id,
                on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
            ),
            execute_tools=_bounded_tool_executor(
                ctx, services, toolset, REVIEWER_MAX_TOOL_ROUNDS
            ),
        )
        if not outcome.ok or outcome.value is None:
            trace["error"] = str(outcome.last_error or outcome.exit_reason or "审稿终答未通过校验")
            return ResultEnvelope(status="failed")
        return ResultEnvelope(status="done", output=dict(outcome.value))

    try:
        envelope = supervisor.spawn(
            SpawnSpec(
                kind="reviewer",
                goal=spec.goal,
                context_slice=spec.context_slice(capture, rerun, round_no),
                toolset=toolset,
                tool_tier="readonly",
                budgets=budgets,
                output_schema_id=spec.output_schema_id,
            ),
            runner,
            parent_tier="execute",
            output_validator=lambda output: validate(output, template.output_schema),
        )
    except AgentError as exc:
        # 装配 / 切片缺陷（E510 等）：审稿按未完成计，不炸消费方节点
        return None, llm_calls["count"], f"审稿子代理派发被拒（{exc.code.value}：{exc}）"
    if not envelope.ok or envelope.output is None:
        detail = envelope.status
        if envelope.error_code:
            detail += f"，{envelope.error_code}"
        if trace["error"]:
            detail += f"，{trace['error']}"
        return None, llm_calls["count"], f"审稿子代理未完成（{detail}）"
    return normalize_verdict(envelope.output), llm_calls["count"], ""


def _run_review_loop(
    ctx: NodeContext,
    services: NodeServices,
    supervisor: Any,
    registry: PromptRegistry,
    spec: _ReviewSpec,
    *,
    first: _SandboxWaveResult,
    sandbox_wave: Callable[[str, int], _SandboxWaveResult | None],
    max_runs: int,
    usage: dict[str, int],
) -> tuple[dict[str, Any], _SandboxWaveResult]:
    """复跑核对 → 独立审稿 → 一票驳回退 R2 修复 → 复审 → 僵持。

    返回 ``(review, 最终采用的那一波)``。``sandbox_wave(feedback, max_runs)`` 由消费方
    提供（实验节点直跑、清洗 / 稳健性经监督者派发），返回 None 表示修复波没派出去；
    ``usage["runs"|"waves"]`` 就地累加修复波的用量。僵持时保留最后一波**通过验收**的
    结果——审稿意见记进 ``review``，由闸门裁定，不因审稿把能跑的结果扔掉。
    """
    review: dict[str, Any] = {"executed": False, "reason": "", "llm_calls": 0}
    report, capture, final_answer = first
    rounds = 0
    while True:
        rounds += 1
        rerun = _rerun_check(ctx, services, capture)
        verdict, calls, error = _spawn_reviewer(
            ctx, services, supervisor, registry, spec, capture, final_answer, rerun, rounds
        )
        review["llm_calls"] += calls
        if verdict is None:
            if rounds == 1:
                review.update({"rounds": rounds, "rerun": rerun, "reason": error})
            else:
                # 修复波之后复审没完成：上一轮的阻断性意见没人证实已解决，
                # 按僵持处理（宁可多进一次闸门，不把未经复审的修复当通过）
                review.update({
                    "rounds": rounds,
                    "rerun": rerun,
                    "stalemate": True,
                    "reason": f"修复后复审未完成（{error}），阻断性意见未经证实解决",
                })
            break
        review.update({
            "executed": True,
            "rounds": rounds,
            "verdict": verdict["verdict"],
            "findings": verdict["findings"],
            "blockers": verdict["blockers"],
            "summary": verdict["summary"],
            "rerun": rerun,
            "stalemate": False,
            "reason": "",
        })
        _emit_progress(services, {
            "kind": spec.progress_kind,
            "round": rounds,
            "verdict": verdict["verdict"],
            "blockers": verdict["blockers"],
            "findings": len(verdict["findings"]),
            "rerun_consistent": bool(rerun.get("executed") and rerun.get("consistent")),
            "summary": verdict["summary"],
        })
        if verdict["verdict"] == "accept":
            break
        remaining_runs = max_runs - usage["runs"]
        if rounds >= REVIEW_MAX_ROUNDS:
            review.update({
                "stalemate": True,
                "reason": f"审稿 {REVIEW_MAX_ROUNDS} 轮后仍有阻断性意见未解决",
            })
            break
        if remaining_runs < 1:
            review.update({
                "stalemate": True,
                "reason": "R2 运行预算已尽，无法按审稿意见修复",
            })
            break
        repair = sandbox_wave(
            review_feedback(verdict["findings"], verdict["summary"], rerun),
            remaining_runs,
        )
        if repair is None:
            review.update({
                "stalemate": True,
                "reason": "按审稿意见的修复波未能派发，保留已验收结果",
            })
            break
        repair_report, repair_capture, repair_final = repair
        usage["runs"] += int(repair_report["usage"]["runs"])
        usage["waves"] += int(repair_report["attempts"])
        if repair_report["status"] != "passed":
            # 修复波没过验收：保留上一波已验收的代码与指标，未解决的意见随
            # stalemate 进闸门
            review.update({
                "stalemate": True,
                "reason": "按审稿意见的修复波未通过验收，保留已验收结果",
            })
            break
        report, capture, final_answer = repair_report, repair_capture, repair_final
    return review, (report, capture, final_answer)


class ExperimentExecutionNode(LlmSkillNode):
    """实验阶段 = 沙盒 Agent 执行体（H3 前置刀：迁移自单发生成+私有重试环）+ 独立审稿（§8.4）。

    模型在多轮会话里写码/跑码/读反馈直到验收断言通过或 R2 预算（6 次运行，
    §5.4）耗尽；验收以确定性断言为准（最后一次运行成功 + 指标标记行在场），
    模型自述完成无效。输出形状与迁移前一致（approach_summary/metrics/
    stdout_tail/experiment_summary/progress_note），下游（验证/论文）零改动；
    新增 sandbox_report（sandbox-run-report.v1 形状）供复现与评测。

    验收通过后进入**生成者-评审者**环：节点先用同一份脚本、同一种子确定性复跑
    一次核对指标，再经监督者派发 ``reviewer`` 子代理（独立上下文、只读工具）
    审稿；一票驳回（reject + ≥1 blocker）退回 R2 按意见修复后复审一次，仍驳回
    或修不动即「僵持」——保留已验收结果、把未解决的意见记进 ``outputs["review"]``
    交验证阶段的 G3 结果采用闸门裁定。监督者缺席时如实 ``review.executed=false``。
    """

    prompt_id = "experiment_code.sandbox"
    review_prompt_id = REVIEW_PROMPT_ID
    state = TaskState.EXPERIMENTING

    #: R2 运行预算（§4.7/§5.4 拍板值）：整个实验步骤最多 6 次沙箱运行。
    max_sandbox_runs = 6

    def __init__(
        self,
        registry: PromptRegistry,
        available_packages: str = DEFAULT_AVAILABLE_PACKAGES,
        hardware_note: str = DEFAULT_HARDWARE_NOTE,
        knowledge: KnowledgePort | None = None,
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
        # 知识库端口在场 = 两个只读知识工具已注册进 ToolBus（两个装配点一起做）：
        # 审稿人据此拿到 knowledge_search / knowledge_read；缺席时只有工作区只读工具。
        self.knowledge = knowledge

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
        plan = chosen_plan(planning, ctx.review_decisions)
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(plan, ensure_ascii=False),
            "data_preparation": (
                json.dumps(preparation, ensure_ascii=False) if preparation else "无"
            ),
            # 假设表进实验任务卡：实现须遵守全部适用假设，重点验证 / 待检验项的
            # 参数要做成可调常量，检验阶段才有扰动的把手（§9.1）。
            "model_assumptions": assumption_material(
                plan_assumptions(planning, plan.get("id"))
            ),
            # 符号表进实验任务卡：代码里的常量 / 变量 / 指标按方案记号命名与注释，
            # 论文引用同一套符号时才对得上（§9.1「同一符号贯穿」）。
            "model_symbols": symbol_material(plan_symbols(planning, plan.get("id"))),
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

        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        plan = chosen_plan(planning, ctx.review_decisions)
        system_prompt = template.render(variables)
        llm_calls = {"count": 0}
        chat = text_protocol_chat(
            services.llm,
            label=self.prompt_id,
            on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
        )
        env_fingerprint = _env_fingerprint(ctx, services)
        waves: list[_SandboxCapture] = []

        def sandbox_wave(brief_suffix: str | None, max_runs: int) -> _SandboxWaveResult:
            """一次独立装配的沙盒任务（首轮 / 按审稿意见修复）：每次自己的证据捕获。"""
            capture = _SandboxCapture()
            waves.append(capture)
            final_answer: dict[str, Any] = {}
            brief = tool_protocol_note(SANDBOX_TOOL_NAMES)
            if brief_suffix:
                brief += "\n\n" + brief_suffix
            task = SandboxTask(
                task_id=f"{ctx.step_id}:experiment",
                goal=(
                    f"实现并运行方案「{plan.get('name') or plan.get('id') or '选定方案'}」"
                    "的实验代码，产出真实指标与结果表"
                ),
                system_prompt=system_prompt,
                task_brief=brief,
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
                max_runs=max_runs,
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
            report = run_sandbox_task(
                task,
                chat=chat,
                execute_tools=_sandbox_tool_executor(ctx, services, capture),
                workspace_files=lambda: _workspace_files(ctx, services),
                read_text=_workspace_reader(ctx, services),
                env_fingerprint=env_fingerprint,
                publish_code=_publish_code_callback(ctx, services, capture, "experiment.py"),
                on_final_answer=final_answer.update,
            )
            return report, capture, final_answer

        report, capture, final_answer = sandbox_wave(None, self.max_sandbox_runs)
        usage = {
            "runs": int(report["usage"]["runs"]),
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
                metrics={
                    "llm_attempts": llm_calls["count"],
                    "code_rounds": usage["runs"],
                    "waves": usage["waves"],
                },
            )

        # ── 生成者-评审者（§8.4）：复跑核对 → 独立审稿 → 驳回退 R2 → 僵持进 G3 ──
        supervisor = (services.extras or {}).get("subagents")
        if supervisor is None:
            review: dict[str, Any] = {
                "executed": False,
                "reason": "未配置子代理监督者，跳过独立审稿",
                "llm_calls": 0,
            }
        else:
            review, (report, capture, final_answer) = _run_review_loop(
                ctx,
                services,
                supervisor,
                self._registry,
                self._review_spec(plan, planning),
                first=(report, capture, final_answer),
                sandbox_wave=sandbox_wave,
                max_runs=self.max_sandbox_runs,
                usage=usage,
            )

        node_metrics = {
            "llm_attempts": llm_calls["count"] + int(review["llm_calls"]),
            "code_rounds": usage["runs"],
            "waves": usage["waves"],
        }
        if review.get("rounds"):
            node_metrics["review_rounds"] = int(review["rounds"])
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
        if review.get("executed"):
            summary_bits.append(verdict_summary_text(review))
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
                # 生成者-评审者环的结论（§8.4）：复跑核对 + 独立审稿意见；僵持时
                # stalemate=true，验证阶段据此挂 G3
                "review": review,
            },
            metrics=node_metrics,
            # 所有波的产物并集：修复波重写同名结果文件时沙盒不再报新建，首波引用要留
            artifacts=tuple(_union_artifacts(waves, capture)),
        )

    # -- generator / reviewer loop (§8.4) ------------------------------------------

    def _reviewer_toolset(self) -> tuple[str, ...]:
        if self.knowledge is None:
            return REVIEWER_TOOL_NAMES
        return REVIEWER_TOOL_NAMES + REVIEWER_KNOWLEDGE_TOOL_NAMES

    def _review_spec(
        self, plan: Mapping[str, Any], planning: Mapping[str, Any]
    ) -> _ReviewSpec:
        """实验审稿口径：材料 = 方案 / 假设 / 符号 / 脚本正文 / 指标 / 复跑 / 实现摘要。

        独立上下文 = 只拿这些结构化切片，不继承生成者会话；tier readonly——运行
        部分已由节点做完。
        """
        assumptions = assumption_material(plan_assumptions(planning, plan.get("id")))
        symbols = symbol_material(plan_symbols(planning, plan.get("id")))

        def materials(
            capture: _SandboxCapture,
            final_answer: Mapping[str, Any],
            rerun: Mapping[str, Any],
            files: Sequence[str],
        ) -> dict[str, Any]:
            return {
                "chosen_plan": json.dumps(dict(plan), ensure_ascii=False),
                "model_assumptions": assumptions,
                "model_symbols": symbols,
                "experiment_code": _clip_code(capture.code),
                "metrics": json.dumps(dict(capture.metrics), ensure_ascii=False),
                "rerun_report": rerun_material(rerun),
                "approach_summary": str(final_answer.get("approach_summary") or "无"),
                "stdout_tail": capture.stdout[-_STDOUT_TAIL_CHARS:] or "无",
                "workspace_files": "\n".join(f"- {path}" for path in files) or "无",
            }

        def context_slice(
            capture: _SandboxCapture, rerun: Mapping[str, Any], round_no: int
        ) -> dict[str, Any]:
            return {
                "plan_id": plan.get("id"),
                "plan_name": plan.get("name"),
                "metrics": dict(capture.metrics),
                "rerun_consistent": bool(rerun.get("executed") and rerun.get("consistent")),
                "round": round_no,
            }

        return _ReviewSpec(
            prompt_id=self.review_prompt_id,
            output_schema_id="experiment-review.v1",
            goal="独立核查实验代码与结果能否作为后续检验与论文的依据",
            progress_kind="experiment_review",
            task_label="review",
            toolset=self._reviewer_toolset(),
            focus=EXPERIMENT_REVIEW_FOCUS,
            materials=materials,
            context_slice=context_slice,
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

#: 只在检验脚本自己的审稿环僵持时追加的选项（插在「重做实验」之前）：检查不可信
#: 不等于结论不稳健，重做检验比重做实验便宜得多；同样复用 ``redo:<STATE>`` 语义
#: （G4 已用 redo:PAPER_WRITING 重做提出闸门的阶段本身）。
G3_REDO_VALIDATING_OPTION = {
    "id": "redo:VALIDATING",
    "label": "重做检验",
    "description": "保留实验结果，丢弃本次稳健性检查，按审稿意见重新设计并复跑检验脚本",
}

#: 推荐项口径：未通过项占比不到一半 → 推荐「接受并记录局限」（结论主体成立，
#: 局限如实写进论文即可）；一半及以上 → 推荐「重做实验」。只是 CTA 预选，
#: 最终由人拍板。
G3_REDO_RECOMMEND_RATIO = 0.5

#: 检查项下限：只有一项不算稳健性检验——单项太容易挑一个必过的。
MIN_ROBUSTNESS_CHECKS = 2

#: 任务卡里实验脚本正文的上限（超长截断并标注；模型可 ws_read 全文）。
_EXPERIMENT_CODE_CARD_CHARS = 12_000


def _clip_code(code: str, path: str = EXPERIMENT_SCRIPT_PATH) -> str:
    """任务卡里的脚本正文：超长截断并指路（``path`` = 全文在工作区的位置）。"""
    if len(code) <= _EXPERIMENT_CODE_CARD_CHARS:
        return code
    return (
        code[:_EXPERIMENT_CODE_CARD_CHARS]
        + f"\n# …（脚本共 {len(code)} 字符，此处截断；完整内容请 ws_read {path}）"
    )


def _risk_points(
    plan: Mapping[str, Any],
    judgement: Mapping[str, Any],
    review: Mapping[str, Any] | None = None,
) -> str:
    """检验任务卡的风险点段：方案自报风险 + 评审判读的保留意见与风险 + 实验审稿意见。

    审稿人（§8.4 生成者-评审者）留下的意见排在最前：稳健性检查优先冲着独立审稿
    指出的疑点去；僵持未解决的阻断性意见尤其要有检查对应。
    """
    lines: list[str] = []
    if review and review.get("executed"):
        for entry in review.get("findings") or []:
            if not isinstance(entry, Mapping) or not str(entry.get("issue") or "").strip():
                continue
            status = "未解决" if review.get("stalemate") else "已记录"
            location = str(entry.get("location") or "").strip()
            lines.append(
                f"- 审稿意见（{entry.get('severity') or 'minor'}｜{status}）："
                + (f"{location}——" if location else "")
                + str(entry.get("issue")).strip()
            )
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


def _check_assumption_id(entry: Mapping[str, Any], known_ids: Collection[str]) -> str | None:
    """标记行里某项检查针对的假设 id；不是已知假设（含没填 / 写错）→ None。"""
    raw = entry.get("assumption_id")
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    return candidate if candidate in known_ids else None


def _normalize_checks(
    metrics: Mapping[str, Any], known_assumption_ids: Collection[str] = ()
) -> list[dict[str, Any]]:
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
                # 针对哪条模型假设（§9.1 假设表 → 稳健性检验）；通用检查为 None
                "assumption_id": _check_assumption_id(entry, known_assumption_ids),
            }
        )
    return checks


def _assumption_coverage(
    focus: Sequence[Mapping[str, Any]], checks: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """每条须检验的假设被哪些检查覆盖、结论如何（全部通过才算通过；没覆盖 None）。"""
    coverage: list[dict[str, Any]] = []
    for row in focus:
        matched = [check for check in checks if check.get("assumption_id") == row["id"]]
        coverage.append(
            {
                "id": row["id"],
                "text": row["text"],
                "status": row.get("status"),
                "impact": row.get("impact"),
                "plan_id": (
                    None if row.get("scope") in (None, GLOBAL_ASSUMPTION_SCOPE) else row.get("scope")
                ),
                "check_ids": [str(check["id"]) for check in matched],
                "passed": all(check.get("passed") for check in matched) if matched else None,
            }
        )
    return coverage


def _robustness_checks_check(evidence) -> tuple[bool, str]:
    """断言：标记行含 ≥2 项结构完整的检查（id/name/passed/value/threshold）。"""
    raw = evidence.metrics.get("checks") if evidence.metrics else None
    if not isinstance(raw, list) or not raw:
        return False, (
            "未捕获检验结果：脚本必须原样打印一行 "
            'OMM_METRICS_JSON: {"checks": [{"id": ..., "name": ..., "passed": true/false, '
            '"value": 数值, "threshold": 数值, "detail": ..., "assumption_id": 假设 id 或省略}, ...]}'
            "（独占一行）"
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


def _assumption_coverage_check(focus: Sequence[Mapping[str, Any]]):
    """断言工厂：有须检验的假设时，至少一项检查要通过 assumption_id 指向其中一条。

    只要求「至少一项」而不是「每条都覆盖」——题面给定的分布假设之类未必能用代码
    检验，逼着每条都覆盖只会催生凑数的检查；没覆盖到的在产出里如实列为
    uncovered_focus，论文局限性据此说明。focus 为空（旧运行 / 单次调用路径没有
    假设表）时断言恒过。
    """
    focus_ids = {row["id"] for row in focus}

    def check(evidence) -> tuple[bool, str]:
        if not focus_ids:
            return True, "方案阶段未标注须检验的假设"
        raw = evidence.metrics.get("checks") if evidence.metrics else None
        entries = [entry for entry in (raw or []) if isinstance(entry, Mapping)]
        hit = [
            _check_assumption_id(entry, focus_ids)
            for entry in entries
            if _check_assumption_id(entry, focus_ids)
        ]
        if not hit:
            listing = "；".join(
                f"{row['id']}（{_ASSUMPTION_STATUS_LABELS.get(str(row.get('status')), row.get('status'))}）"
                f"{row['text']}"
                for row in focus
            )
            return False, (
                "没有任何检查针对须检验的模型假设：请至少为其中一条设计检查，并在标记行"
                f'该项里填 "assumption_id"（可选值：{"、".join(sorted(focus_ids))}）。'
                f"须检验的假设：{listing}"
            )
        missing = [row["id"] for row in focus if row["id"] not in set(hit)]
        note = f"检查覆盖假设 {'、'.join(dict.fromkeys(hit))}"
        if missing:
            note += f"；未覆盖：{'、'.join(missing)}（将记入局限性）"
        return True, note

    return check


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
    + 原因），绝不假装跑过。检验验收通过后进生成者-评审者环（§8.4：复跑核对 +
    独立审稿，驳回退 R2 修复、僵持记进 ``robustness["review"]``）。G3 在检查真实
    执行、脚本跑通且至少一项未通过，或任一审稿环（实验 / 检验）僵持时触发（§9.1）：
    判定数字来自检验脚本的标记行，节点只做计数与比例。
    """

    prompt_id = "validating.default"
    sandbox_prompt_id = ROBUSTNESS_PROMPT_ID
    review_prompt_id = ROBUSTNESS_REVIEW_PROMPT_ID
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
        plan = chosen_plan(planning, ctx.review_decisions)
        return {
            "chosen_plan": json.dumps(plan, ensure_ascii=False),
            "experiment_summary": str(
                experiment.get("experiment_summary")
                or experiment.get("stdout_tail")
                or "无"
            ),
            "metrics": json.dumps(dict(experiment.get("metrics") or {}), ensure_ascii=False),
            # 判读要对每条须检验的假设给结论（结果页 validation.checks 直接展示）
            "model_assumptions": assumption_material(self._focus_assumptions(ctx)),
        }

    @staticmethod
    def _focus_assumptions(ctx: NodeContext) -> list[dict[str, Any]]:
        """选定方案下须专门检验的假设（重点验证在前）；没有假设表 → 空表。"""
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        plan = chosen_plan(planning, ctx.review_decisions)
        return assumptions_to_verify(plan_assumptions(planning, plan.get("id")))

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        base = super().run(ctx, services)
        if base.status != NodeResult.SUCCEEDED:
            return base

        robustness, produced = self._execute_checks(ctx, services, base.outputs)
        outputs = {**base.outputs, "robustness": robustness}
        artifacts = base.artifacts + tuple(produced)

        experiment = dict(ctx.prior_outputs.get(TaskState.EXPERIMENTING.value) or {})
        gate = self._g3_review(robustness, experiment.get("review"))
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
    ) -> tuple[dict[str, Any], list[Any]]:
        """检验沙盒（子代理）→ 验收通过后进生成者-评审者环；返回 (robustness, 产物引用)。"""
        def skipped(reason: str) -> tuple[dict[str, Any], list[Any]]:
            return {"executed": False, "reason": reason}, []

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
        risk_points = _risk_points(plan, judgement, experiment.get("review"))
        # 须检验的假设进任务卡：检查优先围绕重点验证 / 待检验假设设计，标记行用
        # assumption_id 回指；已知 id 集合用于归一化与覆盖统计。
        focus = self._focus_assumptions(ctx)
        focus_ids = [row["id"] for row in focus]
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
            "model_assumptions": assumption_material(focus),
            "data_files": "\n".join(f"- {path}" for path in data_files) or "无",
            "available_packages": self._available_packages,
        })

        total_runs = max(1, min(self.max_sandbox_runs, budgets.max_sandbox_runs))
        llm_calls = {"count": 0}
        chat = text_protocol_chat(
            services.llm,
            label=self.sandbox_prompt_id,
            on_call=lambda: llm_calls.__setitem__("count", llm_calls["count"] + 1),
        )
        fingerprint = _env_fingerprint(ctx, services)
        waves: list[_SandboxCapture] = []
        last_envelope: dict[str, Any] = {}

        def sandbox_wave(
            brief_suffix: str | None, max_runs: int
        ) -> _SandboxWaveResult | None:
            """一次经监督者派发的检验沙盒波（首波 / 按审稿意见修复）。"""
            capture = _SandboxCapture()
            final_answer: dict[str, Any] = {}
            brief = tool_protocol_note(SANDBOX_TOOL_NAMES)
            if brief_suffix:
                brief += "\n\n" + brief_suffix
            task = SandboxTask(
                task_id=f"{ctx.step_id}:robustness",
                goal="复跑实验逻辑，在受控扰动下检验结论的稳健性并逐项给出判定",
                system_prompt=system_prompt,
                task_brief=brief,
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
                    SandboxAssertion(
                        id="assumptions_covered",
                        description=(
                            "至少一项检查通过 assumption_id 指向须检验的模型假设"
                            + (f"（{'、'.join(focus_ids)}）" if focus_ids else "（本方案无须检验的假设）")
                        ),
                        check=_assumption_coverage_check(focus),
                    ),
                ),
                seeds=dict(SANDBOX_SEEDS),
                max_runs=max_runs,
            )
            executor = _sandbox_tool_executor(ctx, services, capture)

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
                        "assumptions_to_verify": focus,
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
            waves.append(capture)
            last_envelope["value"] = envelope
            if not envelope.ok or envelope.output is None:
                return None
            return envelope.output, capture, final_answer

        first = sandbox_wave(None, total_runs)
        if first is None:
            envelope = last_envelope["value"]
            return {
                "executed": False,
                "reason": f"检验子代理未完成（{envelope.status}"
                + (f"，{envelope.error_code}" if envelope.error_code else "")
                + "）；检验结论沿用评审判读",
            }, _union_artifacts(waves, None)

        report, capture, final_answer = first
        usage = {"runs": int(report["usage"]["runs"]), "waves": int(report["attempts"])}
        review: dict[str, Any] | None = None
        if str(report.get("status")) == "passed":
            # 生成者-评审者（§8.4）：复跑核对 → 独立审稿 → 驳回退 R2 → 僵持进 G3
            review, (report, capture, final_answer) = _run_review_loop(
                ctx,
                services,
                supervisor,
                self._registry,
                self._review_spec(ctx, plan, code, metrics, focus, focus_ids, risk_points),
                first=first,
                sandbox_wave=sandbox_wave,
                max_runs=total_runs,
                usage=usage,
            )

        status = str(report.get("status") or "failed")
        checks = _normalize_checks(capture.metrics, focus_ids) if status == "passed" else []
        failed = [check for check in checks if not check["passed"]]
        coverage = _assumption_coverage(focus, checks) if status == "passed" else []
        robustness = {
            "executed": True,
            "status": status,
            "attempts": usage["waves"],
            "llm_calls": llm_calls["count"] + int((review or {}).get("llm_calls") or 0),
            "summary": str(final_answer.get("summary") or ""),
            "checks": checks,
            "checks_total": len(checks),
            "checks_failed": len(failed),
            "failed_checks": failed,
            "summary_text": _robustness_summary_text(status, checks),
            # 假设表的检验覆盖（§9.1）：每条须检验的假设被哪些检查覆盖、结论如何；
            # 未覆盖的进论文局限性。复跑没成时两表为空，不假装覆盖过。
            "assumption_coverage": coverage,
            "uncovered_focus": [row["id"] for row in coverage if not row["check_ids"]],
            "final_code_artifact": str(report.get("final_code_artifact") or ""),
            "produced_artifacts": list(report.get("produced_artifacts") or []),
        }
        if review is not None:
            robustness["review"] = review
        return robustness, _union_artifacts(waves, capture)

    def _review_spec(
        self,
        ctx: NodeContext,
        plan: Mapping[str, Any],
        experiment_code: str,
        metrics: Mapping[str, Any],
        focus: Sequence[Mapping[str, Any]],
        focus_ids: Sequence[str],
        risk_points: str,
    ) -> _ReviewSpec:
        """稳健性审稿口径：材料 = 方案 / 须检验假设 / 实验脚本 / 检验脚本 / 检查结果 / 复跑。"""
        script_path = f"steps/{ctx.step_id}/main.py"
        assumptions = assumption_material(list(focus))

        def materials(
            capture: _SandboxCapture,
            final_answer: Mapping[str, Any],
            rerun: Mapping[str, Any],
            files: Sequence[str],
        ) -> dict[str, Any]:
            return {
                "chosen_plan": json.dumps(dict(plan), ensure_ascii=False),
                "model_assumptions": assumptions,
                "experiment_code": _clip_code(experiment_code),
                "metrics": json.dumps(dict(metrics), ensure_ascii=False),
                "checks_code": _clip_code(capture.code, script_path),
                "checks": json.dumps(
                    _normalize_checks(capture.metrics, focus_ids), ensure_ascii=False
                ),
                "rerun_report": rerun_material(rerun),
                "checks_summary": str(final_answer.get("summary") or "无"),
                "risk_points": risk_points or "无",
                "stdout_tail": capture.stdout[-_STDOUT_TAIL_CHARS:] or "无",
                "workspace_files": "\n".join(f"- {path}" for path in files) or "无",
            }

        def context_slice(
            capture: _SandboxCapture, rerun: Mapping[str, Any], round_no: int
        ) -> dict[str, Any]:
            checks = _normalize_checks(capture.metrics, focus_ids)
            return {
                "plan_id": plan.get("id"),
                "checks_total": len(checks),
                "checks_failed": sum(1 for check in checks if not check["passed"]),
                "rerun_consistent": bool(rerun.get("executed") and rerun.get("consistent")),
                "round": round_no,
            }

        return _ReviewSpec(
            prompt_id=self.review_prompt_id,
            output_schema_id="robustness-review.v1",
            goal="独立核查稳健性检验脚本是否真的检验了结论、判定是否可信",
            progress_kind="robustness_review",
            task_label="robustness_review",
            toolset=REVIEWER_TOOL_NAMES,
            focus=ROBUSTNESS_REVIEW_FOCUS,
            materials=materials,
            context_slice=context_slice,
        )

    # -- G3 gate ----------------------------------------------------------------

    @staticmethod
    def _g3_review(
        robustness: Mapping[str, Any],
        review: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """G3 结果采用闸门的三个触发源（任一即挂门，可叠加）：

        - 稳健性复跑真执行、脚本跑通且 ≥1 项检查未通过（v3.20 起）；
        - 实验阶段的生成者-评审者环**僵持**（§8.4「僵持到预算尽 → 上闸门」）：
          独立审稿的阻断性意见到 R2 预算 / 审稿轮数用尽仍未解决（v3.32 起）；
        - 检验脚本自己的审稿环僵持（v3.33 起）：检查本身不可信时多给一个
          ``redo:VALIDATING``「重做检验」选项（保留实验结果、只重做检验）并推荐它。
        选项 id 复用修订门的 ``redo:<STATE>`` 语义，引擎分支不变。
        """
        failed: list[dict[str, Any]] = []
        total = 0
        if robustness.get("executed") and robustness.get("status") == "passed":
            failed = [dict(check) for check in robustness.get("failed_checks") or []]
            total = int(robustness.get("checks_total") or 0)
            if total <= 0:
                failed = []
        stalemate = bool(review and review.get("executed") and review.get("stalemate"))
        unresolved = _unresolved_blockers(review) if stalemate else []
        checks_review = robustness.get("review") or {}
        checks_stalemate = bool(checks_review.get("executed") and checks_review.get("stalemate"))
        checks_unresolved = _unresolved_blockers(checks_review) if checks_stalemate else []
        if not failed and not stalemate and not checks_stalemate:
            return None

        reasons: list[str] = []
        if failed:
            names = "、".join(str(check.get("name") or check.get("id")) for check in failed)
            reasons.append(f"稳健性检查 {total} 项中 {len(failed)} 项未通过：{names}")
        if stalemate:
            reasons.append(
                f"独立审稿 {int((review or {}).get('rounds') or 0)} 轮后仍有 "
                f"{len(unresolved)} 条阻断性意见未解决（{(review or {}).get('reason') or '僵持'}）"
            )
        if checks_stalemate:
            reasons.append(
                f"检验脚本的独立审稿 {int(checks_review.get('rounds') or 0)} 轮后仍有 "
                f"{len(checks_unresolved)} 条阻断性意见未解决（{checks_review.get('reason') or '僵持'}）"
            )
        reason = "；".join(reasons) + "。请确认实验结果的处置方式"
        if stalemate or (total > 0 and len(failed) / total >= G3_REDO_RECOMMEND_RATIO):
            recommended = "redo:EXPERIMENTING"
        elif checks_stalemate:
            recommended = G3_REDO_VALIDATING_OPTION["id"]
        else:
            recommended = G3_ACCEPT_OPTION_ID
        options = []
        for option in G3_OPTIONS:
            if option["id"] == "redo:EXPERIMENTING" and checks_stalemate:
                options.append(dict(G3_REDO_VALIDATING_OPTION))
            options.append(dict(option))
        for entry in options:
            if entry["id"] == recommended:
                entry["recommended"] = True
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
                # 哪些须检验的假设被覆盖 / 未通过 / 没覆盖：拍板「接受并记录局限」
                # 时用户看得到局限落在哪条假设上
                "assumption_coverage": list(robustness.get("assumption_coverage") or []),
                # 审稿僵持：未解决的阻断性意见逐条进卡片（拍板时看得到审稿人指的是什么）
                "review_stalemate": stalemate,
                "reviewer_findings": unresolved,
                # 检验脚本自己的审稿僵持（检查不可信 ≠ 结论不稳健，分开列）
                "checks_review_stalemate": checks_stalemate,
                "checks_reviewer_findings": checks_unresolved,
            },
        }
        return reason, meta


def _unresolved_blockers(review: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """审稿环结论里的阻断性意见（僵持时即「未解决」）。"""
    return [
        dict(entry)
        for entry in (review or {}).get("findings") or []
        if isinstance(entry, Mapping) and entry.get("severity") == "blocker"
    ]


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
    "model_assumptions": "模型假设表（方案阶段确认；编号【状态｜影响｜适用范围】内容）",
    "model_symbols": "模型符号表（方案阶段确认；记号（类型｜范围）＝定义［单位；取值］）",
    "experiment_summary": "实验过程摘要",
    "validation_summary": "检验结论",
    "frozen_numbers": "数字冻结清单（正文数值只准引用此表与上述材料中的数字）",
}
#: 冻结清单不受总编的 source_keys 路由影响：每章都必须看到它（§9 硬规则）。
_ALWAYS_MATERIAL_KEYS = ("frozen_numbers",)
#: 叙述材料（审计允许集的文本来源；冻结清单本身按值进允许集）。两表也算：
#: 假设文本与符号取值里的数字（「删行 ≤ 5%」「{0,1}」）是有出处的。
_NARRATIVE_MATERIAL_KEYS = (
    "problem_analysis",
    "chosen_plan",
    "model_assumptions",
    "model_symbols",
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


def _experiment_material(experiment: Mapping[str, Any]) -> str:
    """论文的实验材料：节点的实验过程摘要 + 独立审稿僵持时未解决的阻断性意见。

    通过的审稿结论节点已写进 ``experiment_summary``（一行「独立审稿通过…」），这里
    不重复；僵持才追加——用户在 G3 选了接受，未解决的意见也必须进论文的局限性。
    """
    summary = str(experiment.get("experiment_summary") or "无")
    review = experiment.get("review")
    if isinstance(review, Mapping) and review.get("stalemate"):
        text = review_material(review, "实验代码")
        if text:
            summary = f"{summary}\n{text}"
    return summary


def _validation_material(
    validation: Mapping[str, Any], review_decisions: Mapping[str, str]
) -> str:
    """论文的检验材料：评审判读 + 沙盒复跑的稳健性结论 + 检验脚本审稿结论 + G3 决策台账。

    稳健性一句话由验证节点按标记行数字生成（不是模型转述）；检验脚本的独立审稿
    （§8.4）通过与僵持都写——检验章要说得出「检验代码本身经过核查」，僵持时未解决
    的意见逐条进局限性；用户在 G3 选了「接受并记录局限」时把这条纪律写进材料——
    未通过的检查项必须进论文的局限性，不允许因为用户点了接受就把它们淡化掉。
    """
    summary = str(validation.get("validation_summary") or "无")
    robustness = validation.get("robustness")
    if isinstance(robustness, Mapping) and robustness.get("executed"):
        text = str(robustness.get("summary_text") or "").strip()
        if text:
            summary = f"{summary}\n{text}"
        coverage_text = _assumption_coverage_text(robustness.get("assumption_coverage"))
        if coverage_text:
            summary = f"{summary}\n{coverage_text}"
        review_text = review_material(robustness.get("review"), "稳健性检验脚本")
        if review_text:
            summary = f"{summary}\n{review_text}"
    if review_decisions.get(TaskState.VALIDATING.value) == G3_ACCEPT_OPTION_ID:
        summary += (
            "\n用户已在结果采用闸门确认「接受并记录局限」：未通过的检查项必须在"
            "模型检验与局限性部分如实说明，不得淡化。"
        )
    return summary


def _assumption_coverage_text(coverage: Any) -> str:
    """假设检验覆盖 → 论文材料的一句话：逐条假设「通过 / 未通过 / 未被检验覆盖」。

    结论只按检验脚本标记行的 passed 汇总（不是模型转述）；未覆盖的假设点明须进
    局限性——方案阶段标了「重点验证」却没人验，论文不能当它成立。
    """
    if not isinstance(coverage, list) or not coverage:
        return ""
    parts: list[str] = []
    for row in coverage:
        if not isinstance(row, Mapping) or not row.get("id"):
            continue
        label = f"{row['id']}「{str(row.get('text') or '').strip()}」"
        check_ids = [str(check_id) for check_id in row.get("check_ids") or []]
        if not check_ids:
            parts.append(f"{label}未被检验覆盖，须在局限性中说明")
        elif row.get("passed"):
            parts.append(f"{label}通过（{'、'.join(check_ids)}）")
        else:
            parts.append(f"{label}未通过（{'、'.join(check_ids)}）")
    if not parts:
        return ""
    return "模型假设检验：" + "；".join(parts) + "。"


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
        plan = chosen_plan(planning, ctx.review_decisions)
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(plan, ensure_ascii=False),
            # 方案阶段的两张表进论文材料：「模型假设」章按表逐条列、「符号说明」与
            # 全文记号以符号表为底稿（§9.1「同一符号贯穿」）；缺表如实写「无」。
            "model_assumptions": assumption_material(
                plan_assumptions(planning, plan.get("id"))
            ),
            "model_symbols": symbol_material(plan_symbols(planning, plan.get("id"))),
            "experiment_summary": _experiment_material(experiment),
            "validation_summary": _validation_material(validation, ctx.review_decisions),
            "frozen_numbers": render_frozen_numbers(build_frozen_numbers(ctx.prior_outputs)),
        }

    @staticmethod
    def _plan_symbol_rows(ctx: NodeContext) -> list[dict[str, Any]]:
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        plan = chosen_plan(planning, ctx.review_decisions)
        return plan_symbols(planning, plan.get("id"))

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
        # 方案符号表是全文记号的底稿：总编漏列的记号确定性补进符号约定（不烧调用），
        # 只记警告；续写路径拿到的是原始骨架，这里同样过一遍（幂等）。
        notation, filled_symbols = complete_notation(
            str(outline.get("notation") or ""), self._plan_symbol_rows(ctx)
        )
        warnings: list[str] = []
        if filled_symbols:
            warnings.append(
                f"总编符号约定漏列 {len(filled_symbols)} 个方案符号"
                f"（{'、'.join(row['symbol'] for row in filled_symbols)}），已按方案符号表补齐"
            )
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
        if filled_symbols:
            metrics_payload["notation_filled"] = len(filled_symbols)
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
