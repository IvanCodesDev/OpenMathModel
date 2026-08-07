"""Per-run lease: cross-process mutual exclusion for advancing one run.

An in-process lock must never impersonate distributed mutual exclusion, so
even the MVP uses a real on-disk lease: created with O_CREAT|O_EXCL (atomic),
carrying owner + expiry, stealable only after expiry, and verified after
every write (steal races resolve by re-reading the file).

Honest limits, stated: a file lease has no fencing token, so it cannot stop a
paused-and-resumed zombie from attempting writes after expiry. The system is
still safe because every side effect goes through the event store, which
rejects stale sequence numbers — the lease provides liveness/efficiency, the
event log provides correctness. The Redis/Postgres lease that replaces this
in the infra batch will add real fencing.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    expires_at: float
    token: str

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class RunLeaseStore:
    def __init__(self, root: str | os.PathLike[str], ttl_s: float = 120.0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.lease.json"

    def _read(self, run_id: str) -> Lease | None:
        path = self._path(run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return Lease(
            run_id=raw["run_id"],
            owner=raw["owner"],
            expires_at=float(raw["expires_at"]),
            token=raw["token"],
        )

    def _write(self, lease: Lease) -> None:
        path = self._path(lease.run_id)
        temp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        temp.write_text(
            json.dumps(
                {
                    "run_id": lease.run_id,
                    "owner": lease.owner,
                    "expires_at": lease.expires_at,
                    "token": lease.token,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temp, path)

    # -- api -------------------------------------------------------------

    def acquire(self, run_id: str, owner: str) -> Lease | None:
        """Try to acquire the run lease; None when it is validly held."""
        path = self._path(run_id)
        lease = Lease(
            run_id=run_id,
            owner=owner,
            expires_at=time.time() + self.ttl_s,
            token=uuid.uuid4().hex,
        )
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current = self._read(run_id)
            if current is not None and not current.expired:
                return None
            # Expired (or corrupt) lease: steal, then verify we actually won,
            # because two stealers can both os.replace — last writer wins.
            self._write(lease)
            final = self._read(run_id)
            if final is not None and final.token == lease.token:
                return lease
            return None
        try:
            os.write(
                fd,
                json.dumps(
                    {
                        "run_id": lease.run_id,
                        "owner": lease.owner,
                        "expires_at": lease.expires_at,
                        "token": lease.token,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        finally:
            os.close(fd)
        return lease

    def renew(self, lease: Lease) -> Lease | None:
        current = self._read(lease.run_id)
        if current is None or current.token != lease.token:
            return None  # lost the lease; caller must stop advancing this run
        renewed = Lease(
            run_id=lease.run_id,
            owner=lease.owner,
            expires_at=time.time() + self.ttl_s,
            token=lease.token,
        )
        self._write(renewed)
        return renewed

    def release(self, lease: Lease) -> None:
        current = self._read(lease.run_id)
        if current is not None and current.token == lease.token:
            try:
                self._path(lease.run_id).unlink()
            except FileNotFoundError:
                pass
