"""设置中心「用量监控」的服务端实现：调用记录、月度聚合、费用估算与预算闸门。

记录面：四个出网点（/api/chat 非流式与流式、/api/llm/test、engine_glue 的
LLM 节点、Auto 路由的难度判定）在调用成功后各自把一条 LlmUsageRow 写进
llm_usage_records；失败调用不记（用量监控回答"用了多少"，故障排查归日志）。

费用是估算值：按模型名前缀匹配一张常见模型的人民币单价表（元 / 百万 token），
未匹配的模型走 DEFAULT_PRICING。页面文案已声明"费用为本月预估值"。

预算闸门：users.usage_settings 存月度预算三项（预算金额 / 提醒阈值 / 硬限制）。
硬限制开启且本月估算费用达到预算时，enforce_budget 把付费接口从配置里筛掉，
只留本地与免费接口；一个都不剩时抛 BUDGET_EXCEEDED。闸门在服务端，改
localStorage 绕不过（与 max_concurrent_runs 的裁决一致）。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import ApiError
from .llm import ChatOutcome, LlmConfig, LlmEndpoint
from .models import User, utcnow
from .orm import LlmUsageRow, ProjectRow, TaskRunRow

# ── 费用估算（元 / 百万 token，(输入, 输出)）───────────────────────────────
#
# 近似单价，用于预估展示，不是计费依据：厂商随时调价，且中转站各有倍率。
# 匹配规则：小写模型名"包含"键即命中，先匹配的先生效（列表有序，具体在前）。

PRICING: tuple[tuple[str, float, float], ...] = (
    # DeepSeek
    ("deepseek-reasoner", 4.0, 16.0),
    ("deepseek-r1", 4.0, 16.0),
    ("deepseek-chat", 2.0, 8.0),
    ("deepseek-v3", 2.0, 8.0),
    ("deepseek", 2.0, 8.0),
    # OpenAI（按 ≈7 元/美元折算）
    ("gpt-5", 15.0, 60.0),
    ("gpt-4.1-mini", 3.0, 12.0),
    ("gpt-4.1", 14.0, 56.0),
    ("gpt-4o-mini", 1.1, 4.4),
    ("gpt-4o", 18.0, 70.0),
    ("o3", 15.0, 60.0),
    ("o1", 105.0, 420.0),
    # Anthropic
    ("claude-3-5-haiku", 5.6, 28.0),
    ("claude", 21.0, 105.0),
    # Google
    ("gemini-2.0-flash", 0.7, 2.8),
    ("gemini-2.5-flash", 2.1, 17.5),
    ("gemini-2.5-pro", 8.8, 70.0),
    ("gemini", 2.1, 17.5),
    # 阿里通义
    ("qwen-max", 2.4, 9.6),
    ("qwen-plus", 0.8, 2.0),
    ("qwen-turbo", 0.3, 0.6),
    ("qwen", 0.8, 2.0),
    # 智谱 / 月之暗面 / 其他常见
    ("glm-4-flash", 0.0, 0.0),
    ("glm", 5.0, 15.0),
    ("kimi", 12.0, 12.0),
    ("moonshot", 12.0, 12.0),
    ("grok", 21.0, 105.0),
    ("mistral", 14.0, 42.0),
    # 本地推理：无 API 费用
    ("ollama", 0.0, 0.0),
    ("llama", 0.0, 0.0),
    ("qwen3:", 0.0, 0.0),
)

#: 未匹配任何前缀时的兜底单价（元 / 百万 token）。
DEFAULT_PRICING: tuple[float, float] = (4.0, 12.0)

#: 本机推理地址：这些主机上的调用一律按免费估算。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"})

#: usage_settings 缺省值：未设置预算时不提醒、不限制。
DEFAULT_USAGE_SETTINGS: dict = {
    "monthly_budget_cny": None,
    "budget_threshold_percent": 80,
    "hard_limit": False,
}

_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def model_pricing(model: str) -> tuple[float, float]:
    """模型名 → (输入单价, 输出单价)，元 / 百万 token；未知模型走兜底价。"""
    name = (model or "").lower()
    for prefix, prompt_price, completion_price in PRICING:
        if prefix in name:
            return prompt_price, completion_price
    return DEFAULT_PRICING


def estimate_cost_cny(model: str, prompt_tokens: int, completion_tokens: int, host: str = "") -> float:
    """一次调用的估算费用（元）：本机主机恒为 0，其余按单价表折算。"""
    if (host or "").lower() in _LOCAL_HOSTS:
        return 0.0
    prompt_price, completion_price = model_pricing(model)
    return prompt_tokens / 1_000_000 * prompt_price + completion_tokens / 1_000_000 * completion_price


def is_free_endpoint(endpoint: LlmEndpoint) -> bool:
    """预算硬限制下仍可使用的接口：本地协议、本机主机或单价为零的模型。"""
    if endpoint.protocol == "ollama" or endpoint.host.lower() in _LOCAL_HOSTS:
        return True
    return model_pricing(endpoint.model) == (0.0, 0.0)


# ── 记录 ────────────────────────────────────────────────────────────────────


def record_usage(
    session: Session,
    *,
    user_id: str,
    source: str,
    outcome: ChatOutcome,
    third_party: bool,
    run_id: str | None = None,
    route_difficulty: int | None = None,
) -> LlmUsageRow:
    """把一次成功的模型调用记入 llm_usage_records（不 commit，随调用方事务）。

    route_difficulty 只在 Auto 路由的回答调用上有值：为路由校准提供离线
    数据（按难度统计模型分布与误判率）。
    """
    row = LlmUsageRow(
        user_id=user_id,
        source=source,
        run_id=run_id,
        endpoint_name=outcome.endpoint.name[:120],
        host=outcome.endpoint.host[:255],
        model=(outcome.model or outcome.endpoint.model)[:120],
        third_party=third_party,
        fallback_used=outcome.fallback_used,
        prompt_tokens=int(outcome.usage.get("prompt_tokens") or 0),
        completion_tokens=int(outcome.usage.get("completion_tokens") or 0),
        elapsed_ms=int(outcome.elapsed_ms or 0),
        route_difficulty=route_difficulty,
        created_at=utcnow(),
    )
    session.add(row)
    return row


def record_stream_usage(
    session: Session,
    *,
    user_id: str,
    source: str,
    endpoint_name: str,
    host: str,
    model: str,
    third_party: bool,
    fallback_used: bool,
    usage: dict,
    elapsed_ms: int,
    route_difficulty: int | None = None,
) -> LlmUsageRow:
    """流式调用的记录入口：字段来自 meta / done 两个 SSE 事件。"""
    row = LlmUsageRow(
        user_id=user_id,
        source=source,
        run_id=None,
        endpoint_name=(endpoint_name or "")[:120],
        host=(host or "")[:255],
        model=(model or "")[:120],
        third_party=third_party,
        fallback_used=fallback_used,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        elapsed_ms=int(elapsed_ms or 0),
        route_difficulty=route_difficulty,
        created_at=utcnow(),
    )
    session.add(row)
    return row


# ── 月度范围与设置 ──────────────────────────────────────────────────────────


def parse_month(value: str | None) -> tuple[int, int]:
    """查询参数 month=YYYY-MM → (年, 月)；缺省取当前 UTC 月，非法则 422。"""
    if value is None or value == "":
        now = utcnow()
        return now.year, now.month
    match = _MONTH_PATTERN.match(value)
    if match is None:
        raise ApiError(422, "VALIDATION_ERROR", "month 参数格式应为 YYYY-MM，例如 2026-08")
    return int(match.group(1)), int(match.group(2))


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """[月初, 下月初) 的半开区间；存储为无时区 UTC，与 utcnow 同基准。"""
    start = datetime(year, month, 1)
    next_start = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return start, next_start


def usage_settings_of(user: User) -> dict:
    """用户的预算设置：未设置的键回落缺省值（历史行可能只存了部分键）。"""
    stored = user.usage_settings if isinstance(user.usage_settings, dict) else {}
    merged = dict(DEFAULT_USAGE_SETTINGS)
    merged.update({k: stored[k] for k in DEFAULT_USAGE_SETTINGS if k in stored})
    return merged


# ── 聚合 ────────────────────────────────────────────────────────────────────


@dataclass
class MonthUsage:
    """一个自然月的聚合结果（供 /summary 与预算闸门共用）。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _month_rows(session: Session, user_id: str, year: int, month: int) -> list[LlmUsageRow]:
    start, next_start = month_bounds(year, month)
    return list(
        session.scalars(
            select(LlmUsageRow)
            .where(
                LlmUsageRow.user_id == user_id,
                LlmUsageRow.created_at >= start,
                LlmUsageRow.created_at < next_start,
            )
            .order_by(LlmUsageRow.created_at.asc())
        )
    )


