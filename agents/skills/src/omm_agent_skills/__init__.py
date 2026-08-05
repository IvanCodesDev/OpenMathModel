"""建模技能实现。

每个技能声明触发条件、输入输出 Schema、依赖、预算与验证器，由内核按题型和风险路由，
不在 Prompt 里隐式约定。
"""

from .llm import LlmCall, ScriptedLlmPort, StubLlmPort, stub_response
from .nodes import (
    LlmSkillNode,
    ModelPlanningNode,
    ProblemAnalysisNode,
    extract_json,
)
from .prompt_registry import (
    DEFAULT_PROMPTS_DIR,
    PromptFormatError,
    PromptRegistry,
    PromptRenderError,
    PromptTemplate,
    load_default_registry,
    parse_prompt_text,
)
from .schema import validate

__all__ = [
    "DEFAULT_PROMPTS_DIR",
    "LlmCall",
    "LlmSkillNode",
    "ModelPlanningNode",
    "ProblemAnalysisNode",
    "PromptFormatError",
    "PromptRegistry",
    "PromptRenderError",
    "PromptTemplate",
    "ScriptedLlmPort",
    "StubLlmPort",
    "extract_json",
    "load_default_registry",
    "parse_prompt_text",
    "stub_response",
    "validate",
]
