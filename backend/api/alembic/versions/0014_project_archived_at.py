"""项目归档时间

Revision ID: 0014_project_archived_at
Revises: 0013_user_privacy_settings
Create Date: 2026-08-16

侧栏「最近任务」的归档语义：archived_at 非空 = 已归档，默认列表不再返回。
归档状态不进 v1 Project 载荷，由列表查询的 archived 过滤参数表达。
可空列兼容 SQLite 补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0014_project_archived_at"
down_revision = "0013_user_privacy_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "archived_at")
