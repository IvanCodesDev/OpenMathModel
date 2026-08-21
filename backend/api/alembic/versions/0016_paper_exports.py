"""论文导出任务表与产物血缘列

Revision ID: 0016_paper_exports
Revises: 0015_llm_usage_route_difficulty
Create Date: 2026-08-21

ADR-0012 阶段 A：客户端直传 .tex、服务端 Tectonic 编译 PDF。
artifacts.inputs 是契约既有的血缘字段（PDF 指向 tex 源），此前 ORM 未落列；
可空 JSON 列兼容 SQLite 补列机制，历史行读取按空列表处理。
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_paper_exports"
down_revision = "0015_llm_usage_route_difficulty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("inputs", sa.JSON(), nullable=True))
    op.create_table(
        "paper_exports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False, index=True
        ),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=True, index=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column(
            "source_artifact_id", sa.String(64), sa.ForeignKey("artifacts.id"), nullable=True
        ),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("paper_exports")
    op.drop_column("artifacts", "inputs")
