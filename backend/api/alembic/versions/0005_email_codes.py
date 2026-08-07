"""注册邮箱验证码表：email_verification_codes

Revision ID: 0005_email_codes
Revises: 0004_login_attempts
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_email_codes"
down_revision = "0004_login_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_email_codes_email_time", "email_verification_codes", ["email", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("email_verification_codes")
