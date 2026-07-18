import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from agent_eval.auth.identity import LEGACY_OPERATOR_USER_ID
from agent_eval.core.settings import Settings
from agent_eval.db.engine import create_engine
from agent_eval.db.enums import AgentStatus, AuthType
from agent_eval.db.models import Agent, AgentVersion, ScoreConfig
from agent_eval.db.session import create_session_factory, session_scope

BUSINESS_TABLES = {
    "agents",
    "agent_versions",
    "auth_users",
    "artifact_index",
    "benchmarks",
    "citation_records",
    "dataset_versions",
    "evaluation_cases",
    "evaluation_runs",
    "execution_items",
    "http_attempts",
    "judge_configs",
    "leaderboard_results",
    "leaderboard_snapshots",
    "notifications",
    "questions",
    "resource_leases",
    "run_agents",
    "run_dataset_versions",
    "score_configs",
    "score_records",
    "sessions",
    "worker_leases",
    "workspace_attempts",
    "workspace_comparisons",
    "workspace_conversations",
    "workspace_messages",
}


def alembic_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    return config


def table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_initial_migration_is_complete_and_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = alembic_config(database_path)

    command.upgrade(config, "head")

    assert table_names(database_path) >= BUSINESS_TABLES
    command.check(config)

    command.downgrade(config, "base")

    assert not BUSINESS_TABLES & table_names(database_path)


def test_comparison_migration_preserves_populated_workspace_conversations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "populated-workspace-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "b5e1f8a3c702")
    agent_id = "10000000000000000000000000000001"
    version_id = "10000000000000000000000000000002"
    conversation_id = "10000000000000000000000000000003"
    message_id = "10000000000000000000000000000004"
    attempt_id = "10000000000000000000000000000005"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO agents (name, provider, status, id, created_at, updated_at) "
            "VALUES ('Migration Agent', 'Provider', 'enabled', ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (agent_id,),
        )
        connection.execute(
            "INSERT INTO agent_versions (agent_id, version, endpoint_url, auth_type, "
            "request_template, answer_jsonpath, timeout_seconds, concurrency, lifecycle_status, "
            "preflight_status, enabled, id, created_at, updated_at) VALUES "
            "(?, 1, 'https://agent.invalid', 'none', '{\"prompt\":\"{{prompt}}\"}', "
            "'$.answer', 120, 1, 'active', 'not_run', 1, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (agent_id, version_id),
        )
        connection.execute(
            "INSERT INTO workspace_conversations (owner_user_id, pinned_agent_version_id, "
            "title, last_turn_no, revision, id, created_at, updated_at) VALUES "
            "(?, ?, 'Existing conversation', 1, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (LEGACY_OPERATOR_USER_ID.hex, version_id, conversation_id),
        )
        connection.execute(
            "INSERT INTO workspace_messages (conversation_id, turn_no, role, variant_no, active, "
            "content, id, created_at) VALUES (?, 1, 'user', 1, 1, 'Existing prompt', ?, "
            "CURRENT_TIMESTAMP)",
            (conversation_id, message_id),
        )
        connection.execute(
            "INSERT INTO workspace_attempts (conversation_id, user_message_id, "
            "agent_version_id, generation_no, idempotency_key, request_fingerprint, status, "
            "http_attempt_count, prompt_hash, prompt_characters, prompt_format, id, created_at, "
            "updated_at) VALUES (?, ?, ?, 1, '00000000-0000-4000-8000-000000000001', ?, "
            "'queued', 0, ?, 15, 'workspace-chat-v2', ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (conversation_id, message_id, version_id, "a" * 64, "b" * 64, attempt_id),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT title, comparison_id, comparison_position FROM workspace_conversations "
            "WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    assert row == ("Existing conversation", None, None)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_attempts WHERE id = ?", (attempt_id,)
        ).fetchone() == (1,)

    command.downgrade(config, "b5e1f8a3c702")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workspace_conversations)")
        }
        title = connection.execute(
            "SELECT title FROM workspace_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM workspace_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    assert title == ("Existing conversation",)
    assert attempt_count == (1,)
    assert not {"comparison_id", "comparison_position"} & columns


def test_csrf_migration_invalidates_existing_sessions(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-session.db"
    config = alembic_config(database_path)
    command.upgrade(config, "84f14ec48208")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                role, token_hash, expires_at, last_seen_at, created_at, id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "operator",
                "a" * 64,
                "2026-07-17 00:00:00",
                "2026-07-16 00:00:00",
                "2026-07-16 00:00:00",
                "00000000000000000000000000000001",
            ),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        csrf_column = next(
            row
            for row in connection.execute("PRAGMA table_info(sessions)")
            if row[1] == "csrf_token_hash"
        )
    assert session_count == 0
    assert csrf_column[3] == 1


def test_attempt_usage_migration_is_nullable_constrained_and_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "attempt-usage-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "c9f1a72d6b84")

    command.upgrade(config, "d0a4e83f7c21")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(http_attempts)")
        }
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'http_attempts'"
        ).fetchone()[0]
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        assert columns[name][3] == 0
        assert f"ck_http_attempts_{name}_nonnegative" in table_sql

    command.downgrade(config, "c9f1a72d6b84")

    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(http_attempts)")
        }
    assert not {"input_tokens", "output_tokens", "total_tokens"} & downgraded_columns


