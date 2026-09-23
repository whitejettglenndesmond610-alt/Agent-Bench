"""add workspace attempt partial answer

Revision ID: f6e1d2c3b4a5
Revises: e4a8b2d6f913
Create Date: 2026-09-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6e1d2c3b4a5"
down_revision: str | None = "e4a8b2d6f913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace_attempts") as batch_op:
        batch_op.add_column(sa.Column("partial_answer", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace_attempts") as batch_op:
        batch_op.drop_column("partial_answer")
