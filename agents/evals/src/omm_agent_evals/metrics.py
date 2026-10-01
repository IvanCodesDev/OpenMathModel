"""E6 看板聚合器的评测侧入口：实现在 ``omm_agent_harness.metrics``（控制面与评测共用一份口径）。"""

from omm_agent_harness.metrics import (
    REPORT_VERSION,
    SANDBOX_TOOLS,
    aggregate_batch,
    aggregate_run,
    compare_reports,
    render_batch_markdown,
    render_markdown,
)

__all__ = [
    "REPORT_VERSION",
    "SANDBOX_TOOLS",
    "aggregate_batch",
    "aggregate_run",
    "compare_reports",
    "render_batch_markdown",
    "render_markdown",
]
