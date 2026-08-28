"""运行中用户备注表（设计文档 §11.3 方案 A，H1 批次）

Revision ID: 0018_run_notes
Revises: 0017_stage_outputs
Create Date: 2026-08-28

append-only 备注：POST /v1/task-runs/{run_id}/notes 落行 + run.log 回执事件；
下一次节点执行时按 scope（global 或指定阶段）注入提示词的「用户补充要求」段。
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_run_notes"
down_revision = "0017_stage_outputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_notes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False, index=True
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(50), nullable=False, server_default="global"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("run_notes")
