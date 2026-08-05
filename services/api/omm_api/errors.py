"""统一错误信封：{code, message, request_id, details}。

错误码只增不改；对外不泄露堆栈与内部细节，服务端日志保留证据。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .middleware import get_request_id

logger = logging.getLogger("omm.api")


class ApiError(Exception):
    """业务错误基类，支持两种等价用法：

    - 子类风格：``NotFoundError("消息", details)``（code/状态码由子类固定）
    - 直接风格：``ApiError(401, "AUTH_REQUIRED", "消息")``（认证模块惯用）
    """

    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(
        self,
        arg1: Any = None,
        arg2: Any = None,
        arg3: Any = None,
        details: Any = None,
    ) -> None:
        if isinstance(arg1, int):
            self.http_status = arg1
            if arg2 is not None:
                self.code = str(arg2)
            self.message = str(arg3 or "")
            self.details = details
        else:
            self.message = str(arg1 or "")
            self.details = arg2 if arg2 is not None else details
        super().__init__(self.message)


class NotFoundError(ApiError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(ApiError):
    code = "CONFLICT"
    http_status = 409


class InvalidActionError(ApiError):
    code = "INVALID_ACTION"
    http_status = 409


class IdempotencyKeyReusedError(ApiError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409

    def __init__(self, message: str = "同一幂等键不允许携带不同的请求内容", details: Any = None) -> None:
        super().__init__(message, details)


def _envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "request_id": get_request_id(),
        "details": details,
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        # message 直接给出第一条字段级原因（如“邮箱格式不正确”），
        # 让 UI 不必解析 details 也能展示可行动的提示。
        message = "请求校验失败"
        if details:
            first_msg = str(details[0].get("msg", "")).strip()
            if first_msg.startswith("Value error, "):
                first_msg = first_msg[len("Value error, "):]
            if first_msg:
                message = first_msg
        return JSONResponse(
            status_code=422,
            content=_envelope("VALIDATION_ERROR", message, details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error request_id=%s", get_request_id())
        return JSONResponse(
            status_code=500,
            content=_envelope("INTERNAL_ERROR", "服务器内部错误"),
        )
