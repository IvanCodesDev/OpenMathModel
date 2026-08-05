"""File-based job queue with atomic claims.

MVP transport: a directory of JSON job files. A job is claimed by atomically
renaming it from ``pending/`` into ``claimed/`` — on NTFS/POSIX ``os.replace``
succeeds for exactly one contender, which is the whole mutual-exclusion story
for delivery (run-level exclusion is the lease's job, see lease.py).

Delivery semantics are at-least-once: a crashed claimer leaves the file in
``claimed/``; ``requeue_stale`` moves it back after its claim expires, and
``deliveries`` counts attempts so poison jobs park in ``dead/`` instead of
looping forever. Redis/Postgres queues replace this class in the infra batch
behind the same method surface.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JobEnvelope:
    job_id: str
    run_id: str
    kind: str  # "advance" is the only kind in this batch
    payload: dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = 0.0
    deliveries: int = 0

    def job_key(self) -> str:
        """Idempotency key: one logical job per (run, kind, payload identity)."""
        payload_part = json.dumps(self.payload, sort_keys=True, ensure_ascii=False)
        return f"{self.run_id}:{self.kind}:{payload_part}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "payload": self.payload,
            "enqueued_at": self.enqueued_at,
            "deliveries": self.deliveries,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "JobEnvelope":
        return JobEnvelope(
            job_id=raw["job_id"],
            run_id=raw["run_id"],
            kind=raw["kind"],
            payload=dict(raw.get("payload") or {}),
            enqueued_at=float(raw.get("enqueued_at", 0.0)),
            deliveries=int(raw.get("deliveries", 0)),
        )


class FileJobQueue:
    def __init__(
        self,
        root: str | os.PathLike[str],
        claim_ttl_s: float = 300.0,
        max_deliveries: int = 3,
    ) -> None:
        self.root = Path(root)
        self.claim_ttl_s = claim_ttl_s
        self.max_deliveries = max_deliveries
        for name in ("pending", "claimed", "done", "dead"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    # -- producer -----------------------------------------------------------

    def enqueue(
        self, run_id: str, kind: str = "advance", payload: dict[str, Any] | None = None
    ) -> JobEnvelope:
        job = JobEnvelope(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            kind=kind,
            payload=payload or {},
            enqueued_at=time.time(),
        )
        target = self.root / "pending" / f"{job.job_id}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(job.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(temp, target)
        return job

    # -- consumer -----------------------------------------------------------

    def claim(self) -> JobEnvelope | None:
        """Claim the oldest pending job; None when the queue is empty."""

        def age_key(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:  # raced away between glob and stat
                return float("inf")

        pending = sorted((self.root / "pending").glob("job_*.json"), key=age_key)
        for candidate in pending:
            claimed_path = self.root / "claimed" / candidate.name
            try:
                os.replace(candidate, claimed_path)  # exactly one winner
            except (FileNotFoundError, PermissionError):
                continue  # someone else won this file; try the next
            job = JobEnvelope.from_dict(
                json.loads(claimed_path.read_text(encoding="utf-8"))
            )
            job = JobEnvelope(
                job_id=job.job_id,
                run_id=job.run_id,
                kind=job.kind,
                payload=job.payload,
                enqueued_at=job.enqueued_at,
                deliveries=job.deliveries + 1,
            )
            claimed_path.write_text(
                json.dumps(job.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            os.utime(claimed_path)  # claim timestamp for staleness detection
            return job
        return None

    def complete(self, job: JobEnvelope) -> None:
        self._move(job, "claimed", "done")

    def fail(self, job: JobEnvelope) -> str:
        """Return the job to pending, or park it in dead/ once poisoned."""
        if job.deliveries >= self.max_deliveries:
            self._move(job, "claimed", "dead")
            return "dead"
        self._move(job, "claimed", "pending")
        return "requeued"

    def requeue_stale(self) -> int:
        """Recover jobs whose claimer disappeared (claim older than TTL)."""
        recovered = 0
        cutoff = time.time() - self.claim_ttl_s
        for path in (self.root / "claimed").glob("job_*.json"):
            if path.stat().st_mtime < cutoff:
                try:
                    os.replace(path, self.root / "pending" / path.name)
                    recovered += 1
                except (FileNotFoundError, PermissionError):
                    continue
        return recovered

    def counts(self) -> dict[str, int]:
        return {
            name: len(list((self.root / name).glob("job_*.json")))
            for name in ("pending", "claimed", "done", "dead")
        }

    def _move(self, job: JobEnvelope, source: str, target: str) -> None:
        source_path = self.root / source / f"{job.job_id}.json"
        target_path = self.root / target / f"{job.job_id}.json"
        source_path.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        os.replace(source_path, target_path)
