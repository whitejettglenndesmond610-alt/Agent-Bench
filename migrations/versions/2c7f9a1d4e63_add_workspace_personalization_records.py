"""add workspace personalization records

Revision ID: 2c7f9a1d4e63
Revises: fa6d2c8e4b10
Create Date: 2026-07-20 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2c7f9a1d4e63"
down_revision: str | None = "fa6d2c8e4b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_comparisons") as batch_op:
        batch_op.add_column(
            sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_workspace_comparisons_revision_nonnegative", "revision >= 0"
        )

    with op.batch_alter_table("workspace_conversations") as batch_op:
        batch_op.drop_constraint(
            "ck_workspace_conversations_comparison_fields_valid", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_workspace_conversations_comparison_fields_valid",
            "(comparison_id IS NULL AND comparison_position IS NULL) OR "
            "(comparison_id IS NOT NULL AND comparison_position IS NOT NULL "
            "AND comparison_position BETWEEN 1 AND 5)",
        )

    op.create_table(
        "workspace_agent_favorites",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_workspace_agent_favorites_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["auth_users.id"],
            name=op.f("fk_workspace_agent_favorites_owner_user_id_auth_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id", "agent_id", name=op.f("pk_workspace_agent_favorites")
        ),
    )

    op.create_table(
        "workspace_prompt_templates",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(content) BETWEEN 1 AND 200000",
            name=op.f("ck_workspace_prompt_templates_content_length"),
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120",
            name=op.f("ck_workspace_prompt_templates_name_length"),
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name=op.f("ck_workspace_prompt_templates_revision_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["auth_users.id"],
            name=op.f("fk_workspace_prompt_templates_owner_user_id_auth_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_prompt_templates")),
        sa.UniqueConstraint(
            "owner_user_id",
            "name",
            name="uq_workspace_prompt_templates_owner_name",
        ),
    )
    op.create_index(
        "ix_workspace_prompt_templates_owner_updated",
        "workspace_prompt_templates",
        ["owner_user_id", "updated_at"],
    )

    op.create_table(
        "workspace_attempt_annotations",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "feedback",
            sa.Enum(
                "up",
                "down",
                name="workspace_attempt_feedback",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("bookmarked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("private_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "feedback IS NOT NULL OR bookmarked = true OR private_note IS NOT NULL",
            name=op.f("ck_workspace_attempt_annotations_meaningful"),
        ),
        sa.CheckConstraint(
            "private_note IS NULL OR length(private_note) BETWEEN 1 AND 10000",
            name=op.f("ck_workspace_attempt_annotations_private_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "conversation_id"],
            ["workspace_attempts.id", "workspace_attempts.conversation_id"],
            name="fk_workspace_attempt_annotations_attempt_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "owner_user_id"],
            ["workspace_conversations.id", "workspace_conversations.owner_user_id"],
            name="fk_workspace_attempt_annotations_conversation_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_workspace_attempt_annotations")),
    )


def downgrade() -> None:
    op.drop_table("workspace_attempt_annotations")
    op.drop_index(
        "ix_workspace_prompt_templates_owner_updated",
        table_name="workspace_prompt_templates",
    )
    op.drop_table("workspace_prompt_templates")
    op.drop_table("workspace_agent_favorites")

    with op.batch_alter_table("workspace_conversations") as batch_op:
        batch_op.drop_constraint(
            "ck_workspace_conversations_comparison_fields_valid", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_workspace_conversations_comparison_fields_valid",
            "(comparison_id IS NULL AND comparison_position IS NULL) OR "
            "(comparison_id IS NOT NULL AND comparison_position BETWEEN 1 AND 5)",
        )

    with op.batch_alter_table("workspace_comparisons") as batch_op:
        batch_op.drop_constraint(
            "ck_workspace_comparisons_revision_nonnegative", type_="check"
        )
        batch_op.drop_column("archived_at")
        batch_op.drop_column("revision")
