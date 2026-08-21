"""llm_usage_records 增加 route_difficulty

Revision ID: 0015_llm_usage_route_difficulty
Revises: 0014_project_archived_at
Create Date: 2026-08-21

Auto 路由的判定难度随回答调用的用量记录落库：为路由校准提供离线数据
（按难度统计模型分布、估算误判率、迭代判定提示词与阈值）。
非 Auto 调用恒为 NULL；可空列兼容 SQLite 补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_llm_usage_route_difficulty"
down_revision = "0014_project_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage_records", sa.Column("route_difficulty", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage_records", "route_difficulty")
