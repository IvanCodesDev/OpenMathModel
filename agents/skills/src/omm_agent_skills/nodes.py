"""LLM-backed skill nodes for the task state machine.

Model output is untrusted data: every response is parsed and validated
against the prompt's output schema before it may become step outputs. On a
violation the node makes exactly ONE repair attempt (feeding the error back),
then fails the step — silent acceptance of malformed structure is how bad
plans reach experiments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from omm_agent_core import NodeContext, NodeResult, NodeServices, TaskState

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
        error: str | None = None
        payload = dict(variables)
        for attempt in (1, 2):  # one normal try + one repair try
            raw = services.llm.complete(template.id, payload)
            try:
                parsed = extract_json(raw)
            except json.JSONDecodeError as exc:
                error = f"not valid JSON: {exc}"
            else:
                problems = validate(parsed, template.output_schema)
                if not problems:
                    return parsed, attempt, None
                error = "; ".join(problems)
            # Feed the failure back for the single repair attempt.
            payload = dict(variables)
            payload["__repair_error"] = error
            payload["__previous_output"] = raw[:2000]
        return None, 2, error


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


class PaperWritingNode(LlmSkillNode):
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
        result = super().run(ctx, services)
        if result.status != NodeResult.SUCCEEDED:
            return result
        if services.artifacts is None:
            return NodeResult.failed("artifact 存储端口未装配，无法发布论文草稿")
        markdown = render_paper_markdown(result.outputs)
        ref = services.artifacts.put(
            ctx.run_id,
            "paper",
            "paper-draft.md",
            markdown.encode("utf-8"),
            "text/markdown",
            ctx.step_id,
        )
        return NodeResult.succeeded(
            outputs=result.outputs, metrics=result.metrics, artifacts=(ref,)
        )
