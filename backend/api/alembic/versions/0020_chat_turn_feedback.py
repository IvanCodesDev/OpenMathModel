"""对话轮的用户评价（赞 / 踩）

Revision ID: 0020_chat_turn_feedback
Revises: 0019_chat_turns
Create Date: 2026-09-07

回复右下角的赞 / 踩按钮落在这一轮对话上（PUT /api/chat/turns/{id}/feedback），
刷新、重进页面都保留，后续可按轮统计。up / down 两个值；NULL = 未评价或已撤回。
可空列兼容 SQLite 补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_chat_turn_feedback"
down_revision = "0019_chat_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_turns", sa.Column("feedback", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_turns", "feedback")
