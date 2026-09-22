"""add judge version pinning and calibration labels

Revision ID: a3f5c8e1d204
Revises: f5e1a2b3c4d6
Create Date: 2026-09-22 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f5c8e1d204"
down_revision: str | None = "f5e1a2b3c4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("score_records") as batch_op:
        batch_op.add_column(sa.Column("judge_provider", sa.String(length=80), nullable=True))
        batch_op.add_column(
            sa.Column("judge_model_requested", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("judge_model_returned", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("judge_prompt_version", sa.String(length=80), nullable=True)
        )
        batch_op.add_column(
            sa.Column("judge_temperature", sa.Numeric(precision=4, scale=2), nullable=True)
        )
        batch_op.add_column(sa.Column("judge_details_json", sa.JSON(), nullable=True))

    op.create_table(
        "judge_calibration_labels",
        sa.Column("score_record_id", sa.Uuid(), nullable=False),
        sa.Column("judge_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("judge_details_json", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "labeled",
                name="judge_calibration_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "human_label",
            sa.Enum(
                "agree",
                "disagree",
                name="judge_calibration_agreement",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("labeled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("labeler_subject_key", sa.String(length=200), nullable=True),
        sa.Column("labeler_display_name", sa.String(length=160), nullable=True),
        sa.Column("labeled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "judge_score BETWEEN 0 AND 100",
            name=op.f("ck_judge_calibration_labels_judge_score_range"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND human_label IS NULL AND labeled_at IS NULL) OR "
            "(status = 'labeled' AND human_label IS NOT NULL AND labeled_at IS NOT NULL)",
            name=op.f("ck_judge_calibration_labels_label_lifecycle_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["labeled_by_user_id"], ["auth_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["score_record_id"], ["score_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_judge_calibration_labels")),
        sa.UniqueConstraint(
            "score_record_id", name=op.f("uq_judge_calibration_labels_score_record_id")
        ),
    )
    op.create_index(
        "ix_judge_calibration_labels_status_created",
        "judge_calibration_labels",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_judge_calibration_labels_status_created", table_name="judge_calibration_labels"
    )
    op.drop_table("judge_calibration_labels")
    with op.batch_alter_table("score_records") as batch_op:
        batch_op.drop_column("judge_details_json")
        batch_op.drop_column("judge_temperature")
        batch_op.drop_column("judge_prompt_version")
        batch_op.drop_column("judge_model_returned")
        batch_op.drop_column("judge_model_requested")
        batch_op.drop_column("judge_provider")
