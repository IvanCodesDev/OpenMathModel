"""附件正文抽取缓存表

Revision ID: 0008_artifact_texts
Revises: 0007_user_avatar
Create Date: 2026-08-12

产物是内容寻址的，同一个 artifact 的字节永远不变，因此正文抽一次可以长期复用。
失败与不支持的结果也要落库，否则每次读取都会重跑一遍注定失败的解析。
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_artifact_texts"
down_revision = "0007_user_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_texts",
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("engine", sa.String(40), nullable=False),
        sa.Column("characters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segments", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("artifact_texts")
