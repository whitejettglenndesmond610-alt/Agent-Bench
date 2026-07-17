"""add resource leases

Revision ID: f3b7c9d2e4a1
Revises: e1f6a2c4b890
Create Date: 2026-07-17 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b7c9d2e4a1"
down_revision: str | None = "e1f6a2c4b890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_leases",
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_key", sa.String(length=255), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("holder_token", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "slot_number >= 1", name=op.f("ck_resource_leases_slot_number_positive")
        ),
        sa.PrimaryKeyConstraint("holder_token", name=op.f("pk_resource_leases")),
        sa.UniqueConstraint(
            "resource_type",
            "resource_key",
            "slot_number",
            name="uq_resource_leases_resource_slot",
        ),
    )
    op.create_index(
        op.f("ix_resource_leases_lease_expires_at"),
        "resource_leases",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resource_leases_owner_id"),
        "resource_leases",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        "ix_resource_leases_resource_lookup",
        "resource_leases",
        ["resource_type", "resource_key", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_resource_leases_resource_lookup", table_name="resource_leases")
    op.drop_index(op.f("ix_resource_leases_owner_id"), table_name="resource_leases")
    op.drop_index(op.f("ix_resource_leases_lease_expires_at"), table_name="resource_leases")
    op.drop_table("resource_leases")
