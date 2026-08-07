"""登录限速窗口计数表：login_attempts（数据库限速器，多实例一致）

Revision ID: 0004_login_attempts
Revises: 0003_v1_alignment
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_login_attempts"
down_revision = "0003_v1_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_attempts_key_time", "login_attempts", ["key", "attempted_at"])


def downgrade() -> None:
    op.drop_table("login_attempts")
