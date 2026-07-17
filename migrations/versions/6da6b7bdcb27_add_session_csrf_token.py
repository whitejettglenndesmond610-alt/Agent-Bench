"""add session csrf token

Revision ID: 6da6b7bdcb27
Revises: 84f14ec48208
Create Date: 2026-07-16 01:16:58.360045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6da6b7bdcb27"
down_revision: str | None = "84f14ec48208"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing sessions cannot be assigned a trustworthy CSRF token, so force reauthentication.
    op.execute(sa.text("DELETE FROM sessions"))
    op.add_column("sessions", sa.Column("csrf_token_hash", sa.String(length=64), nullable=False))


def downgrade() -> None:
    op.drop_column("sessions", "csrf_token_hash")
