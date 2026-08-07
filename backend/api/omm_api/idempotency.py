"""写操作幂等：Idempotency-Key 请求头 + 请求签名。

同 key 同签名 → 返回首次响应；同 key 不同签名 → 409 IDEMPOTENCY_KEY_REUSED。
MVP 限制：produce 执行与记录写入不在同一事务，极小并发窗口内可能重复执行；
Durable 执行接入后由幂等 Activity 兜底，这里保证请求层语义正确。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .errors import IdempotencyKeyReusedError
from .orm import IdempotencyRecord
from .serialize import utcnow


def _signature(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def with_idempotency(
    session_factory: sessionmaker[Session],
    key: str | None,
    signature_payload: Any,
    produce: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    if not key:
        return produce()

    signature = _signature(signature_payload)
    with session_factory() as session:
        record = session.get(IdempotencyRecord, key)
        if record is not None:
            if record.signature != signature:
                raise IdempotencyKeyReusedError()
            return record.status_code, record.response

    status_code, response = produce()

    with session_factory() as session:
        try:
            session.add(
                IdempotencyRecord(
                    key=key,
                    signature=signature,
                    status_code=status_code,
                    response=response,
                    created_at=utcnow(),
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            stored = session.get(IdempotencyRecord, key)
            if stored is not None and stored.signature != signature:
                raise IdempotencyKeyReusedError() from None
    return status_code, response
