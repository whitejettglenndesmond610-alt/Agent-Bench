"""expand active run slots

Revision ID: a7d3e91c5f42
Revises: f2c8a61d4e70
Create Date: 2026-07-17 00:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e91c5f42"
down_revision: str | None = "f2c8a61d4e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.drop_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"), type_="check"
            )
            batch_op.create_check_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"),
                "(status IN ('running', 'paused', 'scoring') "
                "AND active_slot BETWEEN 1 AND 3) OR "
                "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL)",
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    active_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM evaluation_runs WHERE active_slot IS NOT NULL")
    )
    if active_count and active_count > 1:
        raise RuntimeError("downgrade requires at most one active evaluation run")
    if active_count:
        connection.execute(
            sa.text("UPDATE evaluation_runs SET active_slot = 1 WHERE active_slot IS NOT NULL")
        )
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.drop_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"), type_="check"
            )
            batch_op.create_check_constraint(
                op.f("ck_evaluation_runs_active_slot_matches_status"),
                "(status IN ('running', 'paused', 'scoring') AND active_slot = 1) OR "
                "(status NOT IN ('running', 'paused', 'scoring') AND active_slot IS NULL)",
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
