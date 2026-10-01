"""真实产物的展示名改回真实文件名

Revision ID: 0021_artifact_display_names
Revises: 0020_chat_turn_feedback
Create Date: 2026-09-27

产物投影曾按 kind 套用模拟链路的中文名，真实实验画的每张图都登记成「基线实验结果图（模拟）」，
成果页、交付清单与执行轨迹的「写入 …」行里全是「模拟」。投影已改为按 (kind, 文件名) 精确匹配；
本迁移把存量里误标的行改回内容 URI 尾部的真实文件名：artifacts.name 与 artifact.published
事件载荷里的 name 同步修正。模拟链路自己的文件（baseline-metrics.svg / report-draft.md）不动。
纯数据修正、可重复执行；downgrade 不回写错误的名字。
"""

import json

from alembic import op
import sqlalchemy as sa

revision = "0021_artifact_display_names"
down_revision = "0020_chat_turn_feedback"
branch_labels = None
depends_on = None

#: 模拟链路的展示名 → 该名字唯一合法对应的文件名。
_SIM_FILES = {
    "基线实验结果图（模拟）": "baseline-metrics.svg",
    "建模报告草稿（模拟）": "report-draft.md",
}


def _uri_tail(uri: object) -> str:
    return str(uri or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].strip()


def _corrected(name: object, uri: object) -> str | None:
    sim_file = _SIM_FILES.get(str(name or ""))
    if sim_file is None:
        return None
    tail = _uri_tail(uri)
    if not tail or tail == sim_file:
        return None
    return tail


def upgrade() -> None:
    bind = op.get_bind()

    artifacts = sa.table(
        "artifacts",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("uri", sa.String),
    )
    rows = bind.execute(
        sa.select(artifacts.c.id, artifacts.c.name, artifacts.c.uri).where(
            artifacts.c.name.in_(list(_SIM_FILES))
        )
    ).all()
    for row in rows:
        name = _corrected(row.name, row.uri)
        if name is not None:
            bind.execute(artifacts.update().where(artifacts.c.id == row.id).values(name=name))

    events = sa.table(
        "agent_events",
        sa.column("id", sa.String),
        sa.column("type", sa.String),
        sa.column("payload", sa.JSON),
    )
    rows = bind.execute(
        sa.select(events.c.id, events.c.payload).where(events.c.type == "artifact.published")
    ).all()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload or "{}")
        name = _corrected(payload.get("name"), payload.get("uri"))
        if name is not None:
            bind.execute(
                events.update().where(events.c.id == row.id).values(payload={**payload, "name": name})
            )


def downgrade() -> None:
    pass
