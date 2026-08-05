"""Agent 评测。

评测集与生产索引隔离存放，避免被抓取或被 Prompt 记忆污染；评分规则和基线随 Skill
版本一起记录，便于版本回滚决策。
"""

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
    "CANNED_PLANNING",
    "EXPERIMENT_CODE",
    "GOLDEN_EVENT_TYPES",
    "PROBLEM_STATEMENT",
    "build_llm",
    "build_runtime",
]
