"""remap retired deepseek private model aliases

Revision ID: a1b8c3d4e5f6
Revises: 9d2a6f4c8b71
Create Date: 2026-08-13 00:40:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b8c3d4e5f6"
down_revision: str | None = "9d2a6f4c8b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# deepseek-chat / deepseek-reasoner were discontinued on 2026-07-24.
RETIRED_TO_CANONICAL = (
    ("deepseek-chat", "deepseek-v4-flash"),
    ("deepseek-reasoner", "deepseek-v4-flash"),
)


def request_template(model: str) -> str:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "{{prompt}}"}],
            "stream": False,
        },
        separators=(",", ":"),
    )


def upgrade() -> None:
    statement = sa.text(
        "UPDATE agent_versions SET private_model = :canonical, "
        "request_template = :request_template, updated_at = CURRENT_TIMESTAMP "
        "WHERE private_preset = 'deepseek' AND lower(trim(private_model)) = :retired"
    )
    for retired, canonical in RETIRED_TO_CANONICAL:
        op.execute(
            statement.bindparams(
                retired=retired,
                canonical=canonical,
                request_template=request_template(canonical),
            )
        )


def downgrade() -> None:
    # Canonical current model IDs remain valid after downgrade.
    pass
