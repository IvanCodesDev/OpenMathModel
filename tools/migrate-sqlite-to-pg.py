"""SQLite 开发库 → PostgreSQL 整库数据迁移（一次性工具，可重复执行）。

用法（仓库根，使用项目 venv 的 Python）：
    python tools/migrate-sqlite-to-pg.py --inspect   # 只对比两库各表行数，不写任何数据
    python tools/migrate-sqlite-to-pg.py --apply     # 清空 PG 业务表后按外键拓扑序整库拷贝，并重置自增序列

前提与行为：
- PG schema 必须已由 Alembic 升到 head；dev.db 的 create_all+补列结果与之一致
  （一致性由 backend/api/tests/test_migrations.py 守护）。缺表时中止并提示。
- --apply 在单个事务内完成 TRUNCATE 与全部插入，失败整体回滚；alembic_version 不受影响。
- 孤儿行过滤：SQLite 默认不强制外键，dev.db 可能存在指向已删除父行的子行
  （如运行被清扫后残留的事件）。这类行在 PostgreSQL 下无法插入且本就是
  不可达数据，迁移时按外键约束逐表过滤并打印数量。
- 时区：orm.py 的 timestamptz 列在 SQLite 里读出为 naive UTC，写入 PG 前统一补
  UTC 时区；models.py 的无时区列保持 naive 原样。否则经 psycopg 写入 timestamptz
  会被按会话时区解释，时间整体漂移。
- Artifact/头像二进制在磁盘上按内容寻址存放，与数据库切换无关，不在本脚本范围。
"""

from __future__ import annotations

import argparse
import sys
from datetime import timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE = f"sqlite:///{(REPO_ROOT / 'backend' / 'api' / 'data' / 'dev.db').as_posix()}"
DEFAULT_PG = "postgresql+psycopg://openmathmodel:openmathmodel@127.0.0.1:5433/openmathmodel"

CHUNK = 500


def load_metadata():
    from omm_api.db import Base
    from omm_api import models, orm  # noqa: F401  注册账户面与任务面全部模型

    return Base.metadata


def row_counts(engine, table_names):
    counts = {}
    with engine.connect() as conn:
        for name in table_names:
            counts[name] = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
    return counts


def cmd_inspect(src, dst, metadata) -> None:
    names = [t.name for t in metadata.sorted_tables]
    src_counts = row_counts(src, names)
    dst_existing = set(inspect(dst).get_table_names())
    dst_counts = row_counts(dst, [n for n in names if n in dst_existing])
    print(f"{'table':32} {'sqlite':>8} {'pg':>8}")
    for name in names:
        pg_val = dst_counts.get(name, "-")
        print(f"{name:32} {src_counts[name]:>8} {pg_val:>8}")


def fix_timezones(table, rows):
    """timestamptz 列补 UTC；无时区列保持 naive。SQLite 读出永远是 naive。"""
    tz_columns = [
        c.name
        for c in table.columns
        if isinstance(c.type, DateTime) and getattr(c.type, "timezone", False)
    ]
    if not tz_columns:
        return rows
    for row in rows:
        for name in tz_columns:
            value = row[name]
            if value is not None and value.tzinfo is None:
                row[name] = value.replace(tzinfo=timezone.utc)
    return rows


def drop_orphans(table, rows, copied_pks):
    """删掉外键指向不存在父行的孤儿行（SQLite 不强制外键，PG 会拒绝）。"""
    own_pk = list(table.primary_key.columns)
    source_pk_set = {r[own_pk[0].name] for r in rows} if len(own_pk) == 1 else set()
    for column in table.columns:
        for fk in column.foreign_keys:
            parent = fk.column.table.name
            if not fk.column.primary_key:
                continue
            parent_set = source_pk_set if parent == table.name else copied_pks.get(parent)
            if parent_set is None:
                continue
            kept = [r for r in rows if r[column.name] is None or r[column.name] in parent_set]
            if len(kept) != len(rows):
                print(
                    f"  dropped {len(rows) - len(kept)} orphan rows in {table.name} "
                    f"(missing {parent}.{fk.column.name})"
                )
            rows = kept
    return rows


def cmd_apply(src, dst, metadata) -> None:
    tables = metadata.sorted_tables
    missing = set(t.name for t in tables) - set(inspect(dst).get_table_names())
    if missing:
        sys.exit(f"PG 缺表 {sorted(missing)}：先执行 alembic upgrade head 再迁移。")

    copied_pks: dict[str, set] = {}
    with src.connect() as sconn, dst.begin() as dconn:
        quoted = ", ".join(f'"{t.name}"' for t in tables)
        dconn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))

        for table in tables:
            rows = [dict(m) for m in sconn.execute(table.select()).mappings()]
            rows = drop_orphans(table, rows, copied_pks)
            pk_columns = list(table.primary_key.columns)
            if len(pk_columns) == 1:
                copied_pks[table.name] = {r[pk_columns[0].name] for r in rows}
            if rows:
                rows = fix_timezones(table, rows)
                for start in range(0, len(rows), CHUNK):
                    dconn.execute(table.insert(), rows[start : start + CHUNK])
            print(f"copied {table.name}: {len(rows)}")

        # 显式主键插入不会推进序列，逐表重置到 MAX(id)+1，否则后续 INSERT 撞主键
        for table in tables:
            for column in table.primary_key.columns:
                if not column.autoincrement or not column.type.python_type is int:
                    continue
                seq = dconn.execute(
                    text("SELECT pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": column.name},
                ).scalar()
                if seq:
                    dconn.execute(
                        text(
                            f'SELECT setval(:seq, COALESCE((SELECT MAX("{column.name}") '
                            f'FROM "{table.name}"), 0) + 1, false)'
                        ),
                        {"seq": seq},
                    )
    print("done.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE)
    parser.add_argument("--pg", default=DEFAULT_PG)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    metadata = load_metadata()
    src = create_engine(args.sqlite)
    dst = create_engine(args.pg)
    try:
        if args.inspect:
            cmd_inspect(src, dst, metadata)
        else:
            cmd_apply(src, dst, metadata)
    finally:
        src.dispose()
        dst.dispose()


if __name__ == "__main__":
    main()
