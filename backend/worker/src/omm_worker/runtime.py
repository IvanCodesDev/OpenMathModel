"""Worker runtime: recover → heal → advance, under a run lease.

Execution-plane rules implemented here (PROJECT_STRUCTURE / system-overview):

- every advance happens under the run's lease (cross-process exclusion);
- state is recovered by replaying the durable event log, never trusted from
  memory;
- dangling RUNNING steps from a dead executor are failed ("healed") before
  new work starts, so retries are explicit attempts, not silent overwrites;
- events/artifacts are persisted before state moves (engine + JSONL sink);
- the job loop is budgeted — a runaway registry cannot spin forever.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omm_agent_core import (
    AdvanceOutcome,
    LlmPort,
    NodeRegistry,
    NodeServices,
    SystemClock,
    TaskRunEngine,
    TaskRunSnapshot,
    UuidIdGenerator,
    replay_events,
)
from omm_agent_tools import (
    PythonSandbox,
    RecordingInvoker,
    TaskWorkspace,
    ToolRegistry,
    WorkspaceArtifactStore,
)

from .event_store import JsonlEventStore
from .lease import RunLeaseStore
from .queue import FileJobQueue, JobEnvelope


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
        clock=None,
        ids=None,
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
                engine.resolve_review(snapshot, approved=False, reason=reason)
            else:
                raise ValueError(f"unknown action {action!r}")
            return snapshot.state.value
        finally:
            self.leases.release(lease)

    # -- wiring -----------------------------------------------------------------

    def _build_engine(self) -> tuple[TaskRunEngine, NodeServices]:
        services = NodeServices(
            clock=self._clock,
            ids=self._ids,
            artifacts=None,  # bound per run in _open_run
            llm=self._llm,
        )
        engine = TaskRunEngine(
            sink=self.events,
            clock=self._clock,
            ids=self._ids,
            nodes=self._nodes,
            services=services,
        )
        return engine, services

    def _open_run(self, run_id: str) -> tuple[TaskRunEngine, TaskRunSnapshot] | None:
        snapshot = self.get_snapshot(run_id)
        if snapshot is None:
            return None
        engine, services = self._build_engine()

        workspace = TaskWorkspace(self.config.workspaces_dir, run_id)
        services.artifacts = WorkspaceArtifactStore(workspace)

        sandbox = PythonSandbox(workspace, timeout_s=self.config.python_timeout_s)
        registry = ToolRegistry()
        registry.register(sandbox.spec())
        services.tools = RecordingInvoker(
            registry,
            recorder=lambda event_type, payload: engine.record_external(
                snapshot, event_type, payload
            ),
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
        except Exception:  # noqa: BLE001 - the loop must survive any job crash
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
