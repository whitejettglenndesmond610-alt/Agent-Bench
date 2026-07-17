"""add agent version lifecycle

Revision ID: f2c8a61d4e70
Revises: e8a4c7d92130
Create Date: 2026-07-16 23:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8a61d4e70"
down_revision: str | None = "e8a4c7d92130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_versions",
        sa.Column(
            "lifecycle_status",
            sa.Enum(
                "draft",
                "verified",
                "active",
                "retired",
                name="agent_version_lifecycle",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "agent_versions",
        sa.Column(
            "preflight_status",
            sa.Enum(
                "not_run",
                "running",
                "passed",
                "failed",
                name="preflight_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
            server_default="not_run",
        ),
    )
    op.add_column(
        "agent_versions", sa.Column("preflight_report_json", sa.JSON(), nullable=True)
    )
    op.add_column(
        "agent_versions", sa.Column("preflight_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        op.f("ix_agent_versions_lifecycle_status"),
        "agent_versions",
        ["lifecycle_status"],
        unique=False,
    )
    op.execute(sa.text("UPDATE agent_versions SET lifecycle_status = 'retired'"))
    op.execute(
        sa.text(
            "UPDATE agent_versions SET lifecycle_status = 'active' "
            "WHERE enabled = :enabled AND version = ("
            "SELECT MAX(candidate.version) FROM agent_versions AS candidate "
            "WHERE candidate.agent_id = agent_versions.agent_id "
            "AND candidate.enabled = :enabled)"
        ).bindparams(sa.bindparam("enabled", value=True, type_=sa.Boolean()))
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_versions_lifecycle_status"), table_name="agent_versions")
    op.drop_column("agent_versions", "preflight_at")
    op.drop_column("agent_versions", "preflight_report_json")
    op.drop_column("agent_versions", "preflight_status")
    op.drop_column("agent_versions", "lifecycle_status")
