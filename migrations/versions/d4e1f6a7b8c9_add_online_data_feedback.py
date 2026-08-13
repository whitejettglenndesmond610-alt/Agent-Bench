"""add online data feedback batches

Revision ID: d4e1f6a7b8c9
Revises: c3d0e5f6a7b8
Create Date: 2026-08-14 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e1f6a7b8c9"
down_revision: str | None = "c3d0e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "online_data_batches",
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column(
            "environment",
            sa.Enum(
                "production",
                "staging",
                "replay",
                name="online_data_environment",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("external_batch_id", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_errors_json", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("creator_subject_key", sa.String(length=200), nullable=False),
        sa.Column("creator_display_name", sa.String(length=160), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(source_name) BETWEEN 1 AND 80",
            name=op.f("ck_online_data_batches_source_name_length"),
        ),
        sa.CheckConstraint(
            "length(external_batch_id) BETWEEN 1 AND 160",
            name=op.f("ck_online_data_batches_external_batch_id_length"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) = 36",
            name=op.f("ck_online_data_batches_idempotency_key_uuid"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_online_data_batches_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "received_count BETWEEN 1 AND 500",
            name=op.f("ck_online_data_batches_received_count_range"),
        ),
        sa.CheckConstraint(
            "accepted_count >= 0",
            name=op.f("ck_online_data_batches_accepted_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "duplicate_count >= 0",
            name=op.f("ck_online_data_batches_duplicate_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "accepted_count + duplicate_count = received_count",
            name=op.f("ck_online_data_batches_counts_match"),
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["auth_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_online_data_batches")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_online_data_batches_idempotency_key")),
        sa.UniqueConstraint("source_name", "external_batch_id", name="uq_online_batches_source_id"),
    )
    op.create_index(
        "ix_online_data_batches_source_created",
        "online_data_batches",
        ["source_name", "created_at"],
    )

    op.create_table(
        "online_data_records",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=160), nullable=False),
        sa.Column("agent_version", sa.String(length=120), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("trace_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "retained",
                "discarded",
                name="online_data_record_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "risk_severity",
            sa.Enum(
                "low",
                "medium",
                "high",
                "critical",
                name="online_data_risk_severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("match_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("matched_rules_json", sa.JSON(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_subject_key", sa.String(length=200), nullable=True),
        sa.Column("reviewer_display_name", sa.String(length=160), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(external_id) BETWEEN 1 AND 200",
            name=op.f("ck_online_data_records_external_id_length"),
        ),
        sa.CheckConstraint(
            "length(dedupe_key) = 64",
            name=op.f("ck_online_data_records_dedupe_key_sha256"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_online_data_records_content_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(agent_name) BETWEEN 1 AND 160",
            name=op.f("ck_online_data_records_agent_name_length"),
        ),
        sa.CheckConstraint(
            "length(prompt) BETWEEN 1 AND 200000",
            name=op.f("ck_online_data_records_prompt_length"),
        ),
        sa.CheckConstraint(
            "length(answer) BETWEEN 1 AND 500000",
            name=op.f("ck_online_data_records_answer_length"),
        ),
        sa.CheckConstraint(
            "match_count >= 0",
            name=op.f("ck_online_data_records_match_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL "
            "AND reviewer_subject_key IS NULL AND reviewer_display_name IS NULL) OR "
            "(status IN ('retained', 'discarded') AND reviewed_at IS NOT NULL "
            "AND reviewer_subject_key IS NOT NULL AND reviewer_display_name IS NOT NULL)",
            name=op.f("ck_online_data_records_review_lifecycle_valid"),
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["online_data_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["auth_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_online_data_records")),
        sa.UniqueConstraint("batch_id", "external_id", name="uq_online_records_batch_external"),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_online_data_records_dedupe_key")),
    )
    op.create_index(op.f("ix_online_data_records_batch_id"), "online_data_records", ["batch_id"])
    op.create_index(
        "ix_online_records_status_occurred",
        "online_data_records",
        ["status", "occurred_at", "id"],
    )
    op.create_index(
        "ix_online_records_risk_occurred",
        "online_data_records",
        ["risk_severity", "occurred_at", "id"],
    )
    op.create_index(
        "ix_online_records_agent_occurred",
        "online_data_records",
        ["agent_name", "occurred_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_online_records_agent_occurred", table_name="online_data_records")
    op.drop_index("ix_online_records_risk_occurred", table_name="online_data_records")
    op.drop_index("ix_online_records_status_occurred", table_name="online_data_records")
    op.drop_index(op.f("ix_online_data_records_batch_id"), table_name="online_data_records")
    op.drop_table("online_data_records")
    op.drop_index("ix_online_data_batches_source_created", table_name="online_data_batches")
    op.drop_table("online_data_batches")