def _row_cost(row: LlmUsageRow) -> float:
    return estimate_cost_cny(row.model, row.prompt_tokens, row.completion_tokens, row.host)


def month_usage(session: Session, user_id: str, year: int, month: int) -> MonthUsage:
    """月度合计：预算闸门与「较上月」对比都用它（一次全表扫描仅限该用户当月行）。"""
    total = MonthUsage()
    for row in _month_rows(session, user_id, year, month):
        total.requests += 1
        total.prompt_tokens += row.prompt_tokens
        total.completion_tokens += row.completion_tokens
        total.cost_cny += _row_cost(row)
    return total


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def usage_summary(session: Session, user: User, year: int, month: int) -> dict:
    """/api/usage/summary 的响应体：合计、上月对比、Agent 任务数、14 天序列、模型分布与预算。"""
    rows = _month_rows(session, user.id, year, month)
    start, next_start = month_bounds(year, month)

    totals = MonthUsage()
    by_model: dict[str, MonthUsage] = {}
    by_day: dict[str, MonthUsage] = {}
    agent_run_ids: set[str] = set()
    for row in rows:
        cost = _row_cost(row)
        for bucket in (
            totals,
            by_model.setdefault(row.model or "未知模型", MonthUsage()),
            by_day.setdefault(row.created_at.date().isoformat(), MonthUsage()),
        ):
            bucket.requests += 1
            bucket.prompt_tokens += row.prompt_tokens
            bucket.completion_tokens += row.completion_tokens
            bucket.cost_cny += cost
        if row.source == "agent" and row.run_id:
            agent_run_ids.add(row.run_id)

    # 14 天序列：终点取「当月最后一天」与「今天」的较早者，跨月查询也有稳定窗口。
    today = utcnow().date()
    window_end = min(next_start.date() - timedelta(days=1), today) if start.date() <= today else next_start.date() - timedelta(days=1)
    daily = []
    for offset in range(13, -1, -1):
        day = window_end - timedelta(days=offset)
        bucket = by_day.get(day.isoformat(), MonthUsage())
        daily.append(
            {
                "date": day.isoformat(),
                "requests": bucket.requests,
                "total_tokens": bucket.total_tokens,
                "estimated_cost_cny": round(bucket.cost_cny, 4),
            }
        )

    previous = month_usage(session, user.id, *_previous_month(year, month))

    # Agent 任务数：按项目归属统计当月创建的运行；「真实模型」= 当月产生过 agent 用量的运行。
    agent_total = int(
        session.scalar(
            select(func.count())
            .select_from(TaskRunRow)
            .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
            .where(
                ProjectRow.owner == user.id,
                TaskRunRow.created_at >= start,
                TaskRunRow.created_at < next_start,
            )
        )
        or 0
    )

    settings = usage_settings_of(user)
    budget = settings["monthly_budget_cny"]
    used = round(totals.cost_cny, 2)
    percent = int(used / budget * 100) if budget else 0
    models = sorted(by_model.items(), key=lambda item: item[1].total_tokens, reverse=True)

    return {
        "month": f"{year:04d}-{month:02d}",
        "range": {"start": start.date().isoformat(), "end": (next_start.date() - timedelta(days=1)).isoformat()},
        "totals": {
            "requests": totals.requests,
            "prompt_tokens": totals.prompt_tokens,
            "completion_tokens": totals.completion_tokens,
            "total_tokens": totals.total_tokens,
            "estimated_cost_cny": used,
        },
        "previous": {
            "total_tokens": previous.total_tokens,
            "estimated_cost_cny": round(previous.cost_cny, 2),
        },
        "agent_runs": {"total": agent_total, "llm": len(agent_run_ids)},
        "daily": daily,
        "models": [
            {
                "model": model,
                "requests": bucket.requests,
                "prompt_tokens": bucket.prompt_tokens,
                "completion_tokens": bucket.completion_tokens,
                "total_tokens": bucket.total_tokens,
                "estimated_cost_cny": round(bucket.cost_cny, 2),
            }
            for model, bucket in models
        ],
        "budget": {
            "monthly_budget_cny": budget,
            "budget_threshold_percent": settings["budget_threshold_percent"],
            "hard_limit": bool(settings["hard_limit"]),
            "used_cny": used,
            "remaining_cny": round(budget - used, 2) if budget else None,
            "used_percent": percent,
            "alert": bool(budget) and percent >= int(settings["budget_threshold_percent"] or 100),
        },
    }


