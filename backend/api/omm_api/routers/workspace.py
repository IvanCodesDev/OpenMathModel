"""建模流程页首屏快照：同一投影驱动 Agent 左栏和右侧阶段页面。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from omm_contracts import ModelingWorkspaceView

from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..workspace_view import build_modeling_workspace_view
from .task_runs import get_owned_run

router = APIRouter(prefix="/v1/task-runs", tags=["workspace"])


@router.get("/{run_id}/workspace", response_model=ModelingWorkspaceView)
def get_modeling_workspace(
    run_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> ModelingWorkspaceView:
    run = get_owned_run(session, ctx, run_id)
    return build_modeling_workspace_view(session, run, request.app.state.blobs)
