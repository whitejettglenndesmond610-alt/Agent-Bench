"""merge private presets and quality branches

Revision ID: e5f2a7b8c9d0
Revises: b2c4d6e8f0a1, d4e1f6a7b8c9
Create Date: 2026-08-14 11:58:00.000000
"""

from collections.abc import Sequence

revision: str = "e5f2a7b8c9d0"
down_revision: tuple[str, str] = ("b2c4d6e8f0a1", "d4e1f6a7b8c9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
