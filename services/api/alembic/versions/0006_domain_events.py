"""领域事件日志表：agents/core 引擎的执行事实来源（B2 换脑）

Revision ID: 0006_domain_events
Revises: 0005_email_codes
Create Date: 2026-08-05

与 omm_api/orm.py 保持一致；SQLite 开发环境用 create_all，本迁移面向 PostgreSQL 部署。
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_domain_events"
down_revision = "0005_email_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_domain_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_domain_events_run_seq"),
    )
    op.create_index("ix_run_domain_events_run_id", "run_domain_events", ["run_id"])
    op.create_index("ix_run_domain_events_run_seq", "run_domain_events", ["run_id", "seq"])


def downgrade() -> None:
    op.drop_table("run_domain_events")
