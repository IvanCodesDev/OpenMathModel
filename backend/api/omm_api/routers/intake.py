"""发送前接待判定端点：POST /api/v1/task-intake（ADR-0024）。

前端首页/确认页在创建 Project 与 TaskRun **之前**调用本端点：route=start 才继续现有
创建链路（goal 用 task_goal），propose / clarify / reply 由首页对话原地回应。
既有 POST /v1/task-runs 契约不受影响（直接调用方跳过接待属于合法用法，
题面无效时由问题分析节点的 viability 门兜底）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api_models import TaskIntakeInput, TaskIntakeResult, TaskIntakeUnderstanding
from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..intake import IntakeAttachment, IntakeTurn, PendingTask, decide_intake
from ..llm import config_usable, is_third_party_host, parse_llm_config
from ..usage import record_usage

router = APIRouter(prefix="/v1/task-intake", tags=["task-intake"])


@router.post("", response_model=TaskIntakeResult)
def create_task_intake(
    body: TaskIntakeInput,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TaskIntakeResult:
    config = parse_llm_config(ctx.user.llm_config)
    pending = body.pending_task
    decision = decide_intake(
        config,
        body.goal,
        body.has_attachments,
        attachments=[
            IntakeAttachment(name=item.name, excerpt=item.excerpt, characters=item.characters)
            for item in body.attachments
        ],
        on_usage=lambda outcome: record_usage(
            db,
            user_id=ctx.user.id,
            source="chat",
            outcome=outcome,
            third_party=is_third_party_host(outcome.endpoint.host),
        ),
        history=[IntakeTurn(role=turn.role, text=turn.text) for turn in body.history],
        pending_task=PendingTask(goal=pending.goal, domain=pending.domain) if pending else None,
        confirmed=body.confirmed,
    )
    db.commit()  # 判定调用的用量记账与本次请求一起落库
    understanding = decision.understanding
    return TaskIntakeResult(
        intent=decision.intent,  # type: ignore[arg-type]
        reply=decision.reply,
        source=decision.source,  # type: ignore[arg-type]
        route=decision.route,  # type: ignore[arg-type]
        understanding=TaskIntakeUnderstanding(
            kind=understanding.kind,  # type: ignore[arg-type]
            speech_act=understanding.speech_act,  # type: ignore[arg-type]
            execution=understanding.execution,
            requires_modeling=understanding.requires_modeling,
            continuation=understanding.continuation,  # type: ignore[arg-type]
            domain=understanding.domain,  # type: ignore[arg-type]
            missing=list(understanding.missing),  # type: ignore[arg-type]
            confidence=round(understanding.confidence, 2),
        ),
        task_goal=decision.task_goal,
        context_excerpt=decision.context_excerpt,
        chat_ready=config_usable(config),
    )
