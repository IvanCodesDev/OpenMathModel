"""Agent 评测。

评测集与生产索引隔离存放，避免被抓取或被 Prompt 记忆污染；评分规则和基线随 Skill
版本一起记录，便于版本回滚决策。

场景目录：

- ``scenario``：worker 全栈金样（队列→租约→引擎→真实沙箱），LLM 打桩，
  DATA_PREPARATION/EXPERIMENTING/VALIDATING/PAPER_WRITING 为场景内简化节点。
- ``full_chain``：六阶段真实 LLM 节点全链（agents/skills 全部节点），引擎直驱，
  LLM 与 python_run 工具打桩，覆盖审批门、实验修复轮、审批拒绝重试与失败恢复。
"""

from .full_chain import (
    CANNED_EXPERIMENT,
    CANNED_EXPERIMENT_CODE,
    CANNED_PAPER,
    CANNED_PAPER_FINALIZE,
    CANNED_PAPER_OUTLINE,
    CANNED_PREPARATION,
    CANNED_ROBUSTNESS,
    CANNED_VALIDATION,
    CANNED_VALIDATION_CODE,
    FULL_CHAIN_CHAT_SEQUENCE,
    FULL_CHAIN_ENV,
    FULL_CHAIN_GOLDEN_EVENT_TYPES,
    FULL_CHAIN_METRICS,
    FULL_CHAIN_PROMPT_SEQUENCE,
    FULL_CHAIN_RESULTS_CSV,
    FULL_CHAIN_ROBUSTNESS_CHECKS,
    VALIDATION_CODE_MARKER,
    FakeToolInvoker,
    FullChainSession,
    ScriptedRun,
    build_full_chain_llm,
    build_full_chain_session,
    canned_paper_section,
    canned_sandbox_agent,
    robustness_success,
    sandbox_failure,
    sandbox_success,
)
from .scenario import (
    CANNED_ANALYSIS,
    CANNED_PLANNING,
    EXPERIMENT_CODE,
    GOLDEN_EVENT_TYPES,
    PROBLEM_STATEMENT,
    build_llm,
    build_runtime,
)

__all__ = [
    "CANNED_ANALYSIS",
    "CANNED_EXPERIMENT",
    "CANNED_EXPERIMENT_CODE",
    "CANNED_PAPER",
    "CANNED_PAPER_FINALIZE",
    "CANNED_PAPER_OUTLINE",
    "CANNED_PLANNING",
    "CANNED_PREPARATION",
    "CANNED_ROBUSTNESS",
    "CANNED_VALIDATION",
    "CANNED_VALIDATION_CODE",
    "EXPERIMENT_CODE",
    "FULL_CHAIN_CHAT_SEQUENCE",
    "FULL_CHAIN_ENV",
    "FULL_CHAIN_GOLDEN_EVENT_TYPES",
    "FULL_CHAIN_METRICS",
    "FULL_CHAIN_PROMPT_SEQUENCE",
    "FULL_CHAIN_RESULTS_CSV",
    "FULL_CHAIN_ROBUSTNESS_CHECKS",
    "GOLDEN_EVENT_TYPES",
    "PROBLEM_STATEMENT",
    "VALIDATION_CODE_MARKER",
    "FakeToolInvoker",
    "FullChainSession",
    "ScriptedRun",
    "build_full_chain_llm",
    "build_full_chain_session",
    "build_llm",
    "build_runtime",
    "canned_paper_section",
    "canned_sandbox_agent",
    "robustness_success",
    "sandbox_failure",
    "sandbox_success",
]
