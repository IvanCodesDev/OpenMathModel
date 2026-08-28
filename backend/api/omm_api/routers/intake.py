"""发送前接待判定端点：POST /api/v1/task-intake。

前端首页/确认页在创建 Project 与 TaskRun **之前**调用本端点；
"modeling_task" 才继续现有创建链路，其余意图由前端原地展示 reply。
既有 POST /v1/task-runs 契约不受影响（直接调用方跳过接待属于合法用法，
题面无效时由问题分析节点的 viability 门兜底）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api_models import TaskIntakeInput, TaskIntakeResult
from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..intake import IntakeAttachment, decide_intake
from ..llm import is_third_party_host, parse_llm_config
from ..usage import record_usage

router = APIRouter(prefix="/v1/task-intake", tags=["task-intake"])


@router.post("", response_model=TaskIntakeResult)
def create_task_intake(
    body: TaskIntakeInput,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TaskIntakeResult:
    config = parse_llm_config(ctx.user.llm_config)
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
    )
    db.commit()  # 判定调用的用量记账与本次请求一起落库
    return TaskIntakeResult(
        intent=decision.intent,  # type: ignore[arg-type]
        reply=decision.reply,
        source=decision.source,  # type: ignore[arg-type]
    )
