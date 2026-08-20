"""用户数据与隐私设置

Revision ID: 0013_user_privacy_settings
Revises: 0012_artifact_text_images
Create Date: 2026-08-16

设置中心「数据与隐私」的开关与保留策略按用户存 JSON（privacy.privacy_settings_of
解析）；任务保留与文件缓存清理由服务端后台清扫按此执行。可空列兼容 SQLite 补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_user_privacy_settings"
down_revision = "0012_artifact_text_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("privacy_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "privacy_settings")
