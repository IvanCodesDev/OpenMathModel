from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import SESSION_COOKIE_NAME
from .db import get_db
from .errors import ApiError
from .models import AuthSession, User, utcnow
from .security import sha256_hex


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

    # 滑动记录活跃时间；避免每个请求都写库
    if now - session.last_seen_at > timedelta(seconds=60):
        session.last_seen_at = now
        db.commit()

    return AuthContext(user=user, session=session)
