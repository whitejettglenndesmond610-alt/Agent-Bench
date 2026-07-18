"""add workspace comparisons and evaluation cases

Revision ID: c6a2f4d8e913
Revises: b5e1f8a3c702
Create Date: 2026-07-19 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6a2f4d8e913"
down_revision: str | None = "b5e1f8a3c702"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_comparisons",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key) = 36",
            name=op.f("ck_workspace_comparisons_idempotency_key_uuid"),
        ),
        sa.CheckConstraint(
            "length(prompt) BETWEEN 1 AND 200000",
            name=op.f("ck_workspace_comparisons_prompt_length"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_workspace_comparisons_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "length(title) > 0", name=op.f("ck_workspace_comparisons_title_nonempty")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["auth_users.id"],
            name=op.f("fk_workspace_comparisons_owner_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_comparisons")),
        sa.UniqueConstraint(
            "id", "owner_user_id", name="uq_workspace_comparisons_id_owner"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_workspace_comparisons_idempotency",
        ),
    )
    op.create_index(
        "ix_workspace_comparisons_owner_created",
        "workspace_comparisons",
        ["owner_user_id", "created_at"],
    )

    with op.batch_alter_table("workspace_conversations") as batch_op:
        batch_op.add_column(sa.Column("comparison_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("comparison_position", sa.SmallInteger(), nullable=True))
        batch_op.create_check_constraint(
            "ck_workspace_conversations_comparison_fields_valid",
            "(comparison_id IS NULL AND comparison_position IS NULL) OR "
            "(comparison_id IS NOT NULL AND comparison_position BETWEEN 1 AND 5)",
        )
        batch_op.create_foreign_key(
            "fk_workspace_conversations_comparison_owner",
            "workspace_comparisons",
            ["comparison_id", "owner_user_id"],
            ["id", "owner_user_id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_workspace_conversations_comparison_position",
            ["comparison_id", "comparison_position"],
        )
        batch_op.create_unique_constraint(
            "uq_workspace_conversations_comparison_agent_version",
            ["comparison_id", "pinned_agent_version_id"],
        )
        batch_op.create_unique_constraint(
            "uq_workspace_conversations_id_owner", ["id", "owner_user_id"]
        )
    op.create_index(
        op.f("ix_workspace_conversations_comparison_id"),
        "workspace_conversations",
        ["comparison_id"],
    )
    with op.batch_alter_table("workspace_attempts") as batch_op:
        batch_op.create_unique_constraint(
            "uq_workspace_attempts_id_conversation", ["id", "conversation_id"]
        )

    op.create_table(
        "evaluation_cases",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_conversation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "reviewed",
                "published",
                name="evaluation_case_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("reference_answer", sa.Text(), nullable=True),
        sa.Column(
            "language",
            sa.Enum(
                "zh",
                "en",
                name="evaluation_case_language",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "ability",
            sa.Enum(
                "reasoning",
                "retrieval",
                "data_reasoning",
                "instruction_following",
                "long_context",
                name="evaluation_case_ability",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "question_type",
            sa.Enum(
                "multiple_choice",
                "short_answer",
                "open_ended",
                name="evaluation_case_question_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requires_citations", sa.Boolean(), nullable=False),
        sa.Column("difficulty", sa.String(length=80), nullable=True),
        sa.Column("domain", sa.String(length=120), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("standalone_verified", sa.Boolean(), nullable=False),
        sa.Column("reference_answer_verified", sa.Boolean(), nullable=False),
        sa.Column("rights_verified", sa.Boolean(), nullable=False),
        sa.Column("sensitive_data_verified", sa.Boolean(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key) = 36",
            name=op.f("ck_evaluation_cases_idempotency_key_uuid"),
        ),
        sa.CheckConstraint(
            "length(prompt) BETWEEN 1 AND 200000",
            name=op.f("ck_evaluation_cases_prompt_length"),
        ),
        sa.CheckConstraint(
            "published_prompt_hash IS NULL OR length(published_prompt_hash) = 64",
            name=op.f("ck_evaluation_cases_published_prompt_hash_sha256"),
        ),
        sa.CheckConstraint(
            "question_type IN ('short_answer', 'open_ended')",
            name=op.f("ck_evaluation_cases_supported_question_type"),
        ),
        sa.CheckConstraint(
            "record_hash IS NULL OR length(record_hash) = 64",
            name=op.f("ck_evaluation_cases_record_hash_sha256"),
        ),
        sa.CheckConstraint(
            "reference_answer IS NULL OR length(reference_answer) > 0",
            name=op.f("ck_evaluation_cases_reference_answer_nonempty"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_evaluation_cases_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_evaluation_cases_revision_nonnegative")
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name=op.f("ck_evaluation_cases_source_hash_sha256"),
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL "
            "AND record_hash IS NULL AND published_by_user_id IS NULL AND published_at IS NULL "
            "AND published_prompt_hash IS NULL AND standalone_verified = false "
            "AND reference_answer_verified = false AND rights_verified = false "
            "AND sensitive_data_verified = false) OR "
            "(status = 'reviewed' AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND record_hash IS NOT NULL AND published_by_user_id IS NULL AND published_at IS NULL "
            "AND published_prompt_hash IS NULL AND standalone_verified = true "
            "AND reference_answer_verified = true AND rights_verified = true "
            "AND sensitive_data_verified = true AND reference_answer IS NOT NULL "
            "AND language IS NOT NULL AND ability IS NOT NULL) OR "
            "(status = 'published' AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND record_hash IS NOT NULL AND published_by_user_id IS NOT NULL "
            "AND published_at IS NOT NULL AND published_prompt_hash IS NOT NULL "
            "AND standalone_verified = true AND reference_answer_verified = true "
            "AND rights_verified = true AND sensitive_data_verified = true "
            "AND reference_answer IS NOT NULL AND language IS NOT NULL AND ability IS NOT NULL)",
            name=op.f("ck_evaluation_cases_status_lifecycle_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["auth_users.id"],
            name=op.f("fk_evaluation_cases_owner_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"],
            ["auth_users.id"],
            name=op.f("fk_evaluation_cases_published_by_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["auth_users.id"],
            name=op.f("fk_evaluation_cases_reviewed_by_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id", "source_conversation_id"],
            ["workspace_attempts.id", "workspace_attempts.conversation_id"],
            name="fk_evaluation_cases_source_attempt_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id", "owner_user_id"],
            ["workspace_conversations.id", "workspace_conversations.owner_user_id"],
            name="fk_evaluation_cases_source_conversation_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_cases")),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_evaluation_cases_idempotency"
        ),
        sa.UniqueConstraint(
            "published_prompt_hash",
            name=op.f("uq_evaluation_cases_published_prompt_hash"),
        ),
        sa.UniqueConstraint(
            "source_attempt_id", name=op.f("uq_evaluation_cases_source_attempt_id")
        ),
    )
    op.create_index(
        "ix_evaluation_cases_owner_updated",
        "evaluation_cases",
        ["owner_user_id", "updated_at"],
    )
    op.create_index(
        op.f("ix_evaluation_cases_status"), "evaluation_cases", ["status"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_evaluation_cases_status"), table_name="evaluation_cases")
    op.drop_index("ix_evaluation_cases_owner_updated", table_name="evaluation_cases")
    op.drop_table("evaluation_cases")
    with op.batch_alter_table("workspace_attempts") as batch_op:
        batch_op.drop_constraint(
            "uq_workspace_attempts_id_conversation", type_="unique"
        )
    op.drop_index(
        op.f("ix_workspace_conversations_comparison_id"),
        table_name="workspace_conversations",
    )
    with op.batch_alter_table("workspace_conversations") as batch_op:
        batch_op.drop_constraint(
            "uq_workspace_conversations_comparison_agent_version", type_="unique"
        )
        batch_op.drop_constraint("uq_workspace_conversations_id_owner", type_="unique")
        batch_op.drop_constraint(
            "uq_workspace_conversations_comparison_position", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_workspace_conversations_comparison_owner", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "ck_workspace_conversations_comparison_fields_valid", type_="check"
        )
        batch_op.drop_column("comparison_position")
        batch_op.drop_column("comparison_id")
    op.drop_index(
        "ix_workspace_comparisons_owner_created", table_name="workspace_comparisons"
    )
    op.drop_table("workspace_comparisons")
