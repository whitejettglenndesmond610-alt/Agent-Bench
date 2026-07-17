"""add agent prompt limit

Revision ID: e8a4c7d92130
Revises: d4b6e9f80212
Create Date: 2026-07-16 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8a4c7d92130"
down_revision: str | None = "d4b6e9f80212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_versions", sa.Column("max_prompt_characters", sa.Integer(), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE agent_versions SET max_prompt_characters = 4000 "
            "WHERE agent_id IN (SELECT id FROM agents WHERE name = 'Qwen Math Turbo')"
        )
    )


def downgrade() -> None:
    op.drop_column("agent_versions", "max_prompt_characters")
