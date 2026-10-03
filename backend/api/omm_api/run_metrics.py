"""E6 看板的控制面接线（设计 §14.2、§16 C10）：一条运行的事件日志 → harness 聚合器的运行级报告。

- 领域事件取 ``run_domain_events``（执行事实来源），过程事件取 ``agent_events`` 里的 run.log
  （模型调用、子代理审计、预算硬限等，与预算账本同一数据源）；报告按需现算、不入库——事件日志是
  唯一事实来源，派生报告存下来会过期（回退重做后数字就变了）。
- 费用只用模型目录里精确命中的美元单价（``ModelCatalog.pricing_usd``）；目录没收录的型号由聚合器
  列进 ``llm.unpriced_models``，一个都没收录就不给单价、费用记 None。
  ``usage.model_pricing`` 的兜底价是设置中心估算用的，E6 口径不拿别的型号的价格冒充。
- 批次导出 ``export_run_reports``：逐运行 json + markdown 与批次汇总落到 runs/ 分区（§14.2「报告落
  runs/ 分区」），可选对照上一批次写漂移；由 ``tools/e6-report.py`` 按需调用，不在执行路径里自动写。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from omm_agent_harness.metrics import (
    aggregate_batch,
    aggregate_run,
    compare_reports,
    render_batch_markdown,
    render_markdown,
)
from omm_contracts import AgentEventType
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import model_catalog
from .orm import AgentEventRow, DomainEventRow, TaskRunRow


def _domain_events(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(DomainEventRow)
        .where(DomainEventRow.run_id == run_id)
        .order_by(DomainEventRow.seq.asc())
    ).scalars()
    return [
        {
            "event_type": row.event_type,
            "payload": row.payload or {},
            "seq": row.seq,
            "created_at": row.created_at,
            "run_id": run_id,
        }
        for row in rows
    ]


def _process_events(session: Session, run_id: str) -> list[Mapping[str, Any]]:
    rows = session.execute(
        select(AgentEventRow.payload)
        .where(
            AgentEventRow.run_id == run_id,
            AgentEventRow.type == AgentEventType.run_log.value,
        )
        .order_by(AgentEventRow.sequence.asc())
    ).scalars()
    return [payload for payload in rows if isinstance(payload, Mapping)]


def catalog_prices(
    process_events: Iterable[Mapping[str, Any]],
) -> Optional[dict[str, dict[str, float]]]:
    """本运行用到的型号里，模型目录精确收录的美元单价（USD / 1M tokens）。

    目录不在或一个都没收录 → None。
    """
    catalog = model_catalog.current()
    if catalog is None:
        return None
    models = {
        payload.get("model") for payload in process_events if payload.get("kind") == "llm_call"
    }
    prices: dict[str, dict[str, float]] = {}
    for model in sorted(item for item in models if isinstance(item, str) and item):
        priced = catalog.pricing_usd(model)
        if priced is not None:
            prices[model] = {"input": priced[0], "output": priced[1]}
    return prices or None


def run_metrics_report(session: Session, run: TaskRunRow) -> dict[str, Any]:
    """一条运行的 E6 报告：harness ``aggregate_run`` 的分节字典（结构见该函数）。

    运行 id / 项目 id 以运行行补齐：早于领域事件日志的老运行一条事件都没有，聚合器读不到。
    """
    process = _process_events(session, run.id)
    report = aggregate_run(
        _domain_events(session, run.id), process, prices=catalog_prices(process)
    )
    section = report["run"]
    section["run_id"] = run.id
    section["project_id"] = section.get("project_id") or run.project_id
    return report


def runs_without_events(session: Session, run_ids: Iterable[str]) -> set[str]:
    """一条领域事件都没有的运行：报告除了 id 全是空的，结果只能记在途，批次导出不收。"""
    ids = set(run_ids)
    if not ids:
        return set()
    having = session.execute(
        select(DomainEventRow.run_id).where(DomainEventRow.run_id.in_(ids)).distinct()
    ).scalars()
    return ids - set(having)


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def export_run_reports(
    session: Session,
    runs: Sequence[TaskRunRow],
    out_dir: Path,
    *,
    baseline: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """一批运行的 E6 报告落盘：逐运行 ``<run_id>.json`` / ``<run_id>.md``，批次汇总
    ``batch.json`` / ``batch.md``（``aggregate_batch``：计数求和、比率按求和后的分子分母重算）；
    给了上一批次的 ``batch.json`` 内容时再写 ``drift.json``
    （``compare_reports`` 数值叶子逐路径对照，即 §14.2 的 tokens / 时长 / 费用批次漂移）。
    返回批次汇总。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for run in runs:
        report = run_metrics_report(session, run)
        reports.append(report)
        _write_json(out_dir / f"{run.id}.json", report)
        (out_dir / f"{run.id}.md").write_text(render_markdown(report), encoding="utf-8")
    batch = aggregate_batch(reports)
    _write_json(out_dir / "batch.json", batch)
    (out_dir / "batch.md").write_text(render_batch_markdown(batch), encoding="utf-8")
    if baseline is not None:
        _write_json(out_dir / "drift.json", compare_reports(baseline, batch))
    return batch
