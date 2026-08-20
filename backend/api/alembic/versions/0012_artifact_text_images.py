"""附件正文抽取结果记录内嵌图片数

Revision ID: 0012_artifact_text_images
Revises: 0011_llm_usage_records
Create Date: 2026-08-16

纯文本模型看不到文档里的图（ADR-0010）：抽取时统计 PDF/OOXML 的内嵌图片数，
供前端单模态提醒与后续视觉解析使用。可空列，兼容 SQLite 开发库补列机制。
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_artifact_text_images"
down_revision = "0011_llm_usage_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact_texts", sa.Column("images", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifact_texts", "images")
