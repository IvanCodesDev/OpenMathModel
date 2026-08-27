"""五类页面正文投影读取端点：数据准备/建模方案/实验与验证/论文编辑/最终成果。

正文来自 ``run_domain_events`` 的 STEP_SUCCEEDED 输出（六阶段真实节点的最新
成功产出），只读、不改变运行状态；鉴权与 GET /v1/task-runs/{run_id} 一致
（按运行归属校验，非本人或不存在一律 404）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..api_models import StageOutputs
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..stage_outputs import build_stage_outputs
from .task_runs import get_owned_run

router = APIRouter(prefix="/v1/task-runs", tags=["stage-outputs"])


@router.get("/{run_id}/stage-outputs", response_model=StageOutputs)
def get_stage_outputs(
    run_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> StageOutputs:
    run = get_owned_run(session, ctx, run_id)
    return build_stage_outputs(session, run, request.app.state.blobs)
