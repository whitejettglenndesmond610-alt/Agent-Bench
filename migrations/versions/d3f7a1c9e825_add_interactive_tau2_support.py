"""add interactive tau2 support

Revision ID: d3f7a1c9e825
Revises: c2f7a4d81e35
Create Date: 2026-09-22 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f7a1c9e825"
down_revision: str | None = "c2f7a4d81e35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ABILITIES = (
    "reasoning",
    "retrieval",
    "data_reasoning",
    "instruction_following",
    "long_context",
)
NEW_ABILITIES = (*OLD_ABILITIES, "agentic")
OLD_QUESTION_TYPES = ("multiple_choice", "short_answer", "open_ended")
NEW_QUESTION_TYPES = (*OLD_QUESTION_TYPES, "interactive")
OLD_ARTIFACT_KINDS = ("agent_response", "citation_content", "judge_response")
NEW_ARTIFACT_KINDS = (*OLD_ARTIFACT_KINDS, "trajectory")


def value_enum(name: str, values: tuple[str, ...]) -> sa.Enum:
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
                existing_type=value_enum("ability", OLD_ABILITIES),
                type_=value_enum("ability", NEW_ABILITIES),
                existing_nullable=False,
            )
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.alter_column(
                "target_ability",
                existing_type=value_enum("run_target_ability", OLD_ABILITIES),
                type_=value_enum("run_target_ability", NEW_ABILITIES),
                existing_nullable=True,
            )
            batch_op.alter_column(
                "score_config_id",
                existing_type=sa.Uuid(),
                nullable=True,
            )
        with op.batch_alter_table("evaluation_cases") as batch_op:
            batch_op.alter_column(
                "ability",
                existing_type=value_enum("evaluation_case_ability", OLD_ABILITIES),
                type_=value_enum("evaluation_case_ability", NEW_ABILITIES),
                existing_nullable=True,
            )
            batch_op.alter_column(
                "question_type",
                existing_type=value_enum("evaluation_case_question_type", OLD_QUESTION_TYPES),
                type_=value_enum("evaluation_case_question_type", NEW_QUESTION_TYPES),
                existing_nullable=False,
            )
        with op.batch_alter_table("questions") as batch_op:
            batch_op.alter_column(
                "type",
                existing_type=value_enum("question_type", OLD_QUESTION_TYPES),
                type_=value_enum("question_type", NEW_QUESTION_TYPES),
                existing_nullable=False,
            )
        with op.batch_alter_table("artifact_index") as batch_op:
            batch_op.alter_column(
                "kind",
                existing_type=value_enum("artifact_kind", OLD_ARTIFACT_KINDS),
                type_=value_enum("artifact_kind", NEW_ARTIFACT_KINDS),
                existing_nullable=False,
            )
        with op.batch_alter_table("execution_items") as batch_op:
            batch_op.add_column(sa.Column("task_reward", sa.Numeric(5, 4), nullable=True))
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    tau2_rows = connection.scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM benchmarks WHERE ability = 'agentic') + "
            "(SELECT COUNT(*) FROM evaluation_runs WHERE target_ability = 'agentic') + "
            "(SELECT COUNT(*) FROM questions WHERE type = 'interactive') + "
            "(SELECT COUNT(*) FROM artifact_index WHERE kind = 'trajectory')"
        )
    )
    if tau2_rows:
        raise RuntimeError("downgrade requires tau2 interactive rows to be removed")

    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("execution_items") as batch_op:
            batch_op.drop_column("task_reward")
        with op.batch_alter_table("artifact_index") as batch_op:
            batch_op.alter_column(
                "kind",
                existing_type=value_enum("artifact_kind", NEW_ARTIFACT_KINDS),
                type_=value_enum("artifact_kind", OLD_ARTIFACT_KINDS),
                existing_nullable=False,
            )
        with op.batch_alter_table("questions") as batch_op:
            batch_op.alter_column(
                "type",
                existing_type=value_enum("question_type", NEW_QUESTION_TYPES),
                type_=value_enum("question_type", OLD_QUESTION_TYPES),
                existing_nullable=False,
            )
        with op.batch_alter_table("evaluation_runs") as batch_op:
            batch_op.alter_column(
                "target_ability",
                existing_type=value_enum("run_target_ability", NEW_ABILITIES),
                type_=value_enum("run_target_ability", OLD_ABILITIES),
                existing_nullable=True,
            )
            batch_op.alter_column(
                "score_config_id",
                existing_type=sa.Uuid(),
                nullable=False,
            )
        with op.batch_alter_table("evaluation_cases") as batch_op:
            batch_op.alter_column(
                "ability",
                existing_type=value_enum("evaluation_case_ability", NEW_ABILITIES),
                type_=value_enum("evaluation_case_ability", OLD_ABILITIES),
                existing_nullable=True,
            )
            batch_op.alter_column(
                "question_type",
                existing_type=value_enum("evaluation_case_question_type", NEW_QUESTION_TYPES),
                type_=value_enum("evaluation_case_question_type", OLD_QUESTION_TYPES),
                existing_nullable=False,
            )
        with op.batch_alter_table("benchmarks") as batch_op:
            batch_op.alter_column(
                "ability",
                existing_type=value_enum("ability", NEW_ABILITIES),
                type_=value_enum("ability", OLD_ABILITIES),
                existing_nullable=False,
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
