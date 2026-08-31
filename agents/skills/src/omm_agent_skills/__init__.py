"""建模技能实现。

每个技能声明触发条件、输入输出 Schema、依赖、预算与验证器，由内核按题型和风险路由，
不在 Prompt 里隐式约定。
"""

from .chat_adapter import supports_chat, text_protocol_chat, tool_protocol_note
from .llm import ChatCall, LlmCall, ScriptedLlmPort, StubLlmPort, stub_response
from .nodes import (
    CLEANING_PROMPT_ID,
    DEFAULT_AVAILABLE_PACKAGES,
    DEFAULT_HARDWARE_NOTE,
    G2_ROW_DELETION_THRESHOLD,
    PYTHON_TOOL_NAME,
    SANDBOX_TOOL_NAMES,
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
    "CLEANING_PROMPT_ID",
    "DEFAULT_AVAILABLE_PACKAGES",
    "DEFAULT_HARDWARE_NOTE",
    "DEFAULT_PROMPTS_DIR",
    "G2_ROW_DELETION_THRESHOLD",
    "PYTHON_TOOL_NAME",
    "SANDBOX_TOOL_NAMES",
    "ChatCall",
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
    "supports_chat",
    "text_protocol_chat",
    "tool_protocol_note",
    "validate",
]
