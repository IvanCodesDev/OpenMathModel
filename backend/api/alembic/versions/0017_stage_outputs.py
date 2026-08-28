"""阶段输出版本化表（设计文档 §10.2 / D1.6，H1 批次）

Revision ID: 0017_stage_outputs
Revises: 0016_paper_exports
Create Date: 2026-08-28

STEP_SUCCEEDED 投影时按节点落行：新版本 current、旧版本 superseded，
重试/退回重做的历史输出可审计。content 存节点 outputs 原文（读侧投影
组装六类页面正文契约），lane_id 为 Graph v2 子问题并行预留。
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_stage_outputs"
down_revision = "0016_paper_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stage_outputs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False, index=True
        ),
        sa.Column("node", sa.String(50), nullable=False),
        sa.Column("lane_id", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_id", sa.String(100), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "producer_step_id", sa.String(64), sa.ForeignKey("step_runs.id"), nullable=True
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "node", "version", name="uq_stage_outputs_run_node_version"),
    )
    op.create_index(
        "ix_stage_outputs_run_node_status", "stage_outputs", ["run_id", "node", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_stage_outputs_run_node_status", table_name="stage_outputs")
    op.drop_table("stage_outputs")
