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
from typing import Any

from omm_agent_core import NodeContext, NodeResult, NodeServices, TaskState

from .prompt_registry import PromptRegistry, PromptTemplate
from .schema import validate

_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$", re.MULTILINE)


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
