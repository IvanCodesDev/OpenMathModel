"""用户自定义模型接口配置

Revision ID: 0010_user_llm_config
Revises: 0009_user_max_concurrent_runs
Create Date: 2026-08-13

设置中心「自定义 API」的已保存接口、主接口与行为开关按用户存 JSON；
对话回复与任务执行都在服务端按该配置调用模型。可空列兼容 SQLite 补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_user_llm_config"
down_revision = "0009_user_max_concurrent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("llm_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "llm_config")
