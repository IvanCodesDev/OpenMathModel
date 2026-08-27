from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import SESSION_COOKIE_NAME
from .db import get_db
from .errors import ApiError
from .models import AuthSession, User, utcnow
from .security import sha256_hex

logger = logging.getLogger("omm.api")


@dataclass
class AuthContext:
    user: User
    session: AuthSession


def get_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "请先登录")

    token_hash = sha256_hex(token)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    now = utcnow()
    if session is None or not session.is_active(now):
        raise ApiError(401, "SESSION_EXPIRED", "登录状态已失效，请重新登录")

    user = db.get(User, session.user_id)
    if user is None:
        raise ApiError(401, "SESSION_EXPIRED", "登录状态已失效，请重新登录")

    # 滑动记录活跃时间；避免每个请求都写库。这只是活跃度统计，写不进去也不该
    # 影响请求本身：本地 SQLite 只有一个写位，后台推进线程占着时这次提交会失败，
    # 而它发生在认证阶段，任何业务请求都会被它打成 500。
    if now - session.last_seen_at > timedelta(seconds=60):
        session.last_seen_at = now
        try:
            db.commit()
        except SQLAlchemyError:
            logger.warning("会话活跃时间写入失败，已跳过", exc_info=True)
            db.rollback()

    return AuthContext(user=user, session=session)
