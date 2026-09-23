"""add interactive simulator token usage

Revision ID: e4a8b2d6f913
Revises: d3f7a1c9e825
Create Date: 2026-09-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a8b2d6f913"
down_revision: str | None = "d3f7a1c9e825"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_items") as batch_op:
        batch_op.add_column(sa.Column("simulator_input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("simulator_output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_items") as batch_op:
        batch_op.drop_column("simulator_output_tokens")
        batch_op.drop_column("simulator_input_tokens")
