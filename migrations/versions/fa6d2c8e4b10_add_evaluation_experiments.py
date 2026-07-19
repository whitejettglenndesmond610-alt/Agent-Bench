"""add evaluation experiments

Revision ID: fa6d2c8e4b10
Revises: f8a1c3d5e702
Create Date: 2026-07-19 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa6d2c8e4b10"
down_revision: str | None = "f8a1c3d5e702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_experiments",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("creator_user_id", sa.Uuid(), nullable=True),
        sa.Column("creator_subject_key", sa.String(length=200), nullable=False),
        sa.Column("creator_display_name", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("protocol_hash", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(creator_display_name) > 0",
            name=op.f("ck_evaluation_experiments_creator_display_name_nonempty"),
        ),
        sa.CheckConstraint(
            "length(creator_subject_key) > 0",
            name=op.f("ck_evaluation_experiments_creator_subject_key_nonempty"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) = 36",
            name=op.f("ck_evaluation_experiments_idempotency_key_uuid"),
        ),
        sa.CheckConstraint(
            "length(name) > 0",
            name=op.f("ck_evaluation_experiments_name_nonempty"),
        ),
        sa.CheckConstraint(
            "length(protocol_hash) = 64",
            name=op.f("ck_evaluation_experiments_protocol_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_evaluation_experiments_request_fingerprint_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_user_id"],
            ["auth_users.id"],
            name=op.f("fk_evaluation_experiments_creator_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_experiments")),
        sa.UniqueConstraint(
            "creator_subject_key",
            "idempotency_key",
            name="uq_evaluation_experiments_creator_idempotency",
        ),
        sa.UniqueConstraint(
            "id",
            "protocol_hash",
            name="uq_evaluation_experiments_id_protocol_hash",
        ),
    )
    op.create_index(
        "ix_evaluation_experiments_creator_created",
        "evaluation_experiments",
        ["creator_subject_key", "created_at"],
    )
    op.create_index(
        op.f("ix_evaluation_experiments_creator_user_id"),
        "evaluation_experiments",
        ["creator_user_id"],
    )

    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.add_column(sa.Column("experiment_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("experiment_position", sa.SmallInteger(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_evaluation_runs_experiment_fields_valid",
            "(experiment_id IS NULL AND experiment_position IS NULL) OR "
            "(experiment_id IS NOT NULL AND experiment_position IS NOT NULL "
            "AND experiment_position BETWEEN 1 AND 50)",
        )
        batch_op.create_foreign_key(
            "fk_evaluation_runs_experiment_protocol",
            "evaluation_experiments",
            ["experiment_id", "protocol_hash"],
            ["id", "protocol_hash"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_evaluation_runs_experiment_position",
            ["experiment_id", "experiment_position"],
        )
    op.create_index(
        "ix_evaluation_runs_experiment_id",
        "evaluation_runs",
        ["experiment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_experiment_id", table_name="evaluation_runs")
    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_constraint(
            "uq_evaluation_runs_experiment_position", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_evaluation_runs_experiment_protocol", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_evaluation_runs_experiment_fields_valid", type_="check"
        )
        batch_op.drop_column("experiment_position")
        batch_op.drop_column("experiment_id")

    op.drop_index(
        op.f("ix_evaluation_experiments_creator_user_id"),
        table_name="evaluation_experiments",
    )
    op.drop_index(
        "ix_evaluation_experiments_creator_created",
        table_name="evaluation_experiments",
    )
    op.drop_table("evaluation_experiments")
