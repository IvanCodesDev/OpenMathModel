"""E6 看板批次导出：从控制面数据库取一批运行，按事件日志算运行级报告并落到 runs/ 分区。

设计 §14.2「报告落 runs/ 分区」。用法（仓库根，使用项目 venv 的 Python；数据库沿用
backend/api 的配置，即 OMM_DATABASE_URL 或其缺省）：
    python tools/e6-report.py --run run_xxx --run run_yyy
    python tools/e6-report.py --since 2026-09-20
    python tools/e6-report.py --since 2026-09-20 --baseline runs/e6/20260920-100000/batch.json

输出目录缺省 runs/e6/<UTC 时间戳>/：逐运行 <run_id>.json + <run_id>.md、batch.json + batch.md，
给了 --baseline（上一批次的 batch.json）时另写 drift.json。只读数据库、不改任何运行；费用按模型目录
缓存文件里精确收录的美元单价计算，与 GET /api/v1/task-runs/{run_id}/metrics 同一口径。
一条领域事件都没有的运行（早于事件日志的老运行）不进批次，在 stderr 点名。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export E6 run reports into a runs/ partition.")
    parser.add_argument("--run", action="append", default=[], help="运行 id，可重复")
    parser.add_argument("--since", help="只取该日期（UTC，YYYY-MM-DD）之后创建的运行")
    parser.add_argument("--all", action="store_true", help="库里全部运行")
    parser.add_argument("--out", type=Path, help="输出目录，缺省 runs/e6/<UTC 时间戳>")
    parser.add_argument("--baseline", type=Path, help="上一批次的 batch.json，给了就写 drift.json")
    args = parser.parse_args(argv)
    if not (args.run or args.since or args.all):
        parser.error("至少给一个 --run、--since 或 --all")
    return args


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    from omm_api import model_catalog
    from omm_api.config import get_settings
    from omm_api.db import Database
    from omm_api.orm import TaskRunRow
    from omm_api.run_metrics import export_run_reports, runs_without_events

    settings = get_settings()
    # 只读缓存文件拿单价，不起后台同步线程、不出网
    model_catalog.set_current(model_catalog.ModelCatalog(settings))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or REPO_ROOT / "runs" / "e6" / stamp

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            query = select(TaskRunRow).order_by(TaskRunRow.created_at.asc())
            if args.run:
                query = query.where(TaskRunRow.id.in_(args.run))
            if args.since:
                since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                query = query.where(TaskRunRow.created_at >= since)
            runs = list(session.execute(query).scalars())
            missing = sorted(set(args.run) - {run.id for run in runs})
            if missing:
                print(f"E6_REPORT_MISSING_RUNS {json.dumps(missing)}", file=sys.stderr)
            empty = runs_without_events(session, [run.id for run in runs])
            if empty:
                print(f"E6_REPORT_SKIPPED_NO_EVENTS {json.dumps(sorted(empty))}", file=sys.stderr)
                runs = [run for run in runs if run.id not in empty]
            if not runs:
                print("E6_REPORT_EMPTY 没有符合条件的运行", file=sys.stderr)
                return 2
            batch = export_run_reports(session, runs, out_dir, baseline=baseline)
    finally:
        database.dispose()
    summary = {
        "out": out_dir.as_posix(),
        "runs": batch["runs"],
        "outcomes": batch["outcomes"],
        "total_tokens": batch["totals"]["total_tokens"],
        "cost_usd": batch["totals"]["cost_usd"],
    }
    print(f"E6_REPORT_OK {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
