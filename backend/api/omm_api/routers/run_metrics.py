"""E6 看板运行级报告端点：``GET /api/v1/task-runs/{run_id}/metrics``。

从事件日志按需现算（``run_metrics.run_metrics_report``），只读、不改变运行状态；鉴权与
stage-outputs 一致（按运行归属，非本人或不存在一律 404）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api_models import RunMetricsReport
from ..db import get_session
from ..deps import AuthContext, get_auth_context
from ..run_metrics import run_metrics_report
from .task_runs import get_owned_run

router = APIRouter(prefix="/v1/task-runs", tags=["run-metrics"])


@router.get("/{run_id}/metrics", response_model=RunMetricsReport)
def get_run_metrics(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: Session = Depends(get_session),
) -> RunMetricsReport:
    run = get_owned_run(session, ctx, run_id)
    return RunMetricsReport.model_validate(run_metrics_report(session, run))
