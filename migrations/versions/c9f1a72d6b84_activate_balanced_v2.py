"""activate balanced v2

Revision ID: c9f1a72d6b84
Revises: b8e5c02f4a71
Create Date: 2026-07-17 05:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f1a72d6b84"
down_revision: str | None = "b8e5c02f4a71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cohort_key",
                sa.String(length=80),
                server_default="legacy-v1",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_dataset_versions_cohort_key_nonempty"), "length(cohort_key) > 0"
        )
        batch_op.create_index("ix_dataset_versions_cohort_key", ["cohort_key"], unique=False)

    with op.batch_alter_table("score_configs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_score_configs_ability_weights_total"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_score_configs_ability_weights_range"), type_="check"
        )
        batch_op.add_column(
            sa.Column(
                "instruction_following_weight",
                sa.SmallInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "long_context_weight",
                sa.SmallInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_score_configs_ability_weights_total"),
            "reasoning_weight + retrieval_weight + data_weight + "
            "instruction_following_weight + long_context_weight = 100",
        )
        batch_op.create_check_constraint(
            op.f("ck_score_configs_ability_weights_range"),
            "reasoning_weight BETWEEN 0 AND 100 AND "
            "retrieval_weight BETWEEN 0 AND 100 AND "
            "data_weight BETWEEN 0 AND 100 AND "
            "instruction_following_weight BETWEEN 0 AND 100 AND "
            "long_context_weight BETWEEN 0 AND 100",
        )


def downgrade() -> None:
    connection = op.get_bind()
    nonzero = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM score_configs "
            "WHERE instruction_following_weight != 0 OR long_context_weight != 0"
        )
    )
    nonlegacy = connection.scalar(
        sa.text("SELECT COUNT(*) FROM dataset_versions WHERE cohort_key != 'legacy-v1'")
    )
    if nonzero or nonlegacy:
        raise RuntimeError(
            "downgrade requires balanced-v2 datasets and score configs to be removed"
        )

    with op.batch_alter_table("score_configs") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_score_configs_ability_weights_total"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_score_configs_ability_weights_range"), type_="check"
        )
        batch_op.drop_column("long_context_weight")
        batch_op.drop_column("instruction_following_weight")
        batch_op.create_check_constraint(
            op.f("ck_score_configs_ability_weights_total"),
            "reasoning_weight + retrieval_weight + data_weight = 100",
        )
        batch_op.create_check_constraint(
            op.f("ck_score_configs_ability_weights_range"),
            "reasoning_weight BETWEEN 0 AND 100 AND "
            "retrieval_weight BETWEEN 0 AND 100 AND data_weight BETWEEN 0 AND 100",
        )

    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.drop_index("ix_dataset_versions_cohort_key")
        batch_op.drop_constraint(
            op.f("ck_dataset_versions_cohort_key_nonempty"), type_="check"
        )
        batch_op.drop_column("cohort_key")
