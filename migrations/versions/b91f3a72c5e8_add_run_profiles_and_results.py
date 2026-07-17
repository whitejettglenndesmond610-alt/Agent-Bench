"""add run profiles and leaderboard results

Revision ID: b91f3a72c5e8
Revises: d6c19d2a9f41
Create Date: 2026-07-16 12:00:00.000000
"""

import json
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "b91f3a72c5e8"
down_revision: str | None = "d6c19d2a9f41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ZERO_HASH = "0" * 64
ABILITY_METRICS = ("reasoning", "retrieval", "data_reasoning")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _uuid(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and Decimal(0) <= result <= Decimal(100) else None


def _legacy_result_rows(
    snapshot_id: UUID,
    rankings: Any,
    protocol_hash: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_ranking in _json_list(rankings):
        if not isinstance(raw_ranking, dict):
            continue
        run_agent_id = _uuid(raw_ranking.get("run_agent_id"))
        agent_id = _uuid(raw_ranking.get("agent_id"))
        agent_version_id = _uuid(raw_ranking.get("agent_version_id"))
        if run_agent_id is None or agent_id is None or agent_version_id is None:
            continue
        benchmark_scores = [
            score
            for score in _json_list(raw_ranking.get("benchmark_scores"))
            if isinstance(score, dict)
        ]
        metric_scores: list[tuple[str, Decimal, int]] = []
        overall_score = _decimal(raw_ranking.get("overall_score"))
        if overall_score is not None:
            metric_scores.append(("overall", overall_score, len(benchmark_scores)))
        ability_scores = raw_ranking.get("ability_scores")
        if isinstance(ability_scores, dict):
            for metric in ABILITY_METRICS:
                matching = [
                    score for score in benchmark_scores if score.get("ability") == metric
                ]
                score = _decimal(ability_scores.get(metric))
                if matching and score is not None:
                    metric_scores.append((metric, score, len(matching)))
        rows.extend(
            {
                "id": uuid4(),
                "snapshot_id": snapshot_id,
                "run_agent_id": run_agent_id,
                "agent_id": agent_id,
                "agent_version_id": agent_version_id,
                "metric": metric,
                "score": score,
                "protocol_hash": protocol_hash,
                "benchmark_count": benchmark_count,
            }
            for metric, score, benchmark_count in metric_scores
        )
    return rows


def upgrade() -> None:
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    columns = [
        sa.Column(
            "profile_key",
            sa.String(length=80),
            server_default="comprehensive",
            nullable=False,
        ),
        sa.Column(
            "run_scope",
            sa.Enum(
                "comprehensive",
                "specialist",
                "smoke",
                name="run_scope",
                native_enum=False,
                create_constraint=not is_sqlite,
            ),
            server_default="comprehensive",
            nullable=False,
        ),
        sa.Column(
            "target_ability",
            sa.Enum(
                "reasoning",
                "retrieval",
                "data_reasoning",
                name="run_target_ability",
                native_enum=False,
                create_constraint=not is_sqlite,
            ),
            nullable=True,
        ),
        sa.Column(
            "ranking_eligible", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "protocol_hash",
            sa.String(length=64),
            server_default=ZERO_HASH,
            nullable=False,
        ),
    ]
    if is_sqlite:
        for column in columns:
            op.add_column("evaluation_runs", column)
    else:
        with op.batch_alter_table("evaluation_runs") as batch_op:
            for column in columns:
                batch_op.add_column(column)

    op.execute(
        sa.text(
            "UPDATE evaluation_runs "
            "SET profile_key = 'legacy', protocol_hash = dataset_bundle_hash"
        )
    )

    op.create_table(
        "leaderboard_results",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("run_agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("protocol_hash", sa.String(length=64), nullable=False),
        sa.Column("benchmark_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_leaderboard_results_agent_id_agents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name=op.f("fk_leaderboard_results_agent_version_id_agent_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_agent_id"],
            ["run_agents.id"],
            name=op.f("fk_leaderboard_results_run_agent_id_run_agents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["leaderboard_snapshots.id"],
            name=op.f("fk_leaderboard_results_snapshot_id_leaderboard_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leaderboard_results")),
        sa.UniqueConstraint(
            "snapshot_id",
            "agent_version_id",
            "metric",
            name="uq_leaderboard_results_snapshot_agent_version_metric",
        ),
    )
    op.create_index(
        op.f("ix_leaderboard_results_agent_id"),
        "leaderboard_results",
        ["agent_id"],
        unique=False,
    )

    connection = op.get_bind()
    snapshots = sa.table(
        "leaderboard_snapshots",
        sa.column("id", sa.Uuid()),
        sa.column("run_id", sa.Uuid()),
        sa.column("rankings_json", sa.JSON()),
    )
    runs = sa.table(
        "evaluation_runs",
        sa.column("id", sa.Uuid()),
        sa.column("protocol_hash", sa.String(length=64)),
    )
    results = sa.table(
        "leaderboard_results",
        sa.column("id", sa.Uuid()),
        sa.column("snapshot_id", sa.Uuid()),
        sa.column("run_agent_id", sa.Uuid()),
        sa.column("agent_id", sa.Uuid()),
        sa.column("agent_version_id", sa.Uuid()),
        sa.column("metric", sa.String(length=50)),
        sa.column("score", sa.Numeric(precision=38, scale=18)),
        sa.column("protocol_hash", sa.String(length=64)),
        sa.column("benchmark_count", sa.Integer()),
    )
    rows: list[dict[str, Any]] = []
    for snapshot in connection.execute(
        sa.select(
            snapshots.c.id,
            snapshots.c.rankings_json,
            runs.c.protocol_hash,
        ).join(runs, runs.c.id == snapshots.c.run_id)
    ).mappings():
        snapshot_id = _uuid(snapshot["id"])
        if snapshot_id is None:
            continue
        rows.extend(
            _legacy_result_rows(
                snapshot_id,
                snapshot["rankings_json"],
                str(snapshot["protocol_hash"]),
            )
        )
    if rows:
        connection.execute(results.insert(), rows)
    op.create_index(
        "ix_leaderboard_results_metric_protocol_hash",
        "leaderboard_results",
        ["metric", "protocol_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_leaderboard_results_metric_protocol_hash", table_name="leaderboard_results"
    )
    op.drop_index(op.f("ix_leaderboard_results_agent_id"), table_name="leaderboard_results")
    op.drop_table("leaderboard_results")

    if op.get_bind().dialect.name == "sqlite":
        for column_name in (
            "protocol_hash",
            "ranking_eligible",
            "target_ability",
            "run_scope",
            "profile_key",
        ):
            op.drop_column("evaluation_runs", column_name)
        return

    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.alter_column(
            "target_ability",
            existing_type=sa.Enum(
                "reasoning",
                "retrieval",
                "data_reasoning",
                name="run_target_ability",
                native_enum=False,
                create_constraint=True,
            ),
            type_=sa.String(length=20),
        )
        batch_op.alter_column(
            "run_scope",
            existing_type=sa.Enum(
                "comprehensive",
                "specialist",
                "smoke",
                name="run_scope",
                native_enum=False,
                create_constraint=True,
            ),
            type_=sa.String(length=20),
        )

    with op.batch_alter_table(
        "evaluation_runs",
        reflect_args=(
            sa.Column("run_scope", sa.String(length=20)),
            sa.Column("target_ability", sa.String(length=20)),
        ),
    ) as batch_op:
        batch_op.drop_column("protocol_hash")
        batch_op.drop_column("ranking_eligible")
        batch_op.drop_column("target_ability")
        batch_op.drop_column("run_scope")
        batch_op.drop_column("profile_key")
