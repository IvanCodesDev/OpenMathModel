"""API 进程的执行代码身份：实现在 ``omm_agent_harness.code_identity``（worker 执行面同用一份），
这里只定 API 的口径——快照哪些包、旧代码告警怎么说——与进程级单例。

``npm run dev`` 起的 API 不带热重载（长任务经不起重载打断），``tools/dev-local.mjs`` 发现端口上
已有健康的 API 还会直接复用：改了智能体代码却没把 API 整个重启，真跑用的就一直是启动时的旧代码
（2026-10-03 排查：e898 跑在 v3.60 之前起的进程上，看上去像修复没生效）。快照由 lifespan 在启动时
先拍；每开一个步骤，``executor_stamp()`` 随 STEP_STARTED 领域事件落库（``engine_glue``）；
``/api/system`` 的 ``code`` 摘要供 dev-local 复用已运行的 API 时判断。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omm_agent_harness.code_identity import (
    STALE_CHECK_INTERVAL_S,
    ProcessCode,
    SourceRoot,
    logger,
)
from omm_agent_skills.prompt_registry import DEFAULT_PROMPTS_DIR

#: API 进程执行的源码包（import 名）；提示词模板目录另加。
SOURCE_PACKAGES: tuple[str, ...] = (
    "omm_api",
    "omm_contracts",
    "omm_agent_core",
    "omm_agent_harness",
    "omm_agent_skills",
    "omm_agent_tools",
)


def new_process_code(roots: Sequence[SourceRoot] | None = None, **options: Any) -> ProcessCode:
    """按 API 口径造一个进程代码身份；``roots`` / ``with_git`` / ``clock`` 供测试替换。"""
    return ProcessCode(
        roots,
        packages=SOURCE_PACKAGES,
        prompts_dir=DEFAULT_PROMPTS_DIR,
        process_label="API 进程",
        restart_hint=(
            "把 API 整个停掉再起（npm run dev 先 Ctrl+C，确认 8000 端口的 Python 进程已退出）。"
        ),
        **options,
    )


_PROCESS = new_process_code()


def process_code() -> ProcessCode:
    """本进程的代码身份（模块级单例；测试可替换 ``_PROCESS``）。"""
    return _PROCESS


def executor_stamp(*, run_id: str | None = None, stage: str | None = None) -> dict[str, Any]:
    return process_code().stamp(run_id=run_id, stage=stage)


__all__ = (
    "SOURCE_PACKAGES",
    "STALE_CHECK_INTERVAL_S",
    "ProcessCode",
    "SourceRoot",
    "executor_stamp",
    "logger",
    "new_process_code",
    "process_code",
)
