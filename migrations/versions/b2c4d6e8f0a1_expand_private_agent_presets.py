"""expand private agent presets

Revision ID: b2c4d6e8f0a1
Revises: a1b8c3d4e5f6
Create Date: 2026-08-14 00:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2c4d6e8f0a1"
down_revision: str | None = "a1b8c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PRESETS = (
    "dashscope",
    "deepseek",
    "openai",
    "zhipu",
    "grok",
    "claude",
    "mimo",
)
OLD_PRESETS = ("dashscope", "deepseek", "openai")


def upgrade() -> None:
    with op.batch_alter_table("agent_versions") as batch_op:
        batch_op.drop_constraint("ck_agent_versions_private_agent_preset", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_versions_private_agent_preset",
            "private_preset IS NULL OR private_preset IN "
            f"({', '.join(repr(item) for item in NEW_PRESETS)})",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_versions") as batch_op:
        batch_op.drop_constraint("ck_agent_versions_private_agent_preset", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_versions_private_agent_preset",
            "private_preset IS NULL OR private_preset IN "
            f"({', '.join(repr(item) for item in OLD_PRESETS)})",
        )
