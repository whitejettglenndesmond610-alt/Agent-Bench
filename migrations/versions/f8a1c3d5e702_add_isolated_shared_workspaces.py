"""add isolated shared workspaces

Revision ID: f8a1c3d5e702
Revises: d7b3e5f9a024
Create Date: 2026-07-19 20:30:00.000000
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "f8a1c3d5e702"
down_revision: str | None = "d7b3e5f9a024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_READ_ONLY_USER_ID = UUID("153bc83b-6584-56ec-a79b-70fe5054e537")
OLD_SOURCE_CHECK = (
    "(auth_source = 'legacy_shared' AND is_shared = true AND username IS NULL "
    "AND password_hash IS NULL) OR "
    "(auth_source = 'local' AND is_shared = false AND username IS NOT NULL "
    "AND password_hash LIKE '$argon2%')"
)
NEW_SOURCE_CHECK = (
    "(auth_source = 'legacy_shared' AND is_shared = true AND username IS NULL "
    "AND password_hash IS NULL) OR "
    "(auth_source = 'shared_browser' AND role = 'read_only' "
    "AND is_shared = true AND username IS NULL AND password_hash IS NULL "
    "AND workspace_enabled = true AND length(subject_key) = 79 "
    "AND subject_key LIKE 'shared_browser:%') OR "
    "(auth_source = 'local' AND is_shared = false AND username IS NOT NULL "
    "AND password_hash LIKE '$argon2%')"
)


def upgrade() -> None:
    sessions = sa.table("sessions", sa.column("user_id", sa.Uuid()))
    op.execute(sa.delete(sessions).where(sessions.c.user_id == LEGACY_READ_ONLY_USER_ID))

    with op.batch_alter_table("auth_users") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_auth_users_source_fields_valid"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_auth_users_auth_source"), type_="check")
        batch_op.alter_column(
            "auth_source",
            existing_type=sa.String(length=13),
            type_=sa.String(length=14),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_auth_users_auth_source"),
            "auth_source IN ('legacy_shared', 'shared_browser', 'local')",
        )
        batch_op.create_check_constraint(
            op.f("ck_auth_users_source_fields_valid"), NEW_SOURCE_CHECK
        )


def downgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    auth_users = sa.Table("auth_users", metadata, autoload_with=connection)
    sessions = sa.Table("sessions", metadata, autoload_with=connection)
    browser_ids = sa.select(auth_users.c.id).where(
        auth_users.c.auth_source == "shared_browser"
    )

    references = (
        ("workspace_conversations", "owner_user_id"),
        ("workspace_comparisons", "owner_user_id"),
        ("evaluation_cases", "owner_user_id"),
        ("evaluation_cases", "reviewed_by_user_id"),
        ("evaluation_cases", "published_by_user_id"),
        ("dataset_releases", "owner_user_id"),
        ("dataset_releases", "approved_by_user_id"),
        ("dataset_releases", "published_by_user_id"),
    )
    for table_name, column_name in references:
        table = sa.Table(table_name, metadata, autoload_with=connection)
        referenced = connection.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c[column_name].in_(browser_ids))
        )
        if referenced:
            raise RuntimeError(
                "cannot downgrade while isolated shared workspaces still own data"
            )

    connection.execute(sa.delete(sessions).where(sessions.c.user_id.in_(browser_ids)))
    connection.execute(
        sa.delete(auth_users).where(auth_users.c.auth_source == "shared_browser")
    )

    with op.batch_alter_table("auth_users") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_auth_users_source_fields_valid"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_auth_users_auth_source"), type_="check")
        batch_op.alter_column(
            "auth_source",
            existing_type=sa.String(length=14),
            type_=sa.String(length=13),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_auth_users_auth_source"),
            "auth_source IN ('legacy_shared', 'local')",
        )
        batch_op.create_check_constraint(
            op.f("ck_auth_users_source_fields_valid"), OLD_SOURCE_CHECK
        )
