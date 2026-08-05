"""Ports injected into the engine and nodes.

Prompt/LLM/tool/storage implementations live OUTSIDE the core (dependency
rule 4 in PROJECT_STRUCTURE.md); the core only sees these protocols. In-memory
defaults below exist so the core is testable standalone and so callers can
start with zero infrastructure.
"""

from __future__ import annotations

import hashlib
import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .models import AgentEvent, ArtifactRef, ToolResult


@runtime_checkable
class Clock(Protocol):
    def now_iso(self) -> str:
        """Current UTC time as ISO-8601 string."""


@runtime_checkable
class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...


@runtime_checkable
class EventSink(Protocol):
    """Durable destination for events.

    ``emit`` MUST persist (or forward to something durable) before returning:
    the engine applies an event to the snapshot only after emit returns, which
    is what guarantees "events/artifacts are written before state advances".
    Implementations MUST deduplicate by ``event.event_id`` when re-delivered.
    """

    def emit(self, event: AgentEvent) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def put(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        producer_step: str,
    ) -> ArtifactRef: ...


@runtime_checkable
class LlmPort(Protocol):
    """Minimal completion port. Prompt resolution happens in skills, not here."""

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str: ...


@runtime_checkable
class ToolInvoker(Protocol):
    def invoke(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult: ...


# --------------------------------------------------------------------------
# Zero-infrastructure defaults (tests, local drivers)
# --------------------------------------------------------------------------


class SystemClock:
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class UuidIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SequentialIdGenerator:
    """Deterministic ids for tests and golden trajectories."""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = {}

    def new_id(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):04d}"


class FixedClock:
    """Deterministic clock for tests; advances a fixed amount per call."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00") -> None:
        self._current = datetime.fromisoformat(start)

    def now_iso(self) -> str:
        value = self._current.isoformat()
        from datetime import timedelta

        self._current = self._current + timedelta(seconds=1)
        return value


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []
        self._seen: set[str] = set()

    def emit(self, event: AgentEvent) -> None:
        if event.event_id in self._seen:
            return
        self._seen.add(event.event_id)
        self.events.append(event)


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self._ids = UuidIdGenerator()

    def put(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        producer_step: str,
    ) -> ArtifactRef:
        artifact_id = self._ids.new_id("art")
        uri = f"memory://{run_id}/{artifact_id}/{name}"
        self.blobs[uri] = content
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=uri,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type=media_type,
            producer_step=producer_step,
        )


@dataclass
class NodeServices:
    """Bundle of ports handed to nodes; only what a node may touch.

    ``artifacts``/``llm``/``tools`` are optional because they are bound per
    run by the executor (worker) — a node that needs a missing port must fail
    its step explicitly rather than assume wiring.
    """

    clock: Clock
    ids: IdGenerator
    artifacts: ArtifactStore | None = None
    llm: LlmPort | None = None
    tools: ToolInvoker | None = None
    extras: dict[str, Any] = field(default_factory=dict)
