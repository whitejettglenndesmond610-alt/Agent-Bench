"""normalize private model aliases

Revision ID: 9d2a6f4c8b71
Revises: 7a4e1c9b2d60
Create Date: 2026-07-20 07:00:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d2a6f4c8b71"
down_revision: str | None = "7a4e1c9b2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALIASES = (
    ("dashscope", "qwen plus", "qwen-plus"),
    ("dashscope", "qwen max", "qwen-max"),
    ("dashscope", "qwen math turbo", "qwen-math-turbo"),
    ("dashscope", "qwen3.7 plus", "qwen3.7-plus"),
    ("deepseek", "deepseek v4 pro", "deepseek-v4-pro"),
    ("deepseek", "deepseek v4 flash", "deepseek-v4-flash"),
    ("deepseek", "deepseek chat", "deepseek-chat"),
    ("deepseek", "deepseek reasoner", "deepseek-reasoner"),
    ("openai", "gpt 4.1", "gpt-4.1"),
    ("openai", "gpt 4.1 mini", "gpt-4.1-mini"),
    ("openai", "gpt 4o", "gpt-4o"),
    ("openai", "gpt 4o mini", "gpt-4o-mini"),
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
        "WHERE private_preset = :preset AND lower(trim(private_model)) = :alias"
    )
    for preset, alias, canonical in ALIASES:
        op.execute(
            statement.bindparams(
                preset=preset,
                alias=alias,
                canonical=canonical,
                request_template=request_template(canonical),
            )
        )


def downgrade() -> None:
    # Canonical provider model IDs remain valid when the migration is downgraded.
    pass
