"""用户偏好：最大并发任务上限

Revision ID: 0009_user_max_concurrent_runs
Revises: 0008_artifact_texts
Create Date: 2026-08-12

高级设置里的并发上限按用户存储、创建任务时在服务端校验；NULL 表示沿用部署默认值。
可空列同时兼容 SQLite 开发库的启动补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_user_max_concurrent_runs"
down_revision = "0008_artifact_texts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("max_concurrent_runs", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "max_concurrent_runs")
