"""exclude legacy demo runs

Revision ID: d4b6e9f80212
Revises: c3a5d8e7f901
Create Date: 2026-07-16 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b6e9f80212"
down_revision: str | None = "c3a5d8e7f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    runs = sa.table(
        "evaluation_runs",
        sa.column("name", sa.String()),
        sa.column("ranking_eligible", sa.Boolean()),
    )
    op.execute(runs.update().where(runs.c.name.like("Local Fake %")).values(ranking_eligible=False))
    op.execute(
        sa.text(
            "UPDATE agents SET status = 'disabled' WHERE provider = 'Local Demo' "
            "AND name IN ('Fake Agent Alpha', 'Fake Agent Beta')"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE agents SET status = 'enabled' WHERE provider = 'Local Demo' "
            "AND name IN ('Fake Agent Alpha', 'Fake Agent Beta')"
        )
    )
    runs = sa.table(
        "evaluation_runs",
        sa.column("name", sa.String()),
        sa.column("run_scope", sa.String()),
        sa.column("ranking_eligible", sa.Boolean()),
    )
    op.execute(
        runs.update()
        .where(runs.c.name.like("Local Fake %"), runs.c.run_scope == "comprehensive")
        .values(ranking_eligible=True)
    )
