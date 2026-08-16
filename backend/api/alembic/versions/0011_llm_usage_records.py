"""模型调用用量记录与用户预算设置

Revision ID: 0011_llm_usage_records
Revises: 0010_user_llm_config
Create Date: 2026-08-16

设置中心「用量监控」：每次成功的模型调用记一行 llm_usage_records（费用按
单价表读取时估算，不落库）；users.usage_settings 存月度预算/提醒阈值/硬限制。
可空列兼容 SQLite 补列机制；user_id 不设外键（用量是历史事实，用户删除后仍可审计）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_llm_usage_records"
down_revision = "0010_user_llm_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_records",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("endpoint_name", sa.String(length=120), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("third_party", sa.Boolean(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_usage_user_created", "llm_usage_records", ["user_id", "created_at"])
    op.create_index("ix_llm_usage_records_run_id", "llm_usage_records", ["run_id"])
    op.add_column("users", sa.Column("usage_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "usage_settings")
    op.drop_index("ix_llm_usage_records_run_id", table_name="llm_usage_records")
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage_records")
    op.drop_table("llm_usage_records")
