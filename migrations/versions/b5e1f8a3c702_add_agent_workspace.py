"""add durable agent workspace

Revision ID: b5e1f8a3c702
Revises: a4d9c2e7f601
Create Date: 2026-07-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e1f8a3c702"
down_revision: str | None = "a4d9c2e7f601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_conversations",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("pinned_agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("last_turn_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "last_turn_no >= 0", name=op.f("ck_workspace_conversations_last_turn_no_nonnegative")
        ),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_workspace_conversations_revision_nonnegative")
        ),
        sa.CheckConstraint(
            "length(title) > 0", name=op.f("ck_workspace_conversations_title_nonempty")
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["auth_users.id"],
            name=op.f("fk_workspace_conversations_owner_user_id_auth_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pinned_agent_version_id"],
            ["agent_versions.id"],
            name=op.f("fk_workspace_conversations_pinned_agent_version_id_agent_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_conversations")),
    )
    op.create_index(
        op.f("ix_workspace_conversations_archived_at"),
        "workspace_conversations",
        ["archived_at"],
    )
    op.create_index(
        "ix_workspace_conversations_owner_updated",
        "workspace_conversations",
        ["owner_user_id", "updated_at"],
    )
    op.create_index(
        op.f("ix_workspace_conversations_pinned_agent_version_id"),
        "workspace_conversations",
        ["pinned_agent_version_id"],
    )

    op.create_table(
        "workspace_messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="workspace_message_role",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("variant_no", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(content) > 0", name=op.f("ck_workspace_messages_content_nonempty")
        ),
        sa.CheckConstraint(
            "turn_no > 0", name=op.f("ck_workspace_messages_turn_no_positive")
        ),
        sa.CheckConstraint(
            "role != 'user' OR (variant_no = 1 AND active = true)",
            name=op.f("ck_workspace_messages_user_variant_active"),
        ),
        sa.CheckConstraint(
            "variant_no > 0", name=op.f("ck_workspace_messages_variant_no_positive")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["workspace_conversations.id"],
            name=op.f("fk_workspace_messages_conversation_id_workspace_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_messages")),
        sa.UniqueConstraint(
            "conversation_id",
            "turn_no",
            "role",
            "variant_no",
            name="uq_workspace_messages_turn_role_variant",
        ),
    )
    op.create_index(
        "ix_workspace_messages_conversation_turn",
        "workspace_messages",
        ["conversation_id", "turn_no"],
    )
    op.create_index(
        "uq_workspace_messages_active_assistant",
        "workspace_messages",
        ["conversation_id", "turn_no"],
        unique=True,
        sqlite_where=sa.text("role = 'assistant' AND active = 1"),
        postgresql_where=sa.text("role = 'assistant' AND active"),
    )

    op.create_table(
        "workspace_attempts",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_message_id", sa.Uuid(), nullable=False),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=True),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "canceled",
                name="workspace_attempt_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_owner_id", sa.String(length=120), nullable=True),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_attempt_count", sa.SmallInteger(), nullable=False),
        sa.Column("last_http_status", sa.SmallInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=80), nullable=True),
        sa.Column("error_category", sa.String(length=80), nullable=True),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_characters", sa.Integer(), nullable=False),
        sa.Column("prompt_format", sa.String(length=40), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation_no > 0", name=op.f("ck_workspace_attempts_generation_no_positive")
        ),
        sa.CheckConstraint(
            "http_attempt_count BETWEEN 0 AND 2",
            name=op.f("ck_workspace_attempts_http_attempt_count_range"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_workspace_attempts_input_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599",
            name=op.f("ck_workspace_attempts_last_http_status_range"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_workspace_attempts_latency_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_workspace_attempts_output_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "prompt_characters BETWEEN 1 AND 200000",
            name=op.f("ck_workspace_attempts_prompt_characters_range"),
        ),
        sa.CheckConstraint(
            "length(prompt_hash) = 64",
            name=op.f("ck_workspace_attempts_prompt_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_workspace_attempts_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND claim_owner_id IS NULL AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL AND heartbeat_at IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND claim_owner_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'canceled') AND claim_owner_id IS NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL AND heartbeat_at IS NULL "
            "AND finished_at IS NOT NULL)",
            name=op.f("ck_workspace_attempts_status_lifecycle_fields"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND assistant_message_id IS NOT NULL "
            "AND error_type IS NULL AND error_category IS NULL) OR "
            "(status = 'failed' AND assistant_message_id IS NULL "
            "AND error_type IS NOT NULL AND error_category IS NOT NULL) OR "
            "(status = 'canceled' AND assistant_message_id IS NULL "
            "AND error_type = 'workspace_canceled' AND error_category = 'canceled') OR "
            "(status IN ('queued', 'running') AND assistant_message_id IS NULL "
            "AND error_type IS NULL AND error_category IS NULL)",
            name=op.f("ck_workspace_attempts_status_result_fields"),
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name=op.f("ck_workspace_attempts_total_tokens_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name=op.f("fk_workspace_attempts_agent_version_id_agent_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact_index.id"],
            name=op.f("fk_workspace_attempts_artifact_id_artifact_index"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["workspace_messages.id"],
            name=op.f("fk_workspace_attempts_assistant_message_id_workspace_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["workspace_conversations.id"],
            name=op.f("fk_workspace_attempts_conversation_id_workspace_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["workspace_messages.id"],
            name=op.f("fk_workspace_attempts_user_message_id_workspace_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_attempts")),
        sa.UniqueConstraint(
            "assistant_message_id", name=op.f("uq_workspace_attempts_assistant_message_id")
        ),
        sa.UniqueConstraint(
            "user_message_id", "generation_no", name="uq_workspace_attempts_generation"
        ),
        sa.UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_workspace_attempts_idempotency"
        ),
    )
    op.create_index(
        op.f("ix_workspace_attempts_agent_version_id"),
        "workspace_attempts",
        ["agent_version_id"],
    )
    op.create_index(
        op.f("ix_workspace_attempts_lease_expires_at"),
        "workspace_attempts",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_workspace_attempts_status_created",
        "workspace_attempts",
        ["status", "created_at"],
    )
    op.create_index(
        op.f("ix_workspace_attempts_user_message_id"),
        "workspace_attempts",
        ["user_message_id"],
    )
    op.create_index(
        "uq_workspace_attempts_active_conversation",
        "workspace_attempts",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_workspace_attempts_active_conversation", table_name="workspace_attempts")
    op.drop_index(op.f("ix_workspace_attempts_user_message_id"), table_name="workspace_attempts")
    op.drop_index("ix_workspace_attempts_status_created", table_name="workspace_attempts")
    op.drop_index(op.f("ix_workspace_attempts_lease_expires_at"), table_name="workspace_attempts")
    op.drop_index(op.f("ix_workspace_attempts_agent_version_id"), table_name="workspace_attempts")
    op.drop_table("workspace_attempts")
    op.drop_index("uq_workspace_messages_active_assistant", table_name="workspace_messages")
    op.drop_index("ix_workspace_messages_conversation_turn", table_name="workspace_messages")
    op.drop_table("workspace_messages")
    op.drop_index(
        op.f("ix_workspace_conversations_pinned_agent_version_id"),
        table_name="workspace_conversations",
    )
    op.drop_index(
        "ix_workspace_conversations_owner_updated", table_name="workspace_conversations"
    )
    op.drop_index(
        op.f("ix_workspace_conversations_archived_at"), table_name="workspace_conversations"
    )
    op.drop_table("workspace_conversations")
