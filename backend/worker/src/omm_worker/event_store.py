"""Durable per-run JSONL event log.

This is the MVP storage backend for the run event stream: one append-only
``events.jsonl`` per run. It implements the core ``EventSink`` port and doubles
as the replay source for recovery. When the control plane's PostgreSQL event
table lands (backend/api batch), this class is the swap point — the sink and
replay contracts stay identical.

Idempotency: at-least-once redelivery is absorbed here by dropping events
whose seq is not strictly greater than the last stored seq for that run
(event_id is derived from run_id+seq, so equal seq == same event).

Durability: each append is flushed and fsync'd before ``emit`` returns —
the engine's "event persisted before state advances" guarantee depends on it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from omm_agent_core import AgentEvent


class JsonlEventStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._max_seq: dict[str, int] = {}

    def _path(self, run_id: str) -> Path:
        return self.root / run_id / "events.jsonl"

    def max_seq(self, run_id: str) -> int:
        cached = self._max_seq.get(run_id)
        if cached is not None:
            return cached
        last = 0
        path = self._path(run_id)
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        last = max(last, int(json.loads(line)["seq"]))
        self._max_seq[run_id] = last
        return last

    # -- EventSink port ------------------------------------------------------

    def emit(self, event: AgentEvent) -> None:
        if event.seq <= self.max_seq(event.run_id):
            return  # duplicate delivery of an already-durable event
        path = self._path(event.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._max_seq[event.run_id] = event.seq

    # -- replay source ---------------------------------------------------------

    def load(self, run_id: str) -> list[AgentEvent]:
        path = self._path(run_id)
        if not path.exists():
            return []
        events: list[AgentEvent] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(AgentEvent.from_dict(json.loads(line)))
        events.sort(key=lambda item: item.seq)
        return events

    def run_ids(self) -> list[str]:
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and (entry / "events.jsonl").exists()
        )