def test_multi_worker_migration_rejects_active_runs_before_altering_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "active-run-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "d0a4e83f7c21")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO judge_configs (provider, model, prompt_version, enabled, id, "
            "created_at, updated_at) VALUES ('judge', 'model', 'v1', 1, ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("00000000000000000000000000000001",),
        )
        connection.execute(
            "INSERT INTO score_configs (name, reasoning_weight, retrieval_weight, data_weight, "
            "instruction_following_weight, long_context_weight, retrieval_answer_weight, "
            "retrieval_citation_weight, citation_authenticity_weight, citation_support_weight, "
            "id, created_at, updated_at) VALUES ('Score', 20, 20, 20, 20, 20, 70, 30, 50, "
            "50, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("00000000000000000000000000000002",),
        )
        connection.execute(
            "INSERT INTO evaluation_runs (name, status, desired_state, stage, active_slot, "
            "dataset_bundle_hash, judge_config_id, score_config_id, id, created_at, updated_at) "
            "VALUES ('Active', 'running', 'running', 'inference', 1, ?, ?, ?, ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "a" * 64,
                "00000000000000000000000000000001",
                "00000000000000000000000000000002",
                "00000000000000000000000000000003",
            ),
        )

    with pytest.raises(
        RuntimeError, match="requires all active evaluation runs to be stopped"
    ):
        command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "claim_owner_id" not in columns
    assert revision == ("d0a4e83f7c21",)


def test_resource_lease_migration_has_no_active_run_restriction(tmp_path: Path) -> None:
    database_path = tmp_path / "resource-lease-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "e1f6a2c4b890")

    command.upgrade(config, "head")
    assert "resource_leases" in table_names(database_path)
    command.downgrade(config, "e1f6a2c4b890")
    assert "resource_leases" not in table_names(database_path)


