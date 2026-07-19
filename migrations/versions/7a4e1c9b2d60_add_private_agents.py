"""add private agents

Revision ID: 7a4e1c9b2d60
Revises: 2c7f9a1d4e63
Create Date: 2026-07-20 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a4e1c9b2d60"
down_revision: str | None = "2c7f9a1d4e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_constraint("uq_agents_name", type_="unique")
        batch_op.add_column(sa.Column("owner_user_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_agents_owner_user_id_auth_users",
            "auth_users",
            ["owner_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_agents_private_owner_name",
        "agents",
        ["owner_user_id", "name"],
        unique=True,
        sqlite_where=sa.text("owner_user_id IS NOT NULL AND status != 'disabled'"),
        postgresql_where=sa.text("owner_user_id IS NOT NULL AND status != 'disabled'"),
    )
    op.create_index(
        "uq_agents_public_name",
        "agents",
        ["name"],
        unique=True,
        sqlite_where=sa.text("owner_user_id IS NULL"),
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_index(
        "ix_agents_owner_created", "agents", ["owner_user_id", "created_at"]
    )

    with op.batch_alter_table("agent_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "private_preset",
                sa.String(length=9),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("private_model", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column(
                "private_health_status",
                sa.String(length=14),
                server_default=sa.text("'not_applicable'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("private_health_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("private_health_error_code", sa.String(length=80), nullable=True)
        )
        batch_op.add_column(
            sa.Column("private_health_latency_ms", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_agent_preset",
            "private_preset IN ('dashscope', 'deepseek', 'openai')",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_agent_health",
            "private_health_status IN "
            "('not_applicable', 'untested', 'healthy', 'unhealthy', 'revoked')",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_health_latency_nonnegative",
            "private_health_latency_ms IS NULL OR private_health_latency_ms >= 0",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_model_length",
            "private_model IS NULL OR length(private_model) BETWEEN 1 AND 200",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_health_error_code_length",
            "private_health_error_code IS NULL OR "
            "length(private_health_error_code) BETWEEN 1 AND 80",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_config_valid",
            "(private_preset IS NULL AND private_model IS NULL "
            "AND private_health_status = 'not_applicable' "
            "AND private_health_checked_at IS NULL "
            "AND private_health_error_code IS NULL "
            "AND private_health_latency_ms IS NULL) OR "
            "(private_preset IS NOT NULL AND private_model IS NOT NULL "
            "AND private_health_status != 'not_applicable' "
            "AND auth_type = 'bearer' AND citations_jsonpath IS NULL "
            "AND timeout_seconds = 120 AND concurrency = 1)",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_untested_metadata_empty",
            "private_health_status != 'untested' OR "
            "(private_health_checked_at IS NULL AND private_health_error_code IS NULL "
            "AND private_health_latency_ms IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_healthy_metadata_valid",
            "private_health_status != 'healthy' OR "
            "(private_health_checked_at IS NOT NULL "
            "AND private_health_error_code IS NULL "
            "AND private_health_latency_ms IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_unhealthy_metadata_valid",
            "private_health_status != 'unhealthy' OR "
            "(private_health_checked_at IS NOT NULL "
            "AND private_health_error_code IS NOT NULL "
            "AND private_health_latency_ms IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_agent_versions_private_token_present",
            "private_preset IS NULL OR private_health_status = 'revoked' "
            "OR encrypted_token IS NOT NULL",
        )
    op.create_index(
        "ix_agent_versions_private_health_status",
        "agent_versions",
        ["private_health_status"],
    )


def downgrade() -> None:
    private_agent_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM agents WHERE owner_user_id IS NOT NULL")
    ).scalar_one()
    if private_agent_count:
        raise RuntimeError("cannot downgrade while private Agents remain")

    op.drop_index(
        "ix_agent_versions_private_health_status", table_name="agent_versions"
    )
    with op.batch_alter_table("agent_versions") as batch_op:
        batch_op.drop_constraint(
            "ck_agent_versions_private_token_present", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_unhealthy_metadata_valid", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_healthy_metadata_valid", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_untested_metadata_empty", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_config_valid", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_health_error_code_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_model_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_health_latency_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_agent_health", type_="check"
        )
        batch_op.drop_constraint(
            "ck_agent_versions_private_agent_preset", type_="check"
        )
        batch_op.drop_column("private_health_latency_ms")
        batch_op.drop_column("private_health_error_code")
        batch_op.drop_column("private_health_checked_at")
        batch_op.drop_column("private_health_status")
        batch_op.drop_column("private_model")
        batch_op.drop_column("private_preset")

    op.drop_index("ix_agents_owner_created", table_name="agents")
    op.drop_index("uq_agents_private_owner_name", table_name="agents")
    op.drop_index("uq_agents_public_name", table_name="agents")
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_constraint(
            "fk_agents_owner_user_id_auth_users", type_="foreignkey"
        )
        batch_op.drop_column("owner_user_id")
        batch_op.create_unique_constraint("uq_agents_name", ["name"])
