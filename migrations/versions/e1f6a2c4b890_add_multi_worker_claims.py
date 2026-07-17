"""add multi-worker claims

Revision ID: e1f6a2c4b890
Revises: d0a4e83f7c21
Create Date: 2026-07-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f6a2c4b890"
down_revision: str | None = "d0a4e83f7c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    active_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM evaluation_runs WHERE active_slot IS NOT NULL")
    )
    if active_count:
        raise RuntimeError("migration requires all active evaluation runs to be stopped")

    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.drop_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"), type_="check"
            )
            batch_op.add_column(sa.Column("claim_owner_id", sa.String(length=120), nullable=True))
            batch_op.add_column(
                sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.create_check_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"),
                "(status IN ('running', 'paused', 'scoring') AND active_slot BETWEEN 1 AND 3 "
                "AND claim_owner_id IS NOT NULL AND claimed_at IS NOT NULL) OR "
                "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL "
                "AND claim_owner_id IS NULL AND claimed_at IS NULL)",
            )
            batch_op.create_index(
                op.f("ix_evaluation_runs_claim_owner_id"), ["claim_owner_id"], unique=False
            )
            batch_op.create_index(
                op.f("ix_evaluation_runs_claimed_at"), ["claimed_at"], unique=False
            )
        with op.batch_alter_table("worker_leases") as batch_op:
            batch_op.alter_column(
                "name",
                existing_type=sa.String(length=80),
                type_=sa.String(length=120),
                existing_nullable=False,
            )
            batch_op.create_index(
                op.f("ix_worker_leases_lease_expires_at"),
                ["lease_expires_at"],
                unique=False,
            )
        connection.execute(sa.text("UPDATE worker_leases SET name = owner_id"))
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    active_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM evaluation_runs WHERE active_slot IS NOT NULL")
    )
    if active_count:
        raise RuntimeError("downgrade requires all active evaluation runs to be stopped")

    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("worker_leases") as batch_op:
            batch_op.drop_index(op.f("ix_worker_leases_lease_expires_at"))
            batch_op.alter_column(
                "name",
                existing_type=sa.String(length=120),
                type_=sa.String(length=80),
                existing_nullable=False,
            )
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.drop_index(op.f("ix_evaluation_runs_claimed_at"))
            batch_op.drop_index(op.f("ix_evaluation_runs_claim_owner_id"))
            batch_op.drop_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"), type_="check"
            )
            batch_op.drop_column("claimed_at")
            batch_op.drop_column("claim_owner_id")
            batch_op.create_check_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"),
                "(status IN ('running', 'paused', 'scoring') "
                "AND active_slot BETWEEN 1 AND 3) OR "
                "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL)",
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
