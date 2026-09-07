"""对话回复与「测试连接」：设置中心自定义 API 的两个消费入口。

两条对话通道共用同一套接口解析 / 预算闸门 / 路由决策（``_prepare_call``）：

- ``POST /api/chat``：无状态代理——对话历史由前端随请求携带，服务端不落库，
  流式时以 SSE 转发 meta/delta/done/error。浏览器断连即取消上游调用。
- ``POST /api/chat/turns`` 等（ADR-0016 服务端托管对话轮）：生成是后台作业，
  页面只是观众——断连不影响生成，重进按 scope 拉记录并续接直播；记录落
  ``chat_turns``（「保存任务历史」关闭时只在内存）。有归属（任务 / 首页对话）
  的页面一律走这条；无归属的演示页仍走无状态通道。

用量监控：每次成功调用（非流式回复、流式 done、测试连接、Auto 难度判定）
各记一条 llm_usage_records；预算硬限制开启且当月估算费用达标时，
enforce_budget 只放行本地/免费接口（设置中心「用量监控」）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..chat_turns import ChatTurnHub, sse_line, terminal_event_for
from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..errors import ApiError
from ..llm import (
    ChatOutcome,
    LlmConfig,
    LlmEndpoint,
    auto_route,
    complete_with_fallback,
    is_third_party_host,
    list_models,
    parse_llm_config,
    stream_events,
    test_endpoint,
)
from ..privacy import privacy_settings_of
from ..run_control import JUDGE_HISTORY_TURNS, action_events, run_control_step
from ..schemas import ChatRequest, ChatTurnStartRequest, ChatTurnTraceRequest, LlmTestRequest
from ..usage import enforce_budget, record_stream_usage, record_usage
from .task_runs import get_owned_run

logger = logging.getLogger("omm.chat")

chat_router = APIRouter(tags=["chat"])
llm_router = APIRouter(tags=["llm"])

#: 对话页面的系统提示词：与任务工作流解耦，只约束身份与输出习惯。
CHAT_SYSTEM_PROMPT = (
    "你是 OpenMathModel 的数学建模 Agent，正在任务页面与用户对话。"
    "用户的补充要求会影响后续建模、实验与论文写作，请给出具体、可执行的回应；"
    "默认使用中文，数学公式使用 LaTeX 行内写法。"
)

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


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
    db: Session = Depends(get_db),
):
    """用表单当前值做一次最小补全，验证地址、密钥与模型 ID 全链路可用。"""
    outcome = test_endpoint(_endpoint_from_request(body), allow_proxy=body.allow_proxy)
    record_usage(
        db,
        user_id=ctx.user.id,
        source="test",
        outcome=outcome,
        third_party=is_third_party_host(outcome.endpoint.host),
    )
    db.commit()
    return {
        "ok": True,
        "latency_ms": outcome.elapsed_ms,
        "model": outcome.model,
        "host": outcome.endpoint.host,
        "third_party": is_third_party_host(outcome.endpoint.host),
        "reply": outcome.text[:200],
    }


@llm_router.post("/models")
def list_llm_models(
    body: LlmTestRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """按表单当前值拉取该接口提供的模型 ID，供「默认模型 ID」补全。

    只读一张列表：不产生 token、不记用量、不落库，因此也不受预算闸门约束。
    密钥同样只在服务端使用，与其余出网点一致。
    """
    endpoint = _endpoint_from_request(body)
    return {
        "models": list_models(endpoint, allow_proxy=body.allow_proxy),
        "host": endpoint.host,
        "third_party": is_third_party_host(endpoint.host),
    }


@llm_router.get("/catalog")
def get_model_catalog(request: Request, ctx: AuthContext = Depends(get_auth_context)):
    """厂商在售型号目录（ADR-0017）：设置中心「模型厂商」卡片与模型 ID 补全的数据源。

    永不阻塞在出网上：返回当前已有的同步结果（或内置快照），``synced_at`` /
    ``stale`` / ``error`` 让页面如实标注新鲜度。
    """
    return request.app.state.model_catalog.view()


@llm_router.post("/catalog/refresh")
def refresh_model_catalog(request: Request, ctx: AuthContext = Depends(get_auth_context)):
    """「立即同步」：同步拉一次公共目录后返回新视图；拉不到抛 502，旧数据保留。"""
    catalog = request.app.state.model_catalog
    if not request.app.state.settings.model_catalog_enabled:
        raise ApiError(409, "MODEL_CATALOG_DISABLED", "模型目录同步已在服务端关闭（OMM_MODEL_CATALOG_ENABLED=false）")
    catalog.refresh(force=True)
    return catalog.view()


def _latest_user_text(body: ChatRequest) -> str:
    for message in reversed(body.messages):
        if message.role == "user":
            return message.content
    return body.messages[-1].content


# ── 两条通道共用的调用准备 ────────────────────────────────────────────────


@dataclass
class PreparedCall:
    """一次对话调用的全部决定项；Auto 难度判定延后到 resolve_chain（它本身要出网）。"""

    gated: LlmConfig
    messages: list[dict[str, str]]
    images: Optional[list[dict[str, str]]]
    model: Optional[str]
    use_stream: bool
    chain: Optional[list[LlmEndpoint]]
    route_meta: Optional[dict[str, Any]]
    #: 非 None = Auto 模式，待判定：(问题, 微上下文, 上轮难度, 上轮接口, 继承轮数)
    auto: Optional[tuple[str, str, Optional[int], Optional[str], int]]


UsageHook = Callable[[str, ChatOutcome, Optional[int]], None]


def _prepare_call(body: ChatRequest, ctx: AuthContext, db: Session) -> PreparedCall:
    config = parse_llm_config(ctx.user.llm_config)
    if not config.endpoints:
        raise ApiError(
            400,
            "LLM_NOT_CONFIGURED",
            "尚未配置模型接口：请在设置中心「自定义 API」填写并保存接口后再试",
        )
    # 预算硬限制：达标后只留本地/免费接口；全是付费接口时直接 429（用量监控）。
    gated = enforce_budget(db, ctx.user, config)
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + [
        {"role": m.role, "content": m.content} for m in body.messages
    ]
    # 视觉直通图片（ADR-0010）：挂到最后一条 user 消息，各协议格式在 llm.py 转换。
    images = [image.model_dump() for image in body.images] or None
    use_stream = gated.stream if body.stream is None else body.stream

    # 模型选择器三种形态：Auto（难度判定 + 权重路由）/ 指定已保存接口 / 默认主接口链。
    # 携图且用户没有显式钉接口时，设置中心「视觉理解」定向优先（ADR-0015 决策 4）：
    # Auto 难度路由看不见图片，可能把图发给纯文本模型。
    chain: Optional[list[LlmEndpoint]] = None
    route_meta: Optional[dict[str, Any]] = None
    auto = None
    vision = gated.route_for("vision") if images and not body.endpoint_id else None
    if vision is not None:
        chain = gated.chain_from(vision)
        route_meta = {"mode": "task", "kind": "vision", "endpoint_id": vision.id}
    elif body.route == "auto":
        state = body.route_state
        auto = (
            # 判定输入优先用前端回传的原始问题：最后一条消息里混着任务/附件/
            # 模式指令块，既偏置难度又浪费判定 token（旧客户端无此字段时回落）。
            (body.route_question or _latest_user_text(body)).strip(),
            (body.route_context or "").strip(),
            state.difficulty if state else None,
            state.endpoint_id if state else None,
            state.turns if state else 0,
        )
    elif body.endpoint_id:
        endpoint = gated.find(body.endpoint_id)
        if endpoint is None:
            if config.find(body.endpoint_id) is not None:
                raise ApiError(
                    429,
                    "BUDGET_EXCEEDED",
                    "本月预估费用已达预算上限，该付费接口已暂停；可换用本地/免费接口，"
                    "或在设置中心「用量监控」调整预算",
                )
            raise ApiError(404, "LLM_ENDPOINT_NOT_FOUND", "选中的接口已被删除，请重新选择模型")
        chain = gated.chain_from(endpoint)

    return PreparedCall(
        gated=gated,
        messages=messages,
        images=images,
        model=body.model,
        use_stream=use_stream,
        chain=chain,
        route_meta=route_meta,
        auto=auto,
    )


def _resolve_chain(
    prepared: PreparedCall, record: UsageHook
) -> tuple[Optional[list[LlmEndpoint]], Optional[dict[str, Any]]]:
    """Auto 模式在此做难度判定（会出网一次，记 route 用量）；其余形态直接返回。"""
    if prepared.auto is None:
        return prepared.chain, prepared.route_meta
    question, context, last_difficulty, last_endpoint_id, turns = prepared.auto
    decision = auto_route(
        prepared.gated,
        question,
        context=context,
        last_difficulty=last_difficulty,
        last_endpoint_id=last_endpoint_id,
        turns_since_judge=turns,
        on_usage=lambda outcome: record("route", outcome, None),
    )
    return list(decision.chain), decision.meta()


def _usage_hooks(request: Request, user_id: str):
    """后台可用的记账钩子：用独立会话，SSE 迭代 / 托管线程都在请求依赖清理之后。"""
    session_factory = request.app.state.db.session_factory

    def record_outcome(source: str, outcome: ChatOutcome, route_difficulty: Optional[int]) -> None:
        try:
            with session_factory() as session:
                record_usage(
                    session,
                    user_id=user_id,
                    source=source,
                    outcome=outcome,
                    third_party=is_third_party_host(outcome.endpoint.host),
                    route_difficulty=route_difficulty,
                )
                session.commit()
        except Exception:  # noqa: BLE001 - 用量记账绝不允许影响对话
            logger.exception("chat 用量记账失败")

    def record_stream_done(meta: dict, done: dict) -> None:
        try:
            route = meta.get("route") or {}
            with session_factory() as session:
                record_stream_usage(
                    session,
                    user_id=user_id,
                    source="chat",
                    endpoint_name=str(meta.get("endpoint") or ""),
                    host=str(meta.get("host") or ""),
                    model=str(meta.get("model") or ""),
                    third_party=bool(meta.get("third_party")),
                    fallback_used=bool(meta.get("fallback_used")),
                    usage=dict(done.get("usage") or {}),
                    elapsed_ms=int(done.get("elapsed_ms") or 0),
                    route_difficulty=route.get("difficulty") if isinstance(route, dict) else None,
                )
                session.commit()
        except Exception:  # noqa: BLE001 - 用量记账绝不允许影响流式输出
            logger.exception("chat 流式用量记账失败")

    return record_outcome, record_stream_done


# ── 无状态通道 ────────────────────────────────────────────────────────────


@chat_router.post("")
def chat(
    body: ChatRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    prepared = _prepare_call(body, ctx, db)
    record_outcome, record_stream_done = _usage_hooks(request, ctx.user.id)
    chain, route_meta = _resolve_chain(prepared, record_outcome)

    if not prepared.use_stream:
        outcome = complete_with_fallback(
            prepared.gated, prepared.messages, model=prepared.model, chain=chain, images=prepared.images
        )
        record_outcome("chat", outcome, route_meta.get("difficulty") if route_meta else None)
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
        meta: dict = {}
        for event in stream_events(
            prepared.gated,
            prepared.messages,
            model=prepared.model,
            chain=chain,
            extra_meta=extra_meta,
            images=prepared.images,
        ):
            if event.get("type") == "meta":
                meta = event
            elif event.get("type") == "done":
                record_stream_done(meta, event)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── 托管对话轮（ADR-0016） ────────────────────────────────────────────────


def _hub(request: Request) -> ChatTurnHub:
    return request.app.state.chat_turns


def _ensure_scope_owned(db: Session, ctx: AuthContext, scope_id: str) -> None:
    """任务归属经项目 owner 校验；首页对话（chat_…）没有服务端实体，按 user_id 隔离。"""
    if scope_id.startswith("run_"):
        get_owned_run(db, ctx, scope_id)


def _turn_or_404(view: Optional[dict[str, Any]]) -> dict[str, Any]:
    if view is None:
        raise ApiError(404, "CHAT_TURN_NOT_FOUND", "这轮对话不存在或已删除")
    return view


@chat_router.post("/turns", status_code=202)
def start_chat_turn(
    body: ChatTurnStartRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """发起一轮托管对话：立即返回轮视图，生成在后台进行，页面随后附着直播。

    接口配置 / 预算 / 指定接口的校验同步完成并原样报错（400/404/429）；Auto 难度
    判定与上游调用都在后台线程里，页面此刻关掉也不影响。同一 scope 同时只允许
    一轮在生成（409）。
    """
    _ensure_scope_owned(db, ctx, body.scope_id)
    hub = _hub(request)
    if hub.has_running(ctx.user.id, body.scope_id):
        raise ApiError(409, "CHAT_TURN_IN_PROGRESS", "上一轮回复还在生成中，请等它结束或先暂停")
    prepared = _prepare_call(body, ctx, db)
    record_outcome, record_stream_done = _usage_hooks(request, ctx.user.id)

    # 对话即控制面（ADR-0018）：任务归属的追问先经运行控制步骤——识别「重试 / 用方案 B /
    # 从数据准备重做」这类指令并真正执行，再把运行状态注入系统提示词。上一轮留下的
    # 待确认提案（取消任务）要在这里带过去，下一句「确认」才有所指。
    control: Optional[dict[str, Any]] = None
    if body.scope_id.startswith("run_") and not body.opening and body.text.strip():
        previous = hub.list_scope(db, ctx.user.id, body.scope_id)
        control = {
            "session_factory": request.app.state.db.session_factory,
            "run_id": body.scope_id,
            "user_id": ctx.user.id,
            "actor": ctx.user.email,
            "text": body.text,
            "config": prepared.gated,
            "previous_actions": list((previous[-1].get("meta") or {}).get("actions") or []) if previous else None,
            # 判定模型要看得到上文（ADR-0019）：「就按你说的那个改」得知道「那个」是什么
            "history": [
                {"text": str(turn.get("text") or ""), "reply": str(turn.get("reply") or "")}
                for turn in previous
                if not turn.get("opening") and (turn.get("text") or turn.get("reply"))
            ][-JUDGE_HISTORY_TURNS:],
        }

    def produce() -> Iterator[dict[str, Any]]:
        if control is not None:
            result = run_control_step(
                control["session_factory"],
                run_id=control["run_id"],
                user_id=control["user_id"],
                actor=control["actor"],
                text=control["text"],
                config=control["config"],
                previous_actions=control["previous_actions"],
                on_judge_usage=lambda outcome: record_outcome("route", outcome, None),
                history=control["history"],
            )
            yield from action_events(result)
            if result.prompt_block and prepared.messages and prepared.messages[0]["role"] == "system":
                prepared.messages[0]["content"] = f"{prepared.messages[0]['content']}\n\n{result.prompt_block}"
        chain, route_meta = _resolve_chain(prepared, record_outcome)
        yield from stream_events(
            prepared.gated,
            prepared.messages,
            model=prepared.model,
            chain=chain,
            extra_meta={"route": route_meta} if route_meta else None,
            images=prepared.images,
        )

    view = hub.start(
        user_id=ctx.user.id,
        scope_id=body.scope_id,
        text=body.text if not body.opening else "",
        opening=body.opening,
        attachments=body.attachments,
        persist=bool(privacy_settings_of(ctx.user).get("save_history", True)),
        producer=produce,
        on_done=record_stream_done,
    )
    return {"turn": view}


@chat_router.get("/scopes/{scope_id}/turns")
def list_chat_turns(
    scope_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """scope 下全部轮（升序）：页面重进时重建对话并找出仍在生成的轮续接。"""
    _ensure_scope_owned(db, ctx, scope_id)
    return {"items": _hub(request).list_scope(db, ctx.user.id, scope_id)}


@chat_router.delete("/scopes/{scope_id}", status_code=200)
def delete_chat_turns(
    scope_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """清空 scope 下的对话记录（删除首页对话 / 清除任务对话）；进行中的轮先停。"""
    deleted = _hub(request).delete_scope(db, ctx.user.id, scope_id)
    db.commit()
    return {"deleted": deleted}


@chat_router.get("/turns/{turn_id}")
def get_chat_turn(
    turn_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    return {"turn": _turn_or_404(_hub(request).get(db, ctx.user.id, turn_id))}


@chat_router.get("/turns/{turn_id}/events")
def stream_chat_turn(
    turn_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    after: int = Query(default=0, ge=0),
):
    """附着直播：回放 after 之后的事件并跟随到终态；每个事件带 seq 供再次附着。

    轮已不在内存（缓冲过期 / 进程重启过）时只发一个按定格状态合成的终态事件，
    正文以 GET /turns/{id} 视图为准。
    """
    hub = _hub(request)
    live = hub.live_for(ctx.user.id, turn_id)
    if live is None:
        view = _turn_or_404(hub.get(db, ctx.user.id, turn_id))
        terminal = terminal_event_for(view)
        return StreamingResponse(iter([sse_line(terminal)]), media_type="text/event-stream", headers=SSE_HEADERS)
    settings = request.app.state.settings
    return StreamingResponse(
        hub.stream_live(live, after, settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@chat_router.post("/turns/{turn_id}/stop")
def stop_chat_turn(
    turn_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """「暂停生成」：定格已生成的部分，观众立刻收到终态事件，上游连接随后关闭。"""
    return {"turn": _turn_or_404(_hub(request).stop(db, ctx.user.id, turn_id))}


@chat_router.patch("/turns/{turn_id}")
def patch_chat_turn(
    turn_id: str,
    body: ChatTurnTraceRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """补写页面侧回复轨迹行（附件解析、难度判定、生成耗时……），重进时照样回放。"""
    view = _turn_or_404(_hub(request).set_trace(db, ctx.user.id, turn_id, body.trace))
    db.commit()
    return {"turn": view}
