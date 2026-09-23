"""add failure clusters

Revision ID: a9c1e3f5d724
Revises: f6e1d2c3b4a5
Create Date: 2026-09-23 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3f5d724"
down_revision: str | None = "f6e1d2c3b4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "failure_clusters",
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("window_days", sa.SmallInteger(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("sample_item_ids", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(name) > 0", name=op.f("ck_failure_clusters_name_nonempty")
        ),
        sa.CheckConstraint(
            "sample_count > 0", name=op.f("ck_failure_clusters_sample_count_positive")
        ),
        sa.CheckConstraint(
            "failure_count >= 0", name=op.f("ck_failure_clusters_failure_count_nonnegative")
        ),
        sa.CheckConstraint(
            "(run_id IS NOT NULL AND window_days IS NULL) OR "
            "(run_id IS NULL AND window_days BETWEEN 1 AND 90)",
            name=op.f("ck_failure_clusters_scope_valid"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_failure_clusters")),
    )
    op.create_index(op.f("ix_failure_clusters_run_id"), "failure_clusters", ["run_id"])
    op.create_index(
        "ix_failure_clusters_scope_created", "failure_clusters", ["run_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_failure_clusters_scope_created", table_name="failure_clusters")
    op.drop_index(op.f("ix_failure_clusters_run_id"), table_name="failure_clusters")
    op.drop_table("failure_clusters")
