"""用户头像：users 表新增内容寻址引用列

Revision ID: 0007_user_avatar
Revises: 0006_domain_events
Create Date: 2026-08-12

与 omm_api/models.py 保持一致；SQLite 开发环境用 create_all，本迁移面向 PostgreSQL 部署。
头像二进制存放在内容寻址存储中，库内只保留 sha256 与服务端识别出的媒体类型。
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_user_avatar"
down_revision = "0006_domain_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_sha256", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("avatar_media_type", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_media_type")
    op.drop_column("users", "avatar_sha256")
