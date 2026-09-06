"""Worker runtime: recover → heal → advance, under a run lease.

Execution-plane rules implemented here (PROJECT_STRUCTURE / system-overview):

- every advance happens under the run's lease (cross-process exclusion);
- state is recovered by replaying the durable event log, never trusted from
  memory;
- dangling RUNNING steps from a dead executor are failed ("healed") before
  new work starts, so retries are explicit attempts, not silent overwrites;
- events/artifacts are persisted before state moves (engine + JSONL sink);
- the job loop is budgeted — a runaway registry cannot spin forever;
- tools are minimally granted: the per-run invoker allowlists python_run only
  and caps the caller tier at "execute" (isomorphic to the API-side glue);
- scheduling follows the ``OMM_GRAPH`` profile (§4.9): ``shadow`` (default)
  keeps the linear engine driving with the Graph v1 scheduler as a shadow,
  ``linear-v1`` lets the graph drive, ``off`` disables the shadow. Divergences
  are logged and kept on ``shadow_divergences``; they never alter a run.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omm_agent_core import (
    GRAPH_MODE_ENV,
    Clock,
    EventType,
    IdGenerator,
    LlmPort,
    NodeRegistry,
    NodeServices,
    SchedulingDivergence,
    SystemClock,
    TaskRunEngine,
    TaskRunSnapshot,
    UuidIdGenerator,
    replay_events,
    resolve_graph_mode,
    schedulers_for_mode,
)
from omm_agent_harness import SubagentSupervisor
from omm_agent_tools import (
    KNOWLEDGE_READ_TOOL,
    KNOWLEDGE_SEARCH_TOOL,
    PythonSandbox,
    RecordingInvoker,
    TaskWorkspace,
    ToolRegistry,
    WorkspaceArtifactStore,
    knowledge_tool_specs,
    load_knowledge_library,
    sandbox_workspace_specs,
    table_profile_spec,
)

from .event_store import JsonlEventStore
from .lease import RunLeaseStore
from .queue import FileJobQueue, JobEnvelope

logger = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    """Filesystem layout + budgets. ``root`` defaults under the gitignored
    ``runs/`` tree so no run product can ever reach git."""

    root: Path
    lease_ttl_s: float = 120.0
    claim_ttl_s: float = 300.0
    max_deliveries: int = 3
    step_budget_per_job: int = 32
    python_timeout_s: float = 60.0

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def leases_dir(self) -> Path:
        return self.root / "leases"

    @property
    def workspaces_dir(self) -> Path:
        return self.root / "workspaces"


class WorkerRuntime:
    #: process_job outcomes that are not engine outcomes
    LEASE_BUSY = "lease_busy"
    UNKNOWN_RUN = "unknown_run"

    def __init__(
        self,
        config: WorkerConfig,
        nodes: NodeRegistry,
        llm: LlmPort | None = None,
        worker_id: str | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        knowledge: Any = None,
        graph_mode: str | None = None,
    ) -> None:
        self.config = config
        self.events = JsonlEventStore(config.events_dir)
        self.queue = FileJobQueue(
            config.queue_dir,
            claim_ttl_s=config.claim_ttl_s,
            max_deliveries=config.max_deliveries,
        )
        self.leases = RunLeaseStore(config.leases_dir, ttl_s=config.lease_ttl_s)
        self.worker_id = worker_id or f"worker_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._nodes = nodes
        self._llm = llm
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIdGenerator()
        # 卡片知识库端口：方案阶段提议人经 ToolBus 自主检索（knowledge_search /
        # knowledge_read）。缺省与 assembly.build_real_nodes 同一份进程缓存实例，
        # 节点预检索与工具检索读的是同一个库。
        self._knowledge = knowledge if knowledge is not None else load_knowledge_library()
        # 调度档位（§4.9 OMM_GRAPH）：显式参数优先，否则读环境变量；非法值按缺省
        # 处理并留警告（与 API 侧 _nodes_mode 同口径，拼写错误不得静默换档）。
        raw_mode = graph_mode if graph_mode is not None else os.environ.get(GRAPH_MODE_ENV)
        self.graph_mode, warning = resolve_graph_mode(raw_mode)
        if warning:
            logger.warning("%s", warning)
        #: 主 / 影子调度器的分歧记录（进程内累计；每条也进 warning 日志）。
        self.shadow_divergences: list[SchedulingDivergence] = []

    @property
    def knowledge(self) -> Any:
        return self._knowledge

    # -- run lifecycle --------------------------------------------------------

    def create_run(
        self,
        project_id: str,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
        enqueue: bool = True,
    ) -> str:
        engine, _services = self._build_engine()
        snapshot, _events = engine.create_run(
            project_id=project_id, inputs=inputs, run_id=run_id
        )
        if enqueue:
            self.queue.enqueue(snapshot.run_id, kind="advance")
        return snapshot.run_id

    def get_snapshot(self, run_id: str) -> TaskRunSnapshot | None:
        """Read-only recovery from the event log (no lease required)."""
        events = self.events.load(run_id)
        if not events:
            return None
        project_id = events[0].payload.get("project_id", "")
        return replay_events(run_id, project_id, events)

    # -- job processing ---------------------------------------------------------

    def process_job(self, job: JobEnvelope) -> str:
        if job.kind != "advance":
            return f"unsupported_kind:{job.kind}"
        lease = self.leases.acquire(job.run_id, self.worker_id)
        if lease is None:
            return self.LEASE_BUSY
        try:
            bundle = self._open_run(job.run_id)
            if bundle is None:
                return self.UNKNOWN_RUN
            engine, snapshot = bundle
            engine.heal_interrupted(snapshot)
            outcome = engine.run_until_blocked(
                snapshot, max_steps=self.config.step_budget_per_job
            )
            return outcome.status
        finally:
            self.leases.release(lease)

    # -- control actions --------------------------------------------------------

    def apply_action(self, run_id: str, action: str, reason: str | None = None) -> str:
        """Apply a user/API action to a run; returns the resulting task state.

        Actions that unblock scheduling (resume / retry / approve) also
        enqueue a fresh advance job. This is the seam the control plane will
        call through once backend/api integration lands.
        """
        lease = self.leases.acquire(run_id, self.worker_id)
        if lease is None:
            raise RuntimeError(f"run {run_id} is busy; action {action!r} not applied")
        try:
            bundle = self._open_run(run_id)
            if bundle is None:
                raise KeyError(f"unknown run {run_id}")
            engine, snapshot = bundle
            if action == "pause":
                engine.request_pause(snapshot)
            elif action == "resume":
                engine.resume(snapshot)
                self.queue.enqueue(run_id, kind="advance")
            elif action == "cancel":
                engine.request_cancel(snapshot)
                self.queue.enqueue(run_id, kind="advance")  # finalize promptly
            elif action == "retry":
                engine.retry(snapshot)
                self.queue.enqueue(run_id, kind="advance")
            elif action == "approve":
                engine.resolve_review(snapshot, approved=True, reason=reason)
                self.queue.enqueue(run_id, kind="advance")
            elif action == "reject":
                # 拒绝 = 退回重做（产品审批卡的「退回重做方案」）：解决审批后运行
                # 落 FAILED 且 failure 指向请求确认的阶段，立即 retry 重新进入该
                # 阶段（attempt+1）并排队推进，下一轮产出后会再次请求确认——与
                # API 侧 resolve_approval 的 reject 分支同构。
                engine.resolve_review(snapshot, approved=False, reason=reason)
                engine.retry(snapshot)
                self.queue.enqueue(run_id, kind="advance")
            else:
                raise ValueError(f"unknown action {action!r}")
            return snapshot.state.value
        finally:
            self.leases.release(lease)

    # -- wiring -----------------------------------------------------------------

    def _build_engine(self, run_id: str | None = None) -> tuple[TaskRunEngine, NodeServices]:
        services = NodeServices(
            clock=self._clock,
            ids=self._ids,
            artifacts=None,  # bound per run in _open_run
            llm=self._llm,
        )
        scheduler, shadow = schedulers_for_mode(self.graph_mode)

        def on_divergence(divergence: SchedulingDivergence) -> None:
            self.shadow_divergences.append(divergence)
            logger.warning(
                "graph shadow divergence run=%s seq=%s state=%s kind=%s primary=%s shadow=%s %s",
                run_id,
                divergence.seq,
                divergence.state,
                divergence.kind,
                divergence.primary,
                divergence.shadow,
                divergence.detail,
            )

        engine = TaskRunEngine(
            sink=self.events,
            clock=self._clock,
            ids=self._ids,
            nodes=self._nodes,
            services=services,
            scheduler=scheduler,
            shadow=shadow,
            on_divergence=on_divergence,
        )
        return engine, services

    def _open_run(self, run_id: str) -> tuple[TaskRunEngine, TaskRunSnapshot] | None:
        snapshot = self.get_snapshot(run_id)
        if snapshot is None:
            return None
        engine, services = self._build_engine(run_id)

        workspace = TaskWorkspace(self.config.workspaces_dir, run_id)
        services.artifacts = WorkspaceArtifactStore(workspace)

        # 沙箱共用 run 的产物存储实例：实验代码创建的文件与节点发布的产物走同
        # 一条存储路径（与 API 侧把沙箱 store 指向其 ApiArtifactStore 同构）。
        sandbox = PythonSandbox(
            workspace,
            timeout_s=self.config.python_timeout_s,
            store=services.artifacts,
        )
        registry = ToolRegistry()
        registry.register(sandbox.spec())
        # 数据阶段工具（与 API 侧 _build_tool_invoker 保持同构）：table_profile
        # 确定性画像 + 工作区四件套（ws_list 是数据节点画像前置的入口）。
        registry.register(table_profile_spec(workspace))
        for spec in sandbox_workspace_specs(workspace):
            registry.register(spec)
        # 方案阶段工具（§10.3 切片二，与 API 侧 _build_tool_invoker 同构）：卡片知识库
        # 两个只读工具，三路提议人子代理在会话里自主检索、顺链读卡。
        for spec in knowledge_tool_specs(self._knowledge):
            registry.register(spec)
        # 最小授权：允许列表与调用方层级封顶 execute；工具事件
        # 经引擎 record_external 落日志（序列分配必须留在引擎单路径上）。
        # 方案阶段的三路 Proposer 子代理并行（H3 fan-out），审计与知识库工具调用
        # 会从多个线程到达：引擎的 emit→apply 单路径不是线程安全的，外部记录
        # 一律经同一把锁串行。
        record_lock = threading.Lock()

        def record(event_type: EventType, payload: dict[str, Any]) -> None:
            with record_lock:
                engine.record_external(snapshot, event_type, payload)

        services.tools = RecordingInvoker(
            registry.with_allowlist(
                {
                    PythonSandbox.TOOL_NAME,
                    "table_profile",
                    "ws_list",
                    "ws_read",
                    "ws_write",
                    "env_probe",
                    KNOWLEDGE_SEARCH_TOOL,
                    KNOWLEDGE_READ_TOOL,
                }
            ),
            recorder=record,
            caller_max_tier="execute",
        )
        # 沙盒执行体（H3）：清洗/实验节点经监督者派发子代理；spawn 与结果
        # 审计走 record_external 落 TOOL_CALLED（payload.tool="subagent:<kind>"，
        # §8.3），与工具调用同一条事件序列（与 API 侧落 run.log 的定位同构）。
        services.extras["subagents"] = SubagentSupervisor(
            audit=lambda payload: record(EventType.TOOL_CALLED, payload)
        )
        return engine, snapshot


class WorkerLoop:
    """Poll → claim → process. ``tick`` is the unit tests drive directly;
    ``run_forever`` is the real process entrypoint."""

    def __init__(self, runtime: WorkerRuntime) -> None:
        self.runtime = runtime

    def tick(self) -> str | None:
        self.runtime.queue.requeue_stale()
        job = self.runtime.queue.claim()
        if job is None:
            return None
        try:
            outcome = self.runtime.process_job(job)
        except Exception:
            self.runtime.queue.fail(job)
            return "job_error"
        if outcome == WorkerRuntime.LEASE_BUSY:
            self.runtime.queue.fail(job)  # someone is on it; try again later
        else:
            self.runtime.queue.complete(job)
        return outcome

    def run_forever(self, idle_sleep_s: float = 1.0) -> None:  # pragma: no cover
        while True:
            if self.tick() is None:
                time.sleep(idle_sleep_s)


def default_config(repo_root: str | os.PathLike[str]) -> WorkerConfig:
    """Standard layout under the gitignored runs/ directory."""
    return WorkerConfig(root=Path(repo_root) / "runs" / "agent-runtime")