def test_identity_migration_preserves_and_backfills_existing_sessions(tmp_path: Path) -> None:
    database_path = tmp_path / "identity-session-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "f3b7c9d2e4a1")
    session_rows = [
        (
            "read_only",
            "a" * 64,
            "b" * 64,
            "10000000000000000000000000000001",
        ),
        (
            "operator",
            "c" * 64,
            "d" * 64,
            "10000000000000000000000000000002",
        ),
    ]
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO sessions (role, token_hash, csrf_token_hash, expires_at, "
            "last_seen_at, created_at, id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*row[:3], now, now, now, row[3]) for row in session_rows],
        )

    command.upgrade(config, "a4d9c2e7f601")
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            "SELECT role, token_hash, csrf_token_hash, id, user_id FROM sessions ORDER BY role"
        ).fetchall()
        users = connection.execute(
            "SELECT role, id, workspace_enabled FROM auth_users ORDER BY role"
        ).fetchall()
    assert migrated == [
        (
            "operator",
            "c" * 64,
            "d" * 64,
            "10000000000000000000000000000002",
            "f91b3c4a9dbd59ea922f56b64a56f801",
        ),
        (
            "read_only",
            "a" * 64,
            "b" * 64,
            "10000000000000000000000000000001",
            "153bc83b658456eca79b70fe5054e537",
        ),
    ]
    assert users == [
        ("operator", "f91b3c4a9dbd59ea922f56b64a56f801", 1),
        ("read_only", "153bc83b658456eca79b70fe5054e537", 0),
    ]

    command.downgrade(config, "f3b7c9d2e4a1")
    with sqlite3.connect(database_path) as connection:
        downgraded = connection.execute(
            "SELECT role, token_hash, csrf_token_hash, id FROM sessions ORDER BY role"
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert downgraded == [
        ("operator", "c" * 64, "d" * 64, "10000000000000000000000000000002"),
        ("read_only", "a" * 64, "b" * 64, "10000000000000000000000000000001"),
    ]
    assert "user_id" not in columns


def test_run_profile_migration_backfills_runs_and_preserves_snapshot_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "run-profile-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "d6c19d2a9f41")
    run_id = "00000000000000000000000000000003"
    agent_id = "00000000000000000000000000000004"
    agent_version_id = "00000000000000000000000000000005"
    run_agent_id = "00000000000000000000000000000006"
    snapshot_id = "00000000000000000000000000000007"
    bundle_hash = "d" * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO judge_configs (
                provider, model, prompt_version, enabled, id, created_at, updated_at
            ) VALUES ('judge', 'model', 'v1', 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            ("00000000000000000000000000000001",),
        )
        connection.execute(
            """
            INSERT INTO score_configs (
                name, reasoning_weight, retrieval_weight, data_weight,
                retrieval_answer_weight, retrieval_citation_weight,
                citation_authenticity_weight, citation_support_weight,
                id, created_at, updated_at
            ) VALUES ('Default', 40, 35, 25, 70, 30, 50, 50, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            ("00000000000000000000000000000002",),
        )
        connection.execute(
            """
            INSERT INTO agents (
                name, provider, status, id, created_at, updated_at
            ) VALUES ('Legacy Agent', 'Provider', 'enabled', ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (agent_id,),
        )
        connection.execute(
            """
            INSERT INTO agent_versions (
                agent_id, version, endpoint_url, auth_type, request_template,
                answer_jsonpath, timeout_seconds, concurrency, enabled,
                id, created_at, updated_at
            ) VALUES (?, 1, 'https://agent.example', 'none', '{}', '$.answer',
                      120, 1, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (agent_id, agent_version_id),
        )
        connection.execute(
            """
            INSERT INTO evaluation_runs (
                name, status, desired_state, stage, active_slot, dataset_bundle_hash,
                judge_config_id, score_config_id, id, created_at, updated_at
            ) VALUES ('Legacy', 'queued', 'running', 'preparing', NULL, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                bundle_hash,
                "00000000000000000000000000000001",
                "00000000000000000000000000000002",
                run_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO run_agents (
                run_id, agent_version_id, status, total, completed, failed, retried,
                id, created_at, updated_at
            ) VALUES (?, ?, 'completed', 2, 2, 0, 0, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (run_id, agent_version_id, run_agent_id),
        )
        rankings = [
            {
                "run_agent_id": run_agent_id,
                "agent_id": agent_id,
                "agent_version_id": agent_version_id,
                "overall_score": 88.5,
                "ability_scores": {"reasoning": 90, "retrieval": 87},
                "benchmark_scores": [
                    {"ability": "reasoning", "score": 90},
                    {"ability": "retrieval", "score": 87},
                ],
            },
            "malformed ranking",
        ]
        connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                run_id, published_at, rankings_json, is_current, id
            ) VALUES (?, CURRENT_TIMESTAMP, ?, 0, ?)
            """,
            (run_id, json.dumps(rankings), snapshot_id),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            """
            SELECT profile_key, run_scope, target_ability, ranking_eligible, protocol_hash
            FROM evaluation_runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        snapshot_index = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'uq_leaderboard_snapshots_current'
            """
        ).fetchone()
        foreign_keys = {
            (row[2], row[3], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(leaderboard_results)")
        }
        result_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(leaderboard_results)")
        }
        backfilled_results = connection.execute(
            """
            SELECT metric, score, protocol_hash, benchmark_count
            FROM leaderboard_results ORDER BY metric
            """
        ).fetchall()
        version_lifecycle = connection.execute(
            "SELECT lifecycle_status, preflight_status FROM agent_versions WHERE id = ?",
            (agent_version_id,),
        ).fetchone()

    assert migrated == ("comprehensive", "comprehensive", None, 1, bundle_hash)
    assert snapshot_index is not None and "WHERE is_current = 1" in snapshot_index[0]
    assert foreign_keys == {
        ("leaderboard_snapshots", "snapshot_id", "CASCADE"),
        ("run_agents", "run_agent_id", "RESTRICT"),
        ("agents", "agent_id", "RESTRICT"),
        ("agent_versions", "agent_version_id", "RESTRICT"),
    }
    assert "ix_leaderboard_results_metric_protocol_hash" in result_indexes
    assert "ix_leaderboard_results_agent_id" in result_indexes
    assert backfilled_results == [
        ("overall", 88.5, bundle_hash, 2),
        ("reasoning", 90, bundle_hash, 1),
        ("retrieval", 87, bundle_hash, 1),
    ]
    assert version_lifecycle == ("active", "not_run")


def test_legacy_profile_normalization_excludes_smoke_from_current_board(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-profile-normalization.db"
    config = alembic_config(database_path)
    command.upgrade(config, "b91f3a72c5e8")
    judge_id = "00000000000000000000000000000001"
    score_id = "00000000000000000000000000000002"
    comprehensive_run_id = "00000000000000000000000000000003"
    smoke_run_id = "00000000000000000000000000000004"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO judge_configs (
                provider, model, prompt_version, enabled, id, created_at, updated_at
            ) VALUES ('judge', 'model', 'v1', 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (judge_id,),
        )
        connection.execute(
            """
            INSERT INTO score_configs (
                name, reasoning_weight, retrieval_weight, data_weight,
                retrieval_answer_weight, retrieval_citation_weight,
                citation_authenticity_weight, citation_support_weight,
                id, created_at, updated_at
            ) VALUES ('Default', 40, 35, 25, 70, 30, 50, 50, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (score_id,),
        )
        benchmark_rows = [
            ("10000000000000000000000000000001", "C-Eval", "reasoning", 100),
            ("10000000000000000000000000000002", "MMLU-Pro", "reasoning", 10),
            ("10000000000000000000000000000003", "FinQA", "data_reasoning", 10),
        ]
        for index, (benchmark_id, name, ability, question_count) in enumerate(benchmark_rows):
            dataset_id = f"2000000000000000000000000000000{index + 1}"
            connection.execute(
                """
                INSERT INTO benchmarks (
                    name, ability, language, source, id, created_at, updated_at
                ) VALUES (?, ?, 'en', 'source', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (name, ability, benchmark_id),
            )
            connection.execute(
                """
                INSERT INTO dataset_versions (
                    benchmark_id, version, source_version, content_hash,
                    question_count, status, id, created_at, updated_at
                ) VALUES (?, 1, 'v1', ?, ?, 'frozen', ?,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (benchmark_id, str(index + 1) * 64, question_count, dataset_id),
            )
        for run_id, name in (
            (comprehensive_run_id, "Legacy Comprehensive"),
            (smoke_run_id, "Legacy Smoke"),
        ):
            connection.execute(
                "INSERT INTO evaluation_runs (name, status, desired_state, stage, active_slot, "
                "dataset_bundle_hash, profile_key, run_scope, ranking_eligible, protocol_hash, "
                "judge_config_id, score_config_id, id, created_at, updated_at) "
                "VALUES (?, 'published', 'running', 'ready_to_publish', NULL, ?, 'legacy', "
                "'comprehensive', 1, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (name, "d" * 64, "d" * 64, judge_id, score_id, run_id),
            )
        connection.execute(
            "INSERT INTO run_dataset_versions (run_id, dataset_version_id) VALUES (?, ?)",
            (comprehensive_run_id, "20000000000000000000000000000001"),
        )
        connection.executemany(
            "INSERT INTO run_dataset_versions (run_id, dataset_version_id) VALUES (?, ?)",
            [
                (smoke_run_id, "20000000000000000000000000000002"),
                (smoke_run_id, "20000000000000000000000000000003"),
            ],
        )
        connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                run_id, published_at, rankings_json, is_current, id
            ) VALUES (?, '2026-07-15 10:00:00', '[]', 0, ?)
            """,
            (comprehensive_run_id, "30000000000000000000000000000001"),
        )
        connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                run_id, published_at, rankings_json, is_current, id
            ) VALUES (?, '2026-07-15 11:00:00', '[]', 1, ?)
            """,
            (smoke_run_id, "30000000000000000000000000000002"),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        runs = connection.execute(
            "SELECT name, profile_key, run_scope, ranking_eligible "
            "FROM evaluation_runs ORDER BY name"
        ).fetchall()
        current_run = connection.execute(
            "SELECT r.name FROM leaderboard_snapshots s JOIN evaluation_runs r ON r.id = s.run_id "
            "WHERE s.is_current = 1"
        ).fetchone()
    assert runs == [
        ("Legacy Comprehensive", "comprehensive", "comprehensive", 1),
        ("Legacy Smoke", "smoke", "smoke", 0),
    ]
    assert current_run == ("Legacy Comprehensive",)


def test_database_constraints_reject_invalid_configuration(tmp_path: Path) -> None:
    database_path = tmp_path / "constraints.db"
    command.upgrade(alembic_config(database_path), "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        values = (
            "00000000000000000000000000000001",
            "invalid",
            50,
            35,
            25,
            70,
            30,
            50,
            50,
            "2026-01-01 00:00:00",
            "2026-01-01 00:00:00",
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO score_configs (
                    id, name, reasoning_weight, retrieval_weight, data_weight,
                    retrieval_answer_weight, retrieval_citation_weight,
                    citation_authenticity_weight, citation_support_weight,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )


def test_balanced_v2_migration_backfills_cohorts_and_extends_weight_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "balanced-v2-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "b8e5c02f4a71")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO benchmarks (name, ability, language, source, id, created_at, updated_at) "
            "VALUES ('Legacy', 'reasoning', 'en', 'source', ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            ("10000000000000000000000000000001",),
        )
        connection.execute(
            "INSERT INTO dataset_versions (benchmark_id, version, source_version, content_hash, "
            "question_count, status, id, created_at, updated_at) VALUES (?, 1, 'v1', ?, 1, "
            "'frozen', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "10000000000000000000000000000001",
                "a" * 64,
                "20000000000000000000000000000001",
            ),
        )
        connection.execute(
            "INSERT INTO score_configs (name, reasoning_weight, retrieval_weight, data_weight, "
            "retrieval_answer_weight, retrieval_citation_weight, citation_authenticity_weight, "
            "citation_support_weight, id, created_at, updated_at) VALUES "
            "('Legacy', 40, 35, 25, 70, 30, 50, 50, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("30000000000000000000000000000001",),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        cohort = connection.execute(
            "SELECT cohort_key FROM dataset_versions"
        ).fetchone()
        weights = connection.execute(
            "SELECT reasoning_weight, retrieval_weight, data_weight, "
            "instruction_following_weight, long_context_weight FROM score_configs"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO score_configs (name, reasoning_weight, retrieval_weight, data_weight, "
                "instruction_following_weight, long_context_weight, retrieval_answer_weight, "
                "retrieval_citation_weight, citation_authenticity_weight, citation_support_weight, "
                "id, created_at, updated_at) VALUES "
                "('Invalid', 20, 20, 20, 20, 19, 70, 30, 50, 50, ?, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)",
                ("30000000000000000000000000000002",),
            )

    assert cohort == ("legacy-v1",)
    assert weights == (40, 35, 25, 0, 0)


def test_result_validity_migration_is_pending_constrained_and_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "result-validity-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "c9f1a72d6b84")
    judge_id = "00000000000000000000000000000001"
    score_id = "00000000000000000000000000000002"
    agent_id = "00000000000000000000000000000003"
    version_id = "00000000000000000000000000000004"
    run_id = "00000000000000000000000000000005"
    run_agent_id = "00000000000000000000000000000006"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO judge_configs (provider, model, prompt_version, enabled, id, created_at, "
            "updated_at) VALUES ('judge', 'model', 'v1', 1, ?, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (judge_id,),
        )
        connection.execute(
            "INSERT INTO score_configs (name, reasoning_weight, retrieval_weight, data_weight, "
            "instruction_following_weight, long_context_weight, retrieval_answer_weight, "
            "retrieval_citation_weight, citation_authenticity_weight, citation_support_weight, "
            "id, created_at, updated_at) VALUES ('Score', 20, 20, 20, 20, 20, 70, 30, 50, 50, "
            "?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (score_id,),
        )
        connection.execute(
            "INSERT INTO agents (name, provider, status, id, created_at, updated_at) VALUES "
            "('Agent', 'Provider', 'enabled', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (agent_id,),
        )
        connection.execute(
            "INSERT INTO agent_versions (agent_id, version, endpoint_url, auth_type, "
            "request_template, answer_jsonpath, timeout_seconds, concurrency, enabled, id, "
            "created_at, updated_at) VALUES (?, 1, 'https://agent.example', 'none', '{}', "
            "'$.answer', 120, 1, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (agent_id, version_id),
        )
        connection.execute(
            "INSERT INTO evaluation_runs (name, status, desired_state, stage, dataset_bundle_hash, "
            "profile_key, run_scope, ranking_eligible, protocol_hash, judge_config_id, "
            "score_config_id, id, created_at, updated_at) VALUES ('Historical', 'published', "
            "'running', 'ready_to_publish', ?, 'unified-v3', 'comprehensive', 1, ?, ?, ?, ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("a" * 64, "b" * 64, judge_id, score_id, run_id),
        )
        connection.execute(
            "INSERT INTO run_agents (run_id, agent_version_id, status, total, completed, failed, "
            "retried, id, created_at, updated_at) VALUES (?, ?, 'completed', 1, 1, 0, 0, ?, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (run_id, version_id, run_agent_id),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            "SELECT validity_status, validity_reason, configuration_failure_count, "
            "configuration_failure_rate FROM run_agents WHERE id = ?",
            (run_agent_id,),
        ).fetchone()
        assert migrated == ("pending", None, 0, 0)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run_agents SET validity_status = 'unknown' WHERE id = ?", (run_agent_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run_agents SET configuration_failure_count = -1 WHERE id = ?",
                (run_agent_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run_agents SET configuration_failure_rate = 100.1 WHERE id = ?",
                (run_agent_id,),
            )

    command.downgrade(config, "c9f1a72d6b84")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(run_agents)")}
        assert connection.execute(
            "SELECT COUNT(*) FROM run_agents WHERE id = ?", (run_agent_id,)
        ).fetchone() == (1,)
    assert not {
        "validity_status",
        "validity_reason",
        "configuration_failure_count",
        "configuration_failure_rate",
    } & columns


async def test_orm_defaults_and_relationships(tmp_path: Path) -> None:
    database_path = tmp_path / "orm.db"
    await asyncio.to_thread(command.upgrade, alembic_config(database_path), "head")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database_path}", data_dir=tmp_path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            agent = Agent(name="Demo Agent", provider="Demo", status=AgentStatus.ENABLED)
            session.add(agent)
            await session.flush()
            session.add(
                AgentVersion(
                    agent_id=agent.id,
                    version=1,
                    endpoint_url="https://agent.example.test/run",
                    auth_type=AuthType.NONE,
                    request_template='{"query":"{{prompt}}"}',
                    answer_jsonpath="$.answer",
                )
            )
            session.add(ScoreConfig(name="MVP Default"))

        async with session_factory() as session:
            stored_agent = await session.scalar(select(Agent).where(Agent.name == "Demo Agent"))
            stored_score_config = await session.scalar(
                select(ScoreConfig).where(ScoreConfig.name == "MVP Default")
            )

        assert stored_agent is not None
        assert stored_score_config is not None
        assert stored_score_config.reasoning_weight == 40
        assert stored_score_config.retrieval_weight == 35
        assert stored_score_config.data_weight == 25
    finally:
        await engine.dispose()
