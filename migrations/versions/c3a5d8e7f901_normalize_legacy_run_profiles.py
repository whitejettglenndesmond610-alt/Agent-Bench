"""normalize legacy run profiles

Revision ID: c3a5d8e7f901
Revises: b91f3a72c5e8
Create Date: 2026-07-16 16:30:00.000000
"""

import hashlib
import json
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "c3a5d8e7f901"
down_revision: str | None = "b91f3a72c5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SMOKE_BENCHMARKS = {"MMLU-Pro", "FinQA"}


def _metric_protocol_hash(
    metric: str,
    datasets: list[dict[str, object]],
    judge: dict[str, object],
    score: dict[str, object],
) -> str:
    relevant = [
        {"content_hash": dataset["content_hash"], "id": dataset["id"]}
        for dataset in datasets
        if metric == "overall" or dataset["ability"] == metric
    ]
    protocol = {
        "aggregation_version": "1",
        "datasets": sorted(relevant, key=lambda item: str(item["id"])),
        "judge": judge,
        "metric": metric,
        "score": score,
    }
    serialized = json.dumps(protocol, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    runs = connection.execute(
        sa.text(
            "SELECT id, judge_config_id, score_config_id FROM evaluation_runs "
            "WHERE profile_key = 'legacy'"
        )
    ).mappings()
    for run in runs:
        datasets = [
            {**dict(row), "id": str(UUID(str(row["id"])))}
            for row in connection.execute(
                sa.text(
                    "SELECT CAST(d.id AS VARCHAR) AS id, d.content_hash, d.question_count, "
                    "b.name, b.ability FROM run_dataset_versions rd "
                    "JOIN dataset_versions d ON d.id = rd.dataset_version_id "
                    "JOIN benchmarks b ON b.id = d.benchmark_id "
                    "WHERE rd.run_id = :run_id"
                ),
                {"run_id": run["id"]},
            ).mappings()
        ]
        names = {str(dataset["name"]) for dataset in datasets}
        question_count = sum(int(dataset["question_count"]) for dataset in datasets)
        is_smoke = names == SMOKE_BENCHMARKS and question_count == 20
        connection.execute(
            sa.text(
                "UPDATE evaluation_runs SET profile_key = :profile_key, "
                "run_scope = :run_scope, ranking_eligible = :ranking_eligible "
                "WHERE id = :run_id"
            ),
            {
                "profile_key": "smoke" if is_smoke else "comprehensive",
                "run_scope": "smoke" if is_smoke else "comprehensive",
                "ranking_eligible": not is_smoke,
                "run_id": run["id"],
            },
        )
        if is_smoke or not datasets:
            continue

        judge_row = connection.execute(
            sa.text(
                "SELECT provider, model, prompt_version FROM judge_configs WHERE id = :id"
            ),
            {"id": run["judge_config_id"]},
        ).mappings().one()
        score_row = connection.execute(
            sa.text(
                "SELECT reasoning_weight, retrieval_weight, data_weight, "
                "retrieval_answer_weight, retrieval_citation_weight, "
                "citation_authenticity_weight, citation_support_weight "
                "FROM score_configs WHERE id = :id"
            ),
            {"id": run["score_config_id"]},
        ).mappings().one()
        result_rows = connection.execute(
            sa.text(
                "SELECT lr.id, lr.metric FROM leaderboard_results lr "
                "JOIN leaderboard_snapshots ls ON ls.id = lr.snapshot_id "
                "WHERE ls.run_id = :run_id"
            ),
            {"run_id": run["id"]},
        ).mappings()
        for result in result_rows:
            connection.execute(
                sa.text(
                    "UPDATE leaderboard_results SET protocol_hash = :protocol_hash "
                    "WHERE id = :result_id"
                ),
                {
                    "protocol_hash": _metric_protocol_hash(
                        str(result["metric"]), datasets, dict(judge_row), dict(score_row)
                    ),
                    "result_id": result["id"],
                },
            )

    snapshots = sa.table("leaderboard_snapshots", sa.column("is_current", sa.Boolean()))
    connection.execute(snapshots.update().values(is_current=False))
    current = connection.execute(
        sa.text(
            "SELECT ls.id FROM leaderboard_snapshots ls "
            "JOIN evaluation_runs r ON r.id = ls.run_id "
            "WHERE r.ranking_eligible = :eligible AND r.run_scope = 'comprehensive' "
            "ORDER BY ls.published_at DESC, ls.id DESC LIMIT 1"
        ),
        {"eligible": True},
    ).scalar_one_or_none()
    if current is not None:
        connection.execute(
            snapshots.update()
            .where(sa.column("id") == current)
            .values(is_current=True),
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE leaderboard_results SET protocol_hash = ("
            "SELECT r.protocol_hash FROM leaderboard_snapshots ls "
            "JOIN evaluation_runs r ON r.id = ls.run_id "
            "WHERE ls.id = leaderboard_results.snapshot_id)"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE evaluation_runs SET profile_key = 'legacy', "
            "run_scope = 'comprehensive', ranking_eligible = :eligible "
            "WHERE profile_key IN ('comprehensive', 'smoke')"
        ),
        {"eligible": True},
    )
    snapshots = sa.table(
        "leaderboard_snapshots",
        sa.column("id", sa.Uuid()),
        sa.column("is_current", sa.Boolean()),
    )
    connection.execute(snapshots.update().values(is_current=False))
    current = connection.execute(
        sa.text(
            "SELECT id FROM leaderboard_snapshots "
            "ORDER BY published_at DESC, id DESC LIMIT 1"
        )
    ).scalar_one_or_none()
    if current is not None:
        connection.execute(
            snapshots.update().where(snapshots.c.id == current).values(is_current=True),
        )
