"""add quality review tasks

Revision ID: c3d0e5f6a7b8
Revises: b2c9d4e5f6a7
Create Date: 2026-08-13 18:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d0e5f6a7b8"
down_revision: str | None = "b2c9d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_review_tasks",
        sa.Column("execution_item_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "unassigned",
                "assigned",
                "in_review",
                "completed",
                name="quality_task_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="unassigned",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum(
                "low",
                "normal",
                "high",
                "urgent",
                name="quality_task_priority",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="normal",
            nullable=False,
        ),
        sa.Column("assigned_to_user_id", sa.Uuid(), nullable=True),
        sa.Column("assignee_subject_key", sa.String(length=200), nullable=True),
        sa.Column("assignee_display_name", sa.String(length=160), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_quality_review_tasks_revision_nonnegative")
        ),
        sa.CheckConstraint(
            "(status = 'unassigned' AND assigned_to_user_id IS NULL) OR "
            "(status IN ('assigned', 'in_review') AND assigned_to_user_id IS NOT NULL) OR "
            "status = 'completed'",
            name=op.f("ck_quality_review_tasks_assignment_lifecycle_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status != 'completed' AND completed_at IS NULL)",
            name=op.f("ck_quality_review_tasks_completion_lifecycle_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_user_id"], ["auth_users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["execution_item_id"], ["execution_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_review_tasks")),
        sa.UniqueConstraint(
            "execution_item_id", name=op.f("uq_quality_review_tasks_execution_item_id")
        ),
    )
    op.create_index(
        "ix_quality_review_tasks_status_priority",
        "quality_review_tasks",
        ["status", "priority", "updated_at"],
    )
    op.create_index(
        "ix_quality_review_tasks_assignee_status",
        "quality_review_tasks",
        ["assigned_to_user_id", "status"],
    )

    op.execute(
        sa.text(
            """
            INSERT INTO quality_review_tasks (
                id, execution_item_id, status, priority, revision,
                created_at, updated_at, completed_at
            )
            SELECT
                f.execution_item_id,
                f.execution_item_id,
                CASE WHEN q.execution_item_id IS NULL THEN 'unassigned' ELSE 'completed' END,
                'normal',
                0,
                MIN(f.created_at),
                MAX(COALESCE(q.updated_at, f.updated_at)),
                MAX(q.reviewed_at)
            FROM bad_pattern_findings AS f
            LEFT JOIN quality_reviews AS q ON q.execution_item_id = f.execution_item_id
            GROUP BY f.execution_item_id, q.execution_item_id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_review_tasks_assignee_status", table_name="quality_review_tasks"
    )
    op.drop_index(
        "ix_quality_review_tasks_status_priority", table_name="quality_review_tasks"
    )
    op.drop_table("quality_review_tasks")
