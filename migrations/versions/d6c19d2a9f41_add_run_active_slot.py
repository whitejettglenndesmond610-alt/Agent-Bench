"""add run active slot

Revision ID: d6c19d2a9f41
Revises: 6da6b7bdcb27
Create Date: 2026-07-16 02:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6c19d2a9f41"
down_revision: str | None = "6da6b7bdcb27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.add_column(sa.Column("active_slot", sa.SmallInteger(), nullable=True))

    connection = op.get_bind()
    active_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM evaluation_runs "
            "WHERE status IN ('running', 'paused', 'scoring')"
        )
    )
    if active_count and active_count > 1:
        raise RuntimeError("migration requires at most one active evaluation run")
    connection.execute(
        sa.text(
            "UPDATE evaluation_runs SET active_slot = 1 "
            "WHERE status IN ('running', 'paused', 'scoring')"
        )
    )

    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.create_unique_constraint(op.f("uq_evaluation_runs_active_slot"), ["active_slot"])
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_active_slot_matches_status"),
            "(status IN ('running', 'paused', 'scoring') AND active_slot = 1) OR "
            "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_evaluation_runs_active_slot_matches_status"), type_="check"
        )
        batch_op.drop_constraint(op.f("uq_evaluation_runs_active_slot"), type_="unique")
        batch_op.drop_column("active_slot")
