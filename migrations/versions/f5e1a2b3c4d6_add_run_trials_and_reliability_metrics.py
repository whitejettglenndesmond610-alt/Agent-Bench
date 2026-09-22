"""add run trials and reliability metrics

Revision ID: f5e1a2b3c4d6
Revises: e5f2a7b8c9d0
Create Date: 2026-09-22 08:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5e1a2b3c4d6"
down_revision: str | None = "e5f2a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trial_count",
                sa.SmallInteger(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
    with op.batch_alter_table("execution_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trial_index",
                sa.SmallInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.drop_constraint("uq_execution_items_agent_question", type_="unique")
        batch_op.create_unique_constraint(
            "uq_execution_items_agent_question",
            ["run_agent_id", "question_id", "trial_index"],
        )
    with op.batch_alter_table("leaderboard_results") as batch_op:
        batch_op.add_column(sa.Column("ci_low", sa.Numeric(precision=38, scale=18), nullable=True))
        batch_op.add_column(
            sa.Column("ci_high", sa.Numeric(precision=38, scale=18), nullable=True)
        )
        batch_op.add_column(
            sa.Column("pass_at_1", sa.Numeric(precision=38, scale=18), nullable=True)
        )
        batch_op.add_column(
            sa.Column("pass_at_k", sa.Numeric(precision=38, scale=18), nullable=True)
        )
        batch_op.add_column(
            sa.Column("pass_pow_k", sa.Numeric(precision=38, scale=18), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("leaderboard_results") as batch_op:
        batch_op.drop_column("pass_pow_k")
        batch_op.drop_column("pass_at_k")
        batch_op.drop_column("pass_at_1")
        batch_op.drop_column("ci_high")
        batch_op.drop_column("ci_low")
    with op.batch_alter_table("execution_items") as batch_op:
        batch_op.drop_constraint("uq_execution_items_agent_question", type_="unique")
        batch_op.create_unique_constraint(
            "uq_execution_items_agent_question",
            ["run_agent_id", "question_id"],
        )
        batch_op.drop_column("trial_index")
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_column("trial_count")
