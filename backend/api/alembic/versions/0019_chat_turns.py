"""服务端托管对话轮（ADR-0016）

Revision ID: 0019_chat_turns
Revises: 0018_run_notes
Create Date: 2026-09-05

对话生成改为后台作业：POST /api/chat/turns 建行，后台线程出网并把 reply/reasoning
增量回写；页面按 scope（run_… / chat_…）拉取并经 SSE 续接直播。scope_id 与 user_id
不设外键（首页对话没有服务端实体；用户删除后行随 privacy 清理）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_chat_turns"
down_revision = "0018_run_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("opening", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=False, server_default=""),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(2000), nullable=True),
        sa.Column("trace", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_chat_turns_scope_created", "chat_turns", ["scope_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_turns_scope_created", table_name="chat_turns")
    op.drop_table("chat_turns")
