"""对话回复与「测试连接」：设置中心自定义 API 的两个消费入口。

/api/chat 是无状态代理：对话历史由前端随请求携带（对话内容本机留存策略
归「数据与隐私」，服务端不落库），服务端按当前用户保存的接口配置出网调用，
流式时以 SSE 把 meta/delta/done/error 事件转发给页面。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..deps import AuthContext, get_auth_context
from ..errors import ApiError
from ..llm import (
    LlmEndpoint,
    auto_route,
    complete_with_fallback,
    is_third_party_host,
    parse_llm_config,
    stream_events,
    test_endpoint,
)
from ..schemas import ChatRequest, LlmTestRequest

chat_router = APIRouter(tags=["chat"])
llm_router = APIRouter(tags=["llm"])

#: 对话页面的系统提示词：与任务工作流解耦，只约束身份与输出习惯。
CHAT_SYSTEM_PROMPT = (
    "你是 OpenMathModel 的数学建模 Agent，正在任务页面与用户对话。"
    "用户的补充要求会影响后续建模、实验与论文写作，请给出具体、可执行的回应；"
    "默认使用中文，数学公式使用 LaTeX 行内写法。"
)


def _endpoint_from_request(body: LlmTestRequest) -> LlmEndpoint:
    return LlmEndpoint(
        id=body.id or "test",
        name=body.name,
        protocol=body.protocol,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        organization=body.organization,
        headers=body.headers,
        path_prefix=body.path_prefix,
    )


@llm_router.post("/test")
def test_llm_endpoint(
    body: LlmTestRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """用表单当前值做一次最小补全，验证地址、密钥与模型 ID 全链路可用。"""
    outcome = test_endpoint(_endpoint_from_request(body), allow_proxy=body.allow_proxy)
    return {
        "ok": True,
        "latency_ms": outcome.elapsed_ms,
        "model": outcome.model,
        "host": outcome.endpoint.host,
        "third_party": is_third_party_host(outcome.endpoint.host),
        "reply": outcome.text[:200],
    }


def _latest_user_text(body: ChatRequest) -> str:
    for message in reversed(body.messages):
        if message.role == "user":
            return message.content
    return body.messages[-1].content


@chat_router.post("")
def chat(
    body: ChatRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    config = parse_llm_config(ctx.user.llm_config)
    if not config.endpoints:
        raise ApiError(
            400,
            "LLM_NOT_CONFIGURED",
            "尚未配置模型接口：请在设置中心「自定义 API」填写并保存接口后再试",
        )
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + [
        {"role": m.role, "content": m.content} for m in body.messages
    ]
    use_stream = config.stream if body.stream is None else body.stream

    # 模型选择器三种形态：Auto（难度判定 + 权重路由）/ 指定已保存接口 / 默认主接口链
    chain = None
    route_meta = None
    if body.route == "auto":
        decision = auto_route(config, _latest_user_text(body))
        chain = list(decision.chain)
        route_meta = decision.meta()
    elif body.endpoint_id:
        endpoint = config.find(body.endpoint_id)
        if endpoint is None:
            raise ApiError(404, "LLM_ENDPOINT_NOT_FOUND", "选中的接口已被删除，请重新选择模型")
        chain = config.chain_from(endpoint)

    if not use_stream:
        outcome = complete_with_fallback(config, messages, model=body.model, chain=chain)
        return {
            "reply": outcome.text,
            "reasoning": outcome.reasoning,
            "model": outcome.model,
            "endpoint": outcome.endpoint.name,
            "host": outcome.endpoint.host,
            "third_party": is_third_party_host(outcome.endpoint.host),
            "fallback_used": outcome.fallback_used,
            "usage": outcome.usage,
            "elapsed_ms": outcome.elapsed_ms,
            "route": route_meta,
        }

    extra_meta = {"route": route_meta} if route_meta else None

    def sse() -> Iterator[str]:
        for event in stream_events(config, messages, model=body.model, chain=chain, extra_meta=extra_meta):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