def usage_csv(session: Session, user: User, year: int, month: int) -> str:
    """导出明细 CSV（带 BOM 供 Excel 直接打开），行序为时间倒序。"""
    rows = _month_rows(session, user.id, year, month)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        ["时间(UTC)", "来源", "模型", "接口", "主机", "第三方中转", "使用备用",
         "输入tokens", "输出tokens", "合计tokens", "耗时ms", "预估费用(元)", "任务运行ID"]
    )
    source_labels = {"chat": "对话", "agent": "Agent任务", "test": "连接测试", "route": "路由判定"}
    for row in reversed(rows):
        writer.writerow(
            [
                row.created_at.isoformat(sep=" ", timespec="seconds"),
                source_labels.get(row.source, row.source),
                row.model,
                row.endpoint_name,
                row.host,
                "是" if row.third_party else "否",
                "是" if row.fallback_used else "否",
                row.prompt_tokens,
                row.completion_tokens,
                row.prompt_tokens + row.completion_tokens,
                row.elapsed_ms,
                f"{_row_cost(row):.4f}",
                row.run_id or "",
            ]
        )
    return "\ufeff" + buffer.getvalue()


# ── 预算闸门 ────────────────────────────────────────────────────────────────


def budget_exhausted(session: Session, user: User) -> bool:
    """硬限制开启且本月估算费用已达预算 → True（未设预算或未开硬限制恒为 False）。"""
    settings = usage_settings_of(user)
    budget = settings["monthly_budget_cny"]
    if not settings["hard_limit"] or not budget:
        return False
    now = utcnow()
    return month_usage(session, user.id, now.year, now.month).cost_cny >= float(budget)


def enforce_budget(session: Session, user: User, config: LlmConfig) -> LlmConfig:
    """预算硬限制的执行点：达到预算后只保留本地/免费接口，全是付费接口则拒绝。

    对应设置项文案「达到预算后暂停付费模型：保留本地模型与免费额度」。
    """
    if not budget_exhausted(session, user):
        return config
    free = tuple(endpoint for endpoint in config.endpoints if is_free_endpoint(endpoint))
    if not free:
        raise ApiError(
            429,
            "BUDGET_EXCEEDED",
            "本月预估费用已达预算上限，付费模型已暂停；可在设置中心「用量监控」调高预算或关闭硬限制",
        )
    return replace(config, endpoints=free)
