"""最大并发任务改为默认不限：清掉随旧面板默认值一起存下的 3

Revision ID: 0022_unlimited_concurrent_runs
Revises: 0021_artifact_display_names
Create Date: 2026-10-05

任务改成按运行并行推进后，「最大并发任务」默认不限（NULL）。旧设置面板每次保存都会把下拉
当前值一起推上来，而下拉默认显示「3 个」，所以存着的 3 绝大多数不是用户刻意选的，留着会让
这些账户照样被 3 卡住；本迁移把它们清回 NULL。1 / 5 / 8 要动手才选得到，原样保留。
纯数据修正；downgrade 不回写（分不清哪些 3 是本迁移清掉的）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0022_unlimited_concurrent_runs"
down_revision = "0021_artifact_display_names"
branch_labels = None
depends_on = None

#: 旧版设置面板下拉的默认显示值（当时 config.default_max_concurrent_runs 也是它）。
_OLD_PANEL_DEFAULT = 3


def upgrade() -> None:
    users = sa.table("users", sa.column("max_concurrent_runs", sa.Integer))
    op.get_bind().execute(
        users.update()
        .where(users.c.max_concurrent_runs == _OLD_PANEL_DEFAULT)
        .values(max_concurrent_runs=None)
    )


def downgrade() -> None:
    pass
