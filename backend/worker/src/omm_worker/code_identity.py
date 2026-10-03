"""worker 进程的执行代码身份：实现在 ``omm_agent_harness.code_identity``（API 同用一份），这里定
worker 的口径与进程级单例，并给引擎 sink 包一层盖章。

worker 执行面同样是长驻进程（``WorkerLoop.run_forever``），不会重新加载代码：改了智能体代码却没把
worker 整个重启，推进用的就一直是启动时的旧代码。与 API 在落库时盖章同一口径——每开一个步骤，
STEP_STARTED 落进 JSONL 事件日志前盖上 ``executor``，E6 看板从事件里读回。评测会话（固定时钟与
顺序 id，事件日志要逐字节可复现）由 ``WorkerRuntime(stamp_executor=False)`` 关掉。
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from typing import Any

from omm_agent_core import AgentEvent, EventSink, EventType
from omm_agent_harness.code_identity import ProcessCode, SourceRoot
from omm_agent_skills.prompt_registry import DEFAULT_PROMPTS_DIR

logger = logging.getLogger(__name__)

#: worker 进程执行的源码包（import 名）；提示词模板目录另加。执行面不导入控制面，所以没有
#: omm_api / omm_contracts。
SOURCE_PACKAGES: tuple[str, ...] = (
    "omm_worker",
    "omm_agent_core",
    "omm_agent_harness",
    "omm_agent_skills",
    "omm_agent_tools",
)


def new_process_code(roots: Sequence[SourceRoot] | None = None, **options: Any) -> ProcessCode:
    """按 worker 口径造一个进程代码身份；``roots`` / ``with_git`` / ``clock`` 供测试替换。"""
    return ProcessCode(
        roots,
        packages=SOURCE_PACKAGES,
        prompts_dir=DEFAULT_PROMPTS_DIR,
        process_label="worker 进程",
        restart_hint="把 worker 进程整个停掉再起（WorkerLoop 不会重新加载代码）。",
        **options,
    )


_PROCESS = new_process_code()


def process_code() -> ProcessCode:
    """本进程的代码身份（模块级单例；测试可替换 ``_PROCESS``）。"""
    return _PROCESS


def executor_stamp(*, run_id: str | None = None, stage: str | None = None) -> dict[str, Any]:
    return process_code().stamp(run_id=run_id, stage=stage)


class StampingSink:
    """引擎 sink 的盖章层：STEP_STARTED 落盘前盖上执行进程的代码身份，其余事件原样转交。

    只盖落盘的那份：引擎内存里的事件不动（reducer 只读 state / step_id / attempt，重放时多出的键
    不碍事）；盖章出错只缺章、照常落盘，不拦步骤。
    """

    def __init__(self, inner: EventSink) -> None:
        self._inner = inner

    def emit(self, event: AgentEvent) -> None:
        if event.event_type is EventType.STEP_STARTED:
            event = _stamped(event)
        self._inner.emit(event)


def _stamped(event: AgentEvent) -> AgentEvent:
    try:
        stamp = executor_stamp(run_id=event.run_id, stage=str(event.payload.get("state") or ""))
    except Exception:  # 身份缺项不许拦住步骤
        logger.exception("run %s: 执行代码身份盖章失败", event.run_id)
        return event
    if not stamp:
        return event
    return dataclasses.replace(event, payload={**event.payload, "executor": stamp})


__all__ = (
    "SOURCE_PACKAGES",
    "StampingSink",
    "executor_stamp",
    "new_process_code",
    "process_code",
)
