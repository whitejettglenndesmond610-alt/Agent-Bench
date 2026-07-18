"""add curated dataset releases

Revision ID: d7b3e5f9a024
Revises: c6a2f4d8e913
Create Date: 2026-07-19 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b3e5f9a024"
down_revision: str | None = "c6a2f4d8e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_releases",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "approved",
                "published",
                name="dataset_release_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_dataset_releases_name_nonempty")),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_dataset_releases_revision_nonnegative")
        ),
        sa.CheckConstraint(
            "length(idempotency_key) = 36",
            name=op.f("ck_dataset_releases_idempotency_key_uuid"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_dataset_releases_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "manifest_hash IS NULL OR length(manifest_hash) = 64",
            name=op.f("ck_dataset_releases_manifest_hash_sha256"),
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND manifest_hash IS NULL AND approved_by_user_id IS NULL "
            "AND approved_at IS NULL AND published_by_user_id IS NULL AND published_at IS NULL) OR "
            "(status = 'approved' AND manifest_hash IS NOT NULL "
            "AND approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_by_user_id IS NULL AND published_at IS NULL) OR "
            "(status = 'published' AND manifest_hash IS NOT NULL "
            "AND approved_by_user_id IS NOT NULL "
            "AND approved_at IS NOT NULL AND published_by_user_id IS NOT NULL "
            "AND published_at IS NOT NULL)",
            name=op.f("ck_dataset_releases_status_lifecycle_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["auth_users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["auth_users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"], ["auth_users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_releases")),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_dataset_releases_idempotency"
        ),
    )
    op.create_index(
        "ix_dataset_releases_owner_updated",
        "dataset_releases",
        ["owner_user_id", "updated_at"],
    )
    op.create_index(
        op.f("ix_dataset_releases_status"), "dataset_releases", ["status"]
    )

    op.create_table(
        "dataset_release_cases",
        sa.Column("dataset_release_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_case_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "position > 0", name=op.f("ck_dataset_release_cases_position_positive")
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name=op.f("ck_dataset_release_cases_record_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_release_id"], ["dataset_releases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_case_id"], ["evaluation_cases.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "dataset_release_id",
            "evaluation_case_id",
            name=op.f("pk_dataset_release_cases"),
        ),
        sa.UniqueConstraint(
            "dataset_release_id", "position", name="uq_dataset_release_cases_position"
        ),
    )

    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.add_column(
            sa.Column("run_eligible", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(sa.Column("dataset_release_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_dataset_versions_id_run_eligible", ["id", "run_eligible"]
        )
        batch_op.create_foreign_key(
            op.f("fk_dataset_versions_dataset_release_id_dataset_releases"),
            "dataset_releases",
            ["dataset_release_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        op.f("ix_dataset_versions_run_eligible"),
        "dataset_versions",
        ["run_eligible"],
    )
    op.create_index(
        op.f("ix_dataset_versions_dataset_release_id"),
        "dataset_versions",
        ["dataset_release_id"],
    )

    with op.batch_alter_table("questions") as batch_op:
        batch_op.add_column(sa.Column("source_evaluation_case_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_questions_source_evaluation_case_id_evaluation_cases"),
            "evaluation_cases",
            ["source_evaluation_case_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            op.f("uq_questions_source_evaluation_case_id"), ["source_evaluation_case_id"]
        )
    op.create_index(
        op.f("ix_questions_source_evaluation_case_id"),
        "questions",
        ["source_evaluation_case_id"],
    )

    with op.batch_alter_table("run_dataset_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "dataset_run_eligible", sa.Boolean(), server_default=sa.true(), nullable=False
            )
        )
        batch_op.create_check_constraint(
            "ck_run_dataset_versions_dataset_must_be_runnable",
            "dataset_run_eligible = true",
        )
        batch_op.create_foreign_key(
            "fk_run_dataset_versions_dataset_runnable",
            "dataset_versions",
            ["dataset_version_id", "dataset_run_eligible"],
            ["id", "run_eligible"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("run_dataset_versions") as batch_op:
        batch_op.drop_constraint(
            "fk_run_dataset_versions_dataset_runnable", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_run_dataset_versions_dataset_must_be_runnable", type_="check"
        )
        batch_op.drop_column("dataset_run_eligible")

    op.drop_index(
        op.f("ix_questions_source_evaluation_case_id"), table_name="questions"
    )
    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_questions_source_evaluation_case_id"), type_="unique"
        )
        batch_op.drop_constraint(
            op.f("fk_questions_source_evaluation_case_id_evaluation_cases"),
            type_="foreignkey",
        )
        batch_op.drop_column("source_evaluation_case_id")

    op.drop_index(
        op.f("ix_dataset_versions_dataset_release_id"), table_name="dataset_versions"
    )
    op.drop_index(
        op.f("ix_dataset_versions_run_eligible"), table_name="dataset_versions"
    )
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_dataset_versions_dataset_release_id_dataset_releases"),
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_dataset_versions_id_run_eligible", type_="unique")
        batch_op.drop_column("dataset_release_id")
        batch_op.drop_column("run_eligible")

    op.drop_table("dataset_release_cases")
    op.drop_index(op.f("ix_dataset_releases_status"), table_name="dataset_releases")
    op.drop_index("ix_dataset_releases_owner_updated", table_name="dataset_releases")
    op.drop_table("dataset_releases")
