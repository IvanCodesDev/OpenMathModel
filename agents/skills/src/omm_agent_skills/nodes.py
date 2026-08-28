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
from typing import Any

from omm_agent_core import NodeContext, NodeResult, NodeServices, TaskState
from omm_agent_harness import (
    LoopBudget,
    LoopTask,
    Message,
    Reply,
    Usage,
    run_inner_loop,
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

#: Marker line experiment scripts must print for structured metrics capture.
#: Anchored to a full line so prose that merely mentions the marker (or a
#: brace later on the same line) cannot produce a bogus capture.
_METRICS_LINE = re.compile(r"^OMM_METRICS_JSON:\s*(\{.*\})\s*$", re.MULTILINE)

_STDOUT_TAIL_CHARS = 2000
_ERROR_FEEDBACK_LIMIT = 3000


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
        return {
            "problem_analysis": json.dumps(analysis, ensure_ascii=False),
            "data_profile": str(
                ctx.prior_outputs.get(TaskState.DATA_PREPARATION.value, {}).get(
                    "profile_summary", "无数据画像"
                )
            ),
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


class DataPreparationNode(LlmSkillNode):
    prompt_id = "data_preparation.default"
    state = TaskState.DATA_PREPARATION

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "attachments_summary": str(ctx.inputs.get("attachments_summary") or "无"),
        }


class ExperimentExecutionNode(LlmSkillNode):
    """Generate experiment code with the LLM, run it in the python sandbox.

    Two loops with distinct purposes:
    - the inherited ``_complete_validated`` repairs STRUCTURALLY invalid model
      output (bad JSON / schema violations), one repair try;
    - this node's round loop repairs code that failed AT RUNTIME: the sandbox
      error is fed back and the code is regenerated once. Rounds are bounded —
      an endlessly self-repairing experiment burns budget without evidence.
    """

    prompt_id = "experiment_code.default"
    state = TaskState.EXPERIMENTING

    #: First attempt + one regeneration with runtime error feedback.
    max_code_rounds = 2

    def __init__(
        self,
        registry: PromptRegistry,
        available_packages: str = DEFAULT_AVAILABLE_PACKAGES,
    ) -> None:
        super().__init__(registry)
        # Which third-party packages the sandbox interpreter really offers:
        # the prompt whitelists imports against this, so code quality scales
        # with the environment instead of being pinned to stdlib-only.
        self._available_packages = available_packages

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        analysis = _require_outputs(ctx, TaskState.PROBLEM_ANALYSIS)
        planning = _require_outputs(ctx, TaskState.MODEL_PLANNING)
        preparation = ctx.prior_outputs.get(TaskState.DATA_PREPARATION.value) or {}
        return {
            "problem_analysis": json.dumps(dict(analysis), ensure_ascii=False),
            "chosen_plan": json.dumps(chosen_plan(planning), ensure_ascii=False),
            "data_preparation": (
                json.dumps(dict(preparation), ensure_ascii=False) if preparation else "无"
            ),
            "available_packages": self._available_packages,
            "error_feedback": "无",
            "previous_code": "无",
        }

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        if services.llm is None:
            return NodeResult.failed("no LLM port configured for this run")
        if services.tools is None:
            return NodeResult.failed("no tool invoker configured for this run")
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

        llm_attempts = 0
        last_error = "experiment did not run"
        for round_no in range(1, self.max_code_rounds + 1):
            parsed, attempts, error = self._complete_validated(template, variables, services)
            llm_attempts += attempts
            if parsed is None:
                return NodeResult.failed(
                    f"model output failed validation after {attempts} attempts: {error}"
                )
            code = str(parsed.get("code") or "")
            result = services.tools.invoke(
                ctx.run_id, ctx.step_id, PYTHON_TOOL_NAME, {"code": code}
            )
            if result.ok:
                return self._to_success(
                    parsed, result, llm_attempts, round_no, ctx, services
                )
            last_error = self._failure_feedback(result)
            variables = dict(variables)
            variables["error_feedback"] = last_error[:_ERROR_FEEDBACK_LIMIT]
            variables["previous_code"] = code
        return NodeResult.failed(
            f"experiment code failed after {self.max_code_rounds} rounds: {last_error}",
            metrics={"llm_attempts": llm_attempts, "code_rounds": self.max_code_rounds},
        )

    @staticmethod
    def _failure_feedback(result: Any) -> str:
        output = result.output or {}
        parts = [str(result.error or result.status)]
        stderr = str(output.get("stderr") or "").strip()
        stdout = str(output.get("stdout") or "").strip()
        if stderr:
            parts.append("stderr:\n" + stderr[-1500:])
        elif stdout:
            parts.append("stdout（尾部）:\n" + stdout[-800:])
        return "\n".join(parts)

    @staticmethod
    def _extract_metrics(stdout: str) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for match in _METRICS_LINE.finditer(stdout):
            try:
                candidate = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                metrics = candidate  # keep the LAST marker line the script printed
        return metrics

    def _to_success(
        self,
        parsed: dict[str, Any],
        result: Any,
        llm_attempts: int,
        code_rounds: int,
        ctx: NodeContext,
        services: NodeServices,
    ) -> NodeResult:
        output = result.output or {}
        stdout = str(output.get("stdout") or "")
        metrics = self._extract_metrics(stdout)
        approach = str(parsed.get("approach_summary") or "")

        summary_bits = [approach]
        if metrics:
            summary_bits.append("核心指标：" + json.dumps(metrics, ensure_ascii=False))
        if result.artifacts:
            names = [ref.uri.rstrip("/").rsplit("/", 1)[-1] for ref in result.artifacts]
            summary_bits.append("产物文件：" + "、".join(names))

        artifacts = list(result.artifacts)
        code = str(parsed.get("code") or "")
        if services.artifacts is not None and code:
            # The sandbox only captures files CREATED by the run; the script
            # itself is written before its snapshot and would otherwise be
            # lost. Publishing it makes the experiment reproducible from the
            # artifact list alone.
            artifacts.append(
                services.artifacts.put(
                    ctx.run_id,
                    "code",
                    "experiment.py",
                    code.encode("utf-8"),
                    "text/x-python",
                    ctx.step_id,
                )
            )

        return NodeResult.succeeded(
            outputs={
                "approach_summary": approach,
                "metrics": metrics,
                "stdout_tail": stdout[-_STDOUT_TAIL_CHARS:],
                "experiment_summary": "\n".join(bit for bit in summary_bits if bit),
                # 面向用户的进度叙述（提示词 v3 的 progress_note）：执行轨迹的正文段
                "progress_note": str(parsed.get("progress_note") or ""),
            },
            metrics={"llm_attempts": llm_attempts, "code_rounds": code_rounds},
            artifacts=tuple(artifacts),
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
