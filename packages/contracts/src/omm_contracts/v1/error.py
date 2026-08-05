# 本文件由 scripts/generate_python.py 从 schemas/v1 生成，禁止手改。
# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, constr


class ErrorEnvelope(BaseModel):
    """
    API 统一错误返回。异步动作返回任务状态而不是伪装成同步成功；错误码只增不改、不复用旧码表达新语义。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: constr(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100) = Field(
        ...,
        description="机器可分类错误码，如 VALIDATION_ERROR / NOT_FOUND / CONFLICT / IDEMPOTENCY_KEY_REUSED / INVALID_ACTION。",
    )
    message: constr(min_length=1, max_length=4000)
    request_id: constr(min_length=1, max_length=100)
    details: dict[str, Any] | list[Any] | None = Field(
        None, description="结构化补充信息（字段级校验错误等），不得包含堆栈或敏感信息。"
    )
