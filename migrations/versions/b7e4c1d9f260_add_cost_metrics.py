"""add cost metrics to leaderboard results

Revision ID: b7e4c1d9f260
Revises: a3f5c8e1d204
Create Date: 2026-09-22 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c1d9f260"
down_revision: str | None = "a3f5c8e1d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("leaderboard_results") as batch_op:
        batch_op.add_column(
            sa.Column("total_cost_usd", sa.Numeric(precision=38, scale=18), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cost_per_task_usd", sa.Numeric(precision=38, scale=18), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("leaderboard_results") as batch_op:
        batch_op.drop_column("cost_per_task_usd")
        batch_op.drop_column("total_cost_usd")
