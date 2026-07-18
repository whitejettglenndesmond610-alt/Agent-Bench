"""add auth users and link sessions

Revision ID: a4d9c2e7f601
Revises: f3b7c9d2e4a1
Create Date: 2026-07-19 09:00:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "a4d9c2e7f601"
down_revision: str | None = "f3b7c9d2e4a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READ_ONLY_USER_ID = UUID("153bc83b-6584-56ec-a79b-70fe5054e537")
OPERATOR_USER_ID = UUID("f91b3c4a-9dbd-59ea-922f-56b64a56f801")


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("subject_key", sa.String(length=200), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "read_only",
                "operator",
                name="auth_user_session_role",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "auth_source",
            sa.Enum(
                "legacy_shared",
                "local",
                name="auth_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("is_shared", sa.Boolean(), nullable=False),
        sa.Column("workspace_enabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(display_name) > 0",
            name=op.f("ck_auth_users_display_name_nonempty"),
        ),
        sa.CheckConstraint(
            "(auth_source = 'legacy_shared' AND is_shared = true AND username IS NULL "
            "AND password_hash IS NULL) OR "
            "(auth_source = 'local' AND is_shared = false AND username IS NOT NULL "
            "AND password_hash LIKE '$argon2%')",
            name=op.f("ck_auth_users_source_fields_valid"),
        ),
        sa.CheckConstraint(
            "length(subject_key) > 0", name=op.f("ck_auth_users_subject_key_nonempty")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_users")),
        sa.UniqueConstraint("subject_key", name=op.f("uq_auth_users_subject_key")),
        sa.UniqueConstraint("username", name=op.f("uq_auth_users_username")),
    )
    op.create_index(op.f("ix_auth_users_auth_source"), "auth_users", ["auth_source"])
    op.create_index(op.f("ix_auth_users_disabled_at"), "auth_users", ["disabled_at"])
    op.create_index(op.f("ix_auth_users_role"), "auth_users", ["role"])

    now = datetime.now(UTC)
    auth_users = sa.table(
        "auth_users",
        sa.column("id", sa.Uuid()),
        sa.column("subject_key", sa.String()),
        sa.column("username", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("role", sa.String()),
        sa.column("auth_source", sa.String()),
        sa.column("password_hash", sa.Text()),
        sa.column("is_shared", sa.Boolean()),
        sa.column("workspace_enabled", sa.Boolean()),
        sa.column("disabled_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        auth_users,
        [
            {
                "id": READ_ONLY_USER_ID,
                "subject_key": "legacy_shared:read_only",
                "username": None,
                "display_name": "Shared read-only",
                "role": "read_only",
                "auth_source": "legacy_shared",
                "password_hash": None,
                "is_shared": True,
                "workspace_enabled": False,
                "disabled_at": None,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": OPERATOR_USER_ID,
                "subject_key": "legacy_shared:operator",
                "username": None,
                "display_name": "Shared operator",
                "role": "operator",
                "auth_source": "legacy_shared",
                "password_hash": None,
                "is_shared": True,
                "workspace_enabled": True,
                "disabled_at": None,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_sessions_user_id_auth_users"),
            "auth_users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.execute(
        sa.text(
            "UPDATE sessions SET user_id = CASE role "
            "WHEN 'read_only' THEN :read_only_id WHEN 'operator' THEN :operator_id END"
        ).bindparams(read_only_id=READ_ONLY_USER_ID, operator_id=OPERATOR_USER_ID)
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint(op.f("fk_sessions_user_id_auth_users"), type_="foreignkey")
        batch_op.drop_column("user_id")
    op.drop_index(op.f("ix_auth_users_role"), table_name="auth_users")
    op.drop_index(op.f("ix_auth_users_disabled_at"), table_name="auth_users")
    op.drop_index(op.f("ix_auth_users_auth_source"), table_name="auth_users")
    op.drop_table("auth_users")
