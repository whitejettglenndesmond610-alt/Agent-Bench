"""add candidate abilities

Revision ID: b8e5c02f4a71
Revises: a7d3e91c5f42
Create Date: 2026-07-17 02:08:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e5c02f4a71"
down_revision: str | None = "a7d3e91c5f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ABILITIES = ("reasoning", "retrieval", "data_reasoning")
NEW_ABILITIES = (*OLD_ABILITIES, "instruction_following", "long_context")


def ability_enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("benchmarks") as batch_op:
            batch_op.alter_column(
                "ability",
                existing_type=ability_enum("ability", OLD_ABILITIES),
                type_=ability_enum("ability", NEW_ABILITIES),
                existing_nullable=False,
            )
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.alter_column(
                "target_ability",
                existing_type=ability_enum("run_target_ability", OLD_ABILITIES),
                type_=ability_enum("run_target_ability", NEW_ABILITIES),
                existing_nullable=True,
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    placeholders = ", ".join(f"'{value}'" for value in OLD_ABILITIES)
    benchmark_count = connection.scalar(
        sa.text(f"SELECT COUNT(*) FROM benchmarks WHERE ability NOT IN ({placeholders})")
    )
    run_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM evaluation_runs "
            f"WHERE target_ability IS NOT NULL AND target_ability NOT IN ({placeholders})"
        )
    )
    if benchmark_count or run_count:
        raise RuntimeError("downgrade requires candidate ability rows to be removed")

    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("benchmarks") as batch_op:
            batch_op.alter_column(
                "ability",
                existing_type=ability_enum("ability", NEW_ABILITIES),
                type_=ability_enum("ability", OLD_ABILITIES),
                existing_nullable=False,
            )
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.alter_column(
                "target_ability",
                existing_type=ability_enum("run_target_ability", NEW_ABILITIES),
                type_=(
                    sa.String(length=14)
                    if is_sqlite
                    else ability_enum("run_target_ability", OLD_ABILITIES)
                ),
                existing_nullable=True,
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
