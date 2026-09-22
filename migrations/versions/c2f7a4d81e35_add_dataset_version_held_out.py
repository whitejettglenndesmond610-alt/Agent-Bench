"""add dataset version held-out flag

Revision ID: c2f7a4d81e35
Revises: b7e4c1d9f260
Create Date: 2026-09-22 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2f7a4d81e35"
down_revision: str | None = "b7e4c1d9f260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "held_out",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.create_index("ix_dataset_versions_held_out", ["held_out"])


def downgrade() -> None:
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.drop_index("ix_dataset_versions_held_out")
        batch_op.drop_column("held_out")
