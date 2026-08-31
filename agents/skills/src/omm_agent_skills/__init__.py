"""建模技能实现。

每个技能声明触发条件、输入输出 Schema、依赖、预算与验证器，由内核按题型和风险路由，
不在 Prompt 里隐式约定。
"""

from .llm import LlmCall, ScriptedLlmPort, StubLlmPort, stub_response
from .nodes import (
    DEFAULT_AVAILABLE_PACKAGES,
    DEFAULT_HARDWARE_NOTE,
    PYTHON_TOOL_NAME,
    DataPreparationNode,
    ExperimentExecutionNode,
    LlmSkillNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    ValidationNode,
    chosen_plan,
    extract_json,
    gpu_hardware_note,
    render_paper_markdown,
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
    "DEFAULT_AVAILABLE_PACKAGES",
    "DEFAULT_HARDWARE_NOTE",
    "DEFAULT_PROMPTS_DIR",
    "PYTHON_TOOL_NAME",
    "DataPreparationNode",
    "ExperimentExecutionNode",
    "LlmCall",
    "LlmSkillNode",
    "ModelPlanningNode",
    "PaperWritingNode",
    "ProblemAnalysisNode",
    "PromptFormatError",
    "PromptRegistry",
    "PromptRenderError",
    "PromptTemplate",
    "ScriptedLlmPort",
    "StubLlmPort",
    "ValidationNode",
    "chosen_plan",
    "extract_json",
    "gpu_hardware_note",
    "load_default_registry",
    "parse_prompt_text",
    "render_paper_markdown",
    "stub_response",
    "validate",
]
