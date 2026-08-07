"""请求中间件：request_id 贯穿 + 写方法 Origin 校验（Cookie 会话的 CSRF 基线防护）。"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_request_id: ContextVar[str] = ContextVar("request_id", default="req_unknown")

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_request_id() -> str:
    return _request_id.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = f"req_{uuid4().hex[:12]}"
        token = _request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """写方法（POST/PUT/PATCH/DELETE）拒绝陌生 Origin。

    Cookie 会话依赖浏览器自动携带凭据，必须阻断跨站写请求：
    - 无 Origin 头（同站导航、curl、测试客户端）放行；
    - Origin 等于请求自身源（同源部署）放行；
    - Origin 在白名单（开发环境 Vite 端口）放行；
    - 其余一律 403 ORIGIN_FORBIDDEN。
    """

    def __init__(self, app, allowed_origins: Iterable[str]) -> None:
        super().__init__(app)
        self._allowed = set(allowed_origins)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin:
                same_origin = origin == f"{request.url.scheme}://{request.url.netloc}"
                if not same_origin and origin not in self._allowed:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "code": "ORIGIN_FORBIDDEN",
                            "message": "请求来源不被允许",
                            "request_id": get_request_id(),
                            "details": None,
                        },
                    )
        return await call_next(request)
