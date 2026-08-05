"""v1 契约对齐：projects.owner、task_runs.params/failure_message、step_runs.created_at、幂等记录表

Revision ID: 0003_v1_alignment
Revises: 0002_auth_tables
Create Date: 2026-08-05

与 omm_api/orm.py 保持一致；SQLite 开发环境用 create_all，本迁移面向 PostgreSQL 部署。
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_v1_alignment"
down_revision = "0002_auth_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("owner", sa.String(200), nullable=False, server_default="local-dev"),
    )
    op.add_column("task_runs", sa.Column("params", sa.JSON))
    op.add_column("task_runs", sa.Column("failure_message", sa.String(4000)))
    op.add_column(
        "step_runs",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("response", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_column("step_runs", "created_at")
    op.drop_column("task_runs", "failure_message")
    op.drop_column("task_runs", "params")
    op.drop_column("projects", "owner")
