"""add ranking result validity

Revision ID: d0a4e83f7c21
Revises: c9f1a72d6b84
Create Date: 2026-07-17 08:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0a4e83f7c21"
down_revision: str | None = "c9f1a72d6b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_agents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "validity_status",
                sa.String(length=7),
                server_default="pending",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("validity_reason", sa.String(length=160), nullable=True))
        batch_op.add_column(
            sa.Column(
                "configuration_failure_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "configuration_failure_rate",
                sa.Numeric(precision=7, scale=4),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_run_agents_validity_status"),
            "validity_status IN ('pending', 'valid', 'invalid')",
        )
        batch_op.create_check_constraint(
            op.f("ck_run_agents_configuration_failure_count_nonnegative"),
            "configuration_failure_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_run_agents_configuration_failure_rate_range"),
            "configuration_failure_rate BETWEEN 0 AND 100",
        )
        batch_op.create_index(
            "ix_run_agents_validity_status", ["validity_status"], unique=False
        )
    with op.batch_alter_table("http_attempts") as batch_op:
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("total_tokens", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_http_attempts_input_tokens_nonnegative"),
            "input_tokens IS NULL OR input_tokens >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_http_attempts_output_tokens_nonnegative"),
            "output_tokens IS NULL OR output_tokens >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_http_attempts_total_tokens_nonnegative"),
            "total_tokens IS NULL OR total_tokens >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("http_attempts") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_http_attempts_total_tokens_nonnegative"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_http_attempts_output_tokens_nonnegative"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_http_attempts_input_tokens_nonnegative"), type_="check"
        )
        batch_op.drop_column("total_tokens")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
    with op.batch_alter_table("run_agents") as batch_op:
        batch_op.drop_index("ix_run_agents_validity_status")
        batch_op.drop_constraint(
            op.f("ck_run_agents_configuration_failure_rate_range"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_run_agents_configuration_failure_count_nonnegative"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_run_agents_validity_status"), type_="check")
        batch_op.drop_column("configuration_failure_rate")
        batch_op.drop_column("configuration_failure_count")
        batch_op.drop_column("validity_reason")
        batch_op.drop_column("validity_status")
