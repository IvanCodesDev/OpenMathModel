"""长任务执行器。

只做持久执行的编排与副作用落地，不承载领域规则；领域语义由 omm-agent-core 决定，
本包把内核产生的命令映射为队列 Job、租约、事件追加与重试（MVP 为文件后端，后续
切换 Postgres/Redis 时保持同一端口面）。
"""

from .event_store import JsonlEventStore
from .lease import Lease, RunLeaseStore
from .queue import FileJobQueue, JobEnvelope
from .runtime import WorkerConfig, WorkerLoop, WorkerRuntime, default_config

__all__ = [
    "FileJobQueue",
    "JobEnvelope",
    "JsonlEventStore",
    "Lease",
    "RunLeaseStore",
    "WorkerConfig",
    "WorkerLoop",
    "WorkerRuntime",
    "default_config",
]
