"""首批六表：projects / task_runs / step_runs / agent_events / artifacts / approval_requests

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04

与 app/orm.py 保持一致；SQLite 开发环境用 create_all，本迁移面向 PostgreSQL 部署。
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("mode", sa.String(50)),
        sa.Column("competition_policy", sa.String(100)),
        sa.Column("workspace_uri", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("workflow_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("auto_start", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("current_node", sa.String(100)),
        sa.Column("paused_from_status", sa.String(50)),
        sa.Column("failure_class", sa.String(50)),
        sa.Column("budget", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_task_runs_project_id", "task_runs", ["project_id"])
    op.create_index("ix_task_runs_status", "task_runs", ["status"])
    op.create_table(
        "step_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("node", sa.String(100), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("input_hash", sa.String(64)),
        sa.Column("failure_class", sa.String(50)),
        sa.Column("detail", sa.String(2000)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "node", "attempt", name="uq_step_runs_run_node_attempt"),
    )
    op.create_index("ix_step_runs_run_id", "step_runs", ["run_id"])
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_index("ix_agent_events_run_sequence", "agent_events", ["run_id", "sequence"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id")),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("uri", sa.String(1000)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("media_type", sa.String(200)),
        sa.Column("producer_step", sa.String(100)),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("task_runs.id"), nullable=False),
        sa.Column("decision_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("options", sa.JSON, nullable=False),
        sa.Column("evidence", sa.JSON),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.JSON),
        sa.Column("client_token", sa.String(64)),
    )
    op.create_index("ix_approval_requests_run_id", "approval_requests", ["run_id"])


def downgrade() -> None:
    op.drop_table("approval_requests")
    op.drop_table("artifacts")
    op.drop_table("agent_events")
    op.drop_table("step_runs")
    op.drop_table("task_runs")
    op.drop_table("projects")
