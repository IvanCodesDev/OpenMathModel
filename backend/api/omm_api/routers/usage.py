"""设置中心「用量监控」：月度汇总、明细导出与预算设置。

数据来自 llm_usage_records（四个出网点在调用成功后写入，见 omm_api.usage 模块
说明）；费用为按单价表的估算值，页面文案已声明。预算设置存 users.usage_settings，
硬限制在服务端聊天与任务执行路径上把关。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..schemas import UsageSettingsUpdateRequest
from ..usage import parse_month, usage_csv, usage_settings_of, usage_summary

router = APIRouter(tags=["usage"])


@router.get("/summary")
def get_usage_summary(
    month: str | None = Query(default=None, description="YYYY-MM，缺省为当前月"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """月度用量汇总：合计、上月对比、Agent 任务数、近 14 天序列、模型分布与预算状态。"""
    year, month_number = parse_month(month)
    return usage_summary(db, ctx.user, year, month_number)


@router.get("/export")
def export_usage(
    month: str | None = Query(default=None, description="YYYY-MM，缺省为当前月"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    """导出当月调用明细 CSV（带 BOM，Excel 可直接打开）。"""
    year, month_number = parse_month(month)
    content = usage_csv(db, ctx.user, year, month_number)
    filename = f"openmathmodel-usage-{year:04d}-{month_number:02d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/settings")
def get_usage_settings(ctx: AuthContext = Depends(get_auth_context)):
    return {"settings": usage_settings_of(ctx.user)}


@router.put("/settings")
def update_usage_settings(
    body: UsageSettingsUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """整体替换保存三个预算项。预算与硬限制存服务端而不是浏览器：
    暂停付费模型的闸门在聊天与任务执行路径上校验，改本机缓存绕不过。"""
    ctx.user.usage_settings = {
        "monthly_budget_cny": body.monthly_budget_cny,
        "budget_threshold_percent": body.budget_threshold_percent,
        "hard_limit": body.hard_limit,
    }
    db.commit()
    return {"settings": usage_settings_of(ctx.user)}
