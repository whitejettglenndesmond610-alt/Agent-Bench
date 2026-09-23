import argparse
import asyncio
import getpass
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_eval.auth.encryption import generate_encryption_key
from agent_eval.auth.identity import normalize_username
from agent_eval.auth.security import hash_password
from agent_eval.core.artifact_store import ArtifactStore, create_artifact_store
from agent_eval.core.logging import configure_logging
from agent_eval.core.settings import Settings
from agent_eval.core.storage import ensure_storage
from agent_eval.datasets.audit import AuditPolicy, audit_question_records, render_audit_report
from agent_eval.datasets.balanced_builder import build_balanced_v2_dataset
from agent_eval.datasets.demo_builder import build_demo_dataset
from agent_eval.datasets.hle_builder import HLE_BENCHMARK, HLE_QUOTA, build_hle_dataset
from agent_eval.datasets.importer import (
    BALANCED_V2_COHORT_KEY,
    OFFICIAL_V4_COHORT_KEY,
    import_question_records,
)
from agent_eval.datasets.livebench_builder import (
    HELDOUT_V1_COHORT_KEY,
    LIVEBENCH_CATEGORIES,
    LIVEBENCH_QUOTAS,
    build_livebench_dataset,
)
from agent_eval.datasets.official_builder import build_official_v4_dataset
from agent_eval.datasets.schema import load_question_records, validate_demo_distribution
from agent_eval.datasets.tau2_importer import (
    DEFAULT_IMPORT_LIMIT,
    SUPPORTED_DOMAINS,
    import_tau2,
)
from agent_eval.db.engine import create_engine, verify_database
from agent_eval.db.enums import SessionRole
from agent_eval.db.session import create_session_factory, session_scope
from agent_eval.maintenance.artifact_archive import (
    ArtifactArchiveError,
    export_artifact_archive,
    restore_artifact_archive,
    verify_artifact_archive,
)
from agent_eval.maintenance.artifact_sync import (
    ArtifactSyncError,
    sync_local_artifacts_to_s3,
)
from agent_eval.maintenance.bootstrap import bootstrap_demo, demo_preflight
from agent_eval.maintenance.database_migration import (
    DatabaseMigrationError,
    migrate_database,
)
from agent_eval.maintenance.demo import (
    build_run_report,
    queue_agent_pair_run,
    queue_fake_run,
    queue_real_run,
    seed_fake_agents,
    wait_for_run,
)
from agent_eval.maintenance.operational_check import (
    OperationalCheckError,
    operational_check,
)
from agent_eval.maintenance.operations import (
    archive_current_leaderboard,
    cleanup_expired_artifacts,
    create_backup,
    restore_latest_backup,
    revalidate_ranking_results,
    verify_latest_backup,
)
from agent_eval.maintenance.secrets import (
    initialize_platform_secrets,
    store_runtime_secret,
    write_demo_environment,
    write_local_real_environment,
)
from agent_eval.maintenance.workspace_users import (
    WorkspaceUserError,
    create_workspace_user,
    generate_workspace_password,
    reset_workspace_user_password,
    write_credential,
)


async def check(
    settings: Settings | None = None, artifact_store: ArtifactStore | None = None
) -> None:
    settings = settings or Settings()
    artifact_store = artifact_store or create_artifact_store(settings)
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    try:
        await verify_database(engine)
        await asyncio.to_thread(artifact_store.verify_writable)
    finally:
        await engine.dispose()


def prompted_password() -> str:
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not password:
        raise WorkspaceUserError("password must not be empty")
    if password != confirmation:
        raise WorkspaceUserError("passwords do not match")
    return password


async def maintain_workspace_user(
    *,
    username: str,
    display_name: str | None,
    role: SessionRole | None,
    output: Path | None,
) -> None:
    username = normalize_username(username)
    password = generate_workspace_password() if output is not None else prompted_password()
    credential_written = False
    if output is not None:
        write_credential(output, username=username, password=password)
        credential_written = True
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            if display_name is not None and role is not None:
                user = await create_workspace_user(
                    session,
                    username=username,
                    display_name=display_name,
                    role=role,
                    password=password,
                )
            else:
                user = await reset_workspace_user_password(
                    session, username=username, password=password
                )
    except BaseException:
        if credential_written and output is not None:
            output.unlink(missing_ok=True)
        raise
    finally:
        await engine.dispose()
    if output is not None:
        print(f"credential written securely: {output}")
    print(f"workspace user ready: {user.username}")


async def import_dataset(path: Path, require_demo_distribution: bool) -> None:
    records = load_question_records(path)
    if require_demo_distribution:
        validate_demo_distribution(records)

    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            imported = await import_question_records(session, records)
    finally:
        await engine.dispose()

    for item in imported:
        action = "created" if item.created else "reused"
        print(
            f"{action} {item.benchmark} v{item.version}: "
            f"{item.question_count} questions, sha256={item.content_hash}"
        )
    print(f"validated and imported {len(records)} questions across {len(imported)} benchmarks")


async def import_balanced_v2(path: Path) -> None:
    records = load_question_records(path)
    report = audit_question_records(records, policy="balanced-v2")
    if not report["policy_passed"]:
        print(render_audit_report(report))
        raise SystemExit(1)

    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session,
                records,
                cohort_key=BALANCED_V2_COHORT_KEY,
                allow_candidate=True,
            )
    finally:
        await engine.dispose()
    print(
        f"audited and imported {len(records)} balanced-v2 questions across "
        f"{len(imported)} benchmarks"
    )


async def import_official_v4(path: Path) -> None:
    records = load_question_records(path)
    counts = {record.benchmark: 0 for record in records}
    for record in records:
        counts[record.benchmark] += 1
    if counts != {"GPQA-Diamond": 20, "CMMLU": 20, "GSM8K": 20}:
        raise SystemExit(f"official-v4 import requires 20/20/20 GPQA/CMMLU/GSM8K, got {counts}")

    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session, records, cohort_key=OFFICIAL_V4_COHORT_KEY
            )
    finally:
        await engine.dispose()
    for item in imported:
        action = "created" if item.created else "reused"
        print(
            f"{action} {item.benchmark} v{item.version}: "
            f"{item.question_count} questions, sha256={item.content_hash}"
        )
    print(f"imported {len(records)} official-v4 questions across {len(imported)} benchmarks")


async def import_livebench(path: Path) -> None:
    records = load_question_records(path)
    counts = {record.benchmark: 0 for record in records}
    for record in records:
        counts[record.benchmark] += 1
    if counts != LIVEBENCH_QUOTAS:
        raise SystemExit(f"livebench import requires quotas {LIVEBENCH_QUOTAS}, got {counts}")

    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
    finally:
        await engine.dispose()
    for item in imported:
        action = "created" if item.created else "reused"
        print(
            f"{action} {item.benchmark} v{item.version}: "
            f"{item.question_count} questions, sha256={item.content_hash}"
        )
    print(
        f"imported {len(records)} held-out livebench questions across "
        f"{len(imported)} benchmarks"
    )


async def import_hle(path: Path) -> None:
    records = load_question_records(path)
    counts = {record.benchmark: 0 for record in records}
    for record in records:
        counts[record.benchmark] += 1
    if counts != {HLE_BENCHMARK: HLE_QUOTA}:
        raise SystemExit(f"hle import requires {HLE_QUOTA} {HLE_BENCHMARK} questions, got {counts}")

    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            imported = await import_question_records(
                session, records, cohort_key=HELDOUT_V1_COHORT_KEY, held_out=True
            )
    finally:
        await engine.dispose()
    for item in imported:
        action = "created" if item.created else "reused"
        print(
            f"{action} {item.benchmark} v{item.version}: "
            f"{item.question_count} questions, sha256={item.content_hash}"
        )
    print(f"imported {len(records)} held-out hle questions across {len(imported)} benchmarks")


async def import_tau2_command(
    domain: str, data_dir: Path | None, limit: int | None
) -> None:
    settings = Settings()
    resolved_dir = data_dir or settings.tau2_data_dir
    if resolved_dir is None:
        raise SystemExit("tau2 data directory required: pass --data-dir or set TAU2_DATA_DIR")
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_scope(session_factory) as session:
            imported = await import_tau2(
                session, domain=domain, data_dir=resolved_dir, limit=limit
            )
    finally:
        await engine.dispose()
    for item in imported:
        action = "created" if item.created else "reused"
        print(
            f"{action} {item.benchmark} v{item.version}: "
            f"{item.question_count} questions, sha256={item.content_hash}"
        )


def audit_dataset(path: Path, policy: str | None, enforce: bool) -> None:
    records = load_question_records(path)
    selected_policy: AuditPolicy | None = "balanced-v2" if policy == "balanced-v2" else None
    report = audit_question_records(records, policy=selected_policy)
    print(render_audit_report(report))
    if enforce and report["violations"]:
        raise SystemExit(1)


async def bootstrap(dataset_path: Path) -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            result = await bootstrap_demo(session, dataset_path)
    finally:
        await engine.dispose()
    print(
        f"bootstrap ready: {result.question_count} questions, {result.dataset_count} datasets, "
        f"judge={'created' if result.judge_created else 'reused'}, "
        f"score={'created' if result.score_created else 'reused'}"
    )


async def preflight(
    require_snapshot: bool,
    settings: Settings | None = None,
    artifact_store: ArtifactStore | None = None,
) -> None:
    settings = settings or Settings()
    artifact_store = artifact_store or create_artifact_store(settings)
    configure_logging(settings.log_level)
    ensure_storage(settings)
    failures: list[str] = []
    try:
        settings.require_worker_runtime()
    except ValueError as error:
        failures.append(str(error))
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        await asyncio.to_thread(artifact_store.verify_writable)
        async with session_factory() as session:
            failures.extend(
                await demo_preflight(session, settings, require_snapshot=require_snapshot)
            )
    finally:
        await engine.dispose()
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        raise SystemExit(1)
    print("demo preflight passed")


async def cleanup_artifacts(
    settings: Settings | None = None, artifact_store: ArtifactStore | None = None
) -> None:
    settings = settings or Settings()
    artifact_store = artifact_store or create_artifact_store(settings)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with (
            artifact_maintenance_lock(engine),
            session_scope(session_factory) as session,
        ):
            rows, files = await cleanup_expired_artifacts(session, artifact_store)
    finally:
        await engine.dispose()
    print(f"removed {rows} expired artifact records and {files} files")


@asynccontextmanager
async def artifact_maintenance_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    """Coordinate PostgreSQL cleanup and archive export on one session advisory lock."""
    if engine.dialect.name != "postgresql":
        yield
        return
    connection = await engine.connect()
    lock_key = "agent-eval:artifact-cleanup-export"
    acquired = False
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        if not acquired:
            raise ArtifactArchiveError("artifact cleanup or archive export is already active")
        yield
    finally:
        if acquired:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
        await connection.close()


async def export_configured_artifact_archive(output: Path) -> None:
    settings = Settings()
    artifact_store = create_artifact_store(settings)
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with artifact_maintenance_lock(engine), session_factory() as session:
            report = await export_artifact_archive(session, artifact_store, output)
    finally:
        await engine.dispose()
    print(
        f"artifacts={report.count} bytes={report.size_bytes} "
        f"manifest_sha256={report.manifest_sha256}"
    )


def verify_configured_artifact_archive(path: Path) -> None:
    report = verify_artifact_archive(path)
    print(
        f"artifacts={report.count} bytes={report.size_bytes} "
        f"manifest_sha256={report.manifest_sha256}"
    )


def restore_configured_artifact_archive(path: Path) -> None:
    settings = Settings()
    ensure_storage(settings)
    report = restore_artifact_archive(path, create_artifact_store(settings))
    print(f"copied={report.copied} existing={report.existing}")


async def archive_leaderboard() -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            archived = await archive_current_leaderboard(session)
    finally:
        await engine.dispose()
    print(f"archived {archived} current leaderboard snapshot(s)")


async def revalidate_results() -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            report = await revalidate_ranking_results(session)
    finally:
        await engine.dispose()
    print(
        f"revalidated {report.run_agent_count} result(s) across {report.run_count} run(s): "
        f"valid={report.valid_count}, invalid={report.invalid_count}, "
        f"updated={report.updated_count}, deleted leaderboard rows={report.deleted_result_count}, "
        f"repaired current snapshots={report.current_repaired_count}"
    )


async def seed_demo_agents() -> None:
    settings = Settings()
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            version_ids = await seed_fake_agents(session)
    finally:
        await engine.dispose()
    print(f"fake Agent versions ready: {', '.join(str(value) for value in version_ids)}")


async def queue_demo_run(question_count: int) -> None:
    settings = Settings()
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            run_id = await queue_fake_run(session, question_count=question_count)
    finally:
        await engine.dispose()
    print(f"fake evaluation queued: {run_id}")


async def queue_real_evaluation(question_count: int) -> None:
    settings = Settings()
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            run_id = await queue_real_run(session, question_count=question_count)
    finally:
        await engine.dispose()
    print(f"real evaluation queued: {run_id}")


async def queue_pair_evaluation(agent_names: list[str], question_count: int) -> None:
    if len(agent_names) != 2:
        raise SystemExit("queue-pair-run requires exactly two --agent values")
    settings = Settings()
    ensure_storage(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await verify_database(engine)
        async with session_scope(session_factory) as session:
            run_id = await queue_agent_pair_run(
                session,
                agent_names=(agent_names[0], agent_names[1]),
                question_count=question_count,
            )
    finally:
        await engine.dispose()
    print(f"pair evaluation queued: {run_id}")


async def wait_for_demo_run(run_id: UUID, timeout_seconds: int) -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        status = await wait_for_run(
            session_factory,
            run_id,
            timeout_seconds=timeout_seconds,
        )
    finally:
        await engine.dispose()
    print(f"evaluation run finished: {run_id}, status={status.value}")
    if status.value != "published":
        raise SystemExit(1)


async def print_run_report(run_id: UUID) -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            report = await build_run_report(session, run_id)
    finally:
        await engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def run_fake_suite(timeout_seconds: int) -> None:
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        for question_count in (20, 100):
            async with session_scope(session_factory) as session:
                run_id = await queue_fake_run(session, question_count=question_count)
            status = await wait_for_run(
                session_factory,
                run_id,
                timeout_seconds=timeout_seconds,
            )
            async with session_factory() as session:
                report = await build_run_report(session, run_id)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if status.value != "published":
                raise SystemExit(1)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent evaluation maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("operational-check")
    subparsers.add_parser("hash-password")
    subparsers.add_parser("generate-encryption-key")
    create_user_parser = subparsers.add_parser("create-workspace-user")
    create_user_parser.add_argument("username")
    create_user_parser.add_argument("display_name")
    create_user_parser.add_argument("role", choices=[role.value for role in SessionRole])
    create_user_parser.add_argument("--credential-output", type=Path)
    reset_user_parser = subparsers.add_parser("reset-workspace-user-password")
    reset_user_parser.add_argument("username")
    reset_user_parser.add_argument("--credential-output", type=Path)
    import_parser = subparsers.add_parser("import-dataset")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--require-demo-distribution", action="store_true")
    balanced_import_parser = subparsers.add_parser("import-balanced-v2")
    balanced_import_parser.add_argument("path", type=Path)
    official_import_parser = subparsers.add_parser("import-official-v4")
    official_import_parser.add_argument("path", type=Path)
    audit_parser = subparsers.add_parser("audit-dataset")
    audit_parser.add_argument("path", type=Path)
    audit_parser.add_argument("--policy", choices=["balanced-v2"])
    audit_parser.add_argument("--enforce", action="store_true")
    build_parser = subparsers.add_parser("build-demo-dataset")
    build_parser.add_argument("output", type=Path, nargs="?", default=Path("data/demo-dataset"))
    balanced_parser = subparsers.add_parser("build-balanced-v2")
    balanced_parser.add_argument("output", type=Path, nargs="?")
    balanced_parser.add_argument("--anchors", type=Path)
    official_parser = subparsers.add_parser("build-official-v4")
    official_parser.add_argument("output", type=Path, nargs="?", default=Path("data/official-v4"))
    livebench_parser = subparsers.add_parser("build-livebench")
    livebench_parser.add_argument(
        "output", type=Path, nargs="?", default=Path("data/livebench-heldout")
    )
    livebench_parser.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="CATEGORY=YYYY-MM-DD",
        help="override the LiveBench release date per category (math/reasoning/data_analysis)",
    )
    livebench_import_parser = subparsers.add_parser("import-livebench")
    livebench_import_parser.add_argument("path", type=Path)
    hle_parser = subparsers.add_parser("build-hle")
    hle_parser.add_argument("output", type=Path, nargs="?", default=Path("data/hle-heldout"))
    hle_import_parser = subparsers.add_parser("import-hle")
    hle_import_parser.add_argument("path", type=Path)
    tau2_parser = subparsers.add_parser("import-tau2")
    tau2_parser.add_argument(
        "--domain",
        choices=SUPPORTED_DOMAINS,
        required=True,
        help="tau2-bench domain to import",
    )
    tau2_parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="tau2-bench data directory (defaults to TAU2_DATA_DIR)",
    )
    tau2_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_IMPORT_LIMIT,
        help="number of tasks to import (default: the full base split)",
    )
    # Legacy alias from the retail-only phase; equivalent to --domain retail.
    tau2_import_parser = subparsers.add_parser("import-tau2-retail")
    tau2_import_parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="tau2-bench data directory (defaults to TAU2_DATA_DIR)",
    )
    tau2_import_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_IMPORT_LIMIT,
        help="number of retail tasks to import (default: the full base split)",
    )
    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--dataset", type=Path, default=Path("/datasets/demo"))
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--require-snapshot", action="store_true")
    subparsers.add_parser("backup")
    subparsers.add_parser("verify-backup")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--confirm-stopped", action="store_true")
    subparsers.add_parser("cleanup-artifacts")
    subparsers.add_parser("archive-current-leaderboard")
    subparsers.add_parser("revalidate-ranking-results")
    secret_parser = subparsers.add_parser("init-secrets")
    secret_parser.add_argument("directory", type=Path)
    subparsers.add_parser("seed-fake-agents")
    queue_parser = subparsers.add_parser("queue-fake-run")
    queue_parser.add_argument("--questions", type=int, choices=[20, 100], required=True)
    real_queue_parser = subparsers.add_parser("queue-real-run")
    real_queue_parser.add_argument("--questions", type=int, choices=[20, 100], required=True)
    pair_queue_parser = subparsers.add_parser("queue-pair-run")
    pair_queue_parser.add_argument("--agent", action="append", required=True)
    pair_queue_parser.add_argument("--questions", type=int, choices=[20, 100], required=True)
    wait_parser = subparsers.add_parser("wait-run")
    wait_parser.add_argument("run_id", type=UUID)
    wait_parser.add_argument("--timeout", type=int, default=300)
    demo_env_parser = subparsers.add_parser("init-demo-env")
    demo_env_parser.add_argument("secret_directory", type=Path)
    demo_env_parser.add_argument("output", type=Path)
    report_parser = subparsers.add_parser("run-report")
    report_parser.add_argument("run_id", type=UUID)
    suite_parser = subparsers.add_parser("run-fake-suite")
    suite_parser.add_argument("--timeout", type=int, default=300)
    set_secret_parser = subparsers.add_parser("set-deepseek-key")
    set_secret_parser.add_argument("directory", type=Path)
    local_env_parser = subparsers.add_parser("init-local-real-env")
    local_env_parser.add_argument("secret_directory", type=Path)
    local_env_parser.add_argument("output", type=Path)
    database_migration_parser = subparsers.add_parser("migrate-database")
    database_migration_parser.add_argument("--source-url", required=True)
    database_migration_parser.add_argument("--target-url", required=True)
    database_migration_parser.add_argument("--batch-size", type=int, default=1000)
    database_migration_parser.add_argument("--dry-run", action="store_true")
    artifact_sync_parser = subparsers.add_parser("sync-artifacts-to-s3")
    artifact_sync_parser.add_argument("--source-local-dir", type=Path, required=True)
    artifact_sync_parser.add_argument("--target-bucket", required=True)
    artifact_sync_parser.add_argument("--target-prefix", default="")
    artifact_sync_parser.add_argument("--target-endpoint-url")
    artifact_sync_parser.add_argument("--target-region")
    artifact_sync_parser.add_argument("--target-force-path-style", action="store_true")
    artifact_sync_parser.add_argument("--concurrency", type=int, default=8)
    artifact_sync_parser.add_argument("--dry-run", action="store_true")
    export_archive_parser = subparsers.add_parser("export-artifact-archive")
    export_archive_parser.add_argument("output", type=Path)
    verify_archive_parser = subparsers.add_parser("verify-artifact-archive")
    verify_archive_parser.add_argument("path", type=Path)
    restore_archive_parser = subparsers.add_parser("restore-artifact-archive")
    restore_archive_parser.add_argument("path", type=Path)
    arguments = parser.parse_args()

    if arguments.command == "check":
        asyncio.run(check())
    elif arguments.command == "operational-check":
        try:
            asyncio.run(operational_check())
        except OperationalCheckError as error:
            parser.exit(1, f"FAILED: {error}\n")
    elif arguments.command == "hash-password":
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("passwords do not match")
        print(hash_password(password))
    elif arguments.command == "generate-encryption-key":
        print(generate_encryption_key())
    elif arguments.command == "create-workspace-user":
        try:
            asyncio.run(
                maintain_workspace_user(
                    username=arguments.username,
                    display_name=arguments.display_name,
                    role=SessionRole(arguments.role),
                    output=arguments.credential_output,
                )
            )
        except (ValueError, WorkspaceUserError) as error:
            parser.exit(1, f"workspace user creation failed: {error}\n")
    elif arguments.command == "reset-workspace-user-password":
        try:
            asyncio.run(
                maintain_workspace_user(
                    username=arguments.username,
                    display_name=None,
                    role=None,
                    output=arguments.credential_output,
                )
            )
        except (ValueError, WorkspaceUserError) as error:
            parser.exit(1, f"workspace password reset failed: {error}\n")
    elif arguments.command == "import-dataset":
        asyncio.run(import_dataset(arguments.path, arguments.require_demo_distribution))
    elif arguments.command == "import-balanced-v2":
        asyncio.run(import_balanced_v2(arguments.path))
    elif arguments.command == "import-official-v4":
        asyncio.run(import_official_v4(arguments.path))
    elif arguments.command == "audit-dataset":
        audit_dataset(arguments.path, arguments.policy, arguments.enforce)
    elif arguments.command == "build-demo-dataset":
        records = build_demo_dataset(arguments.output)
        print(f"built {len(records)} questions in {arguments.output}")
    elif arguments.command == "build-official-v4":
        official_records = build_official_v4_dataset(arguments.output)
        print(f"built {len(official_records)} official-v4 questions in {arguments.output}")
    elif arguments.command == "build-livebench":
        releases: dict[str, str] = {}
        for override in arguments.release:
            category, separator, release = override.partition("=")
            if not separator or category not in LIVEBENCH_CATEGORIES:
                parser.error(
                    "--release must look like math=2024-08-31 with a LiveBench category "
                    "(math, reasoning, data_analysis)"
                )
            releases[category] = release
        livebench_records = build_livebench_dataset(arguments.output, releases=releases)
        print(f"built {len(livebench_records)} livebench questions in {arguments.output}")
    elif arguments.command == "import-livebench":
        asyncio.run(import_livebench(arguments.path))
    elif arguments.command == "build-hle":
        hle_records = build_hle_dataset(arguments.output)
        print(f"built {len(hle_records)} hle questions in {arguments.output}")
    elif arguments.command == "import-hle":
        asyncio.run(import_hle(arguments.path))
    elif arguments.command == "import-tau2":
        asyncio.run(
            import_tau2_command(
                arguments.domain, data_dir=arguments.data_dir, limit=arguments.limit
            )
        )
    elif arguments.command == "import-tau2-retail":
        asyncio.run(
            import_tau2_command("retail", data_dir=arguments.data_dir, limit=arguments.limit)
        )
    elif arguments.command == "build-balanced-v2":
        local_anchors = Path("data/demo-dataset")
        anchor_path = arguments.anchors or (
            local_anchors if local_anchors.exists() else Path("/datasets/demo")
        )
        output_path = arguments.output or (
            Path("data/balanced-v2-candidate")
            if local_anchors.exists()
            else Path("/data/balanced-v2-candidate")
        )
        balanced_manifest = build_balanced_v2_dataset(anchor_path, output_path)
        print(
            f"built {balanced_manifest['question_count']} balanced-v2 candidate questions in "
            f"{output_path}, sha256={balanced_manifest['canonical_bundle_hash']}"
        )
    elif arguments.command == "bootstrap":
        asyncio.run(bootstrap(arguments.dataset))
    elif arguments.command == "preflight":
        asyncio.run(preflight(arguments.require_snapshot))
    elif arguments.command == "backup":
        manifest = create_backup(Settings())
        print(
            f"backup created: {manifest.filename}, sha256={manifest.sha256}, "
            f"size={manifest.size_bytes}"
        )
    elif arguments.command == "verify-backup":
        manifest = verify_latest_backup(Settings())
        print(f"backup verified: {manifest.filename}, sha256={manifest.sha256}")
    elif arguments.command == "restore":
        manifest = restore_latest_backup(Settings(), confirm_stopped=arguments.confirm_stopped)
        print(f"backup restored: {manifest.filename}")
    elif arguments.command == "cleanup-artifacts":
        asyncio.run(cleanup_artifacts())
    elif arguments.command == "archive-current-leaderboard":
        asyncio.run(archive_leaderboard())
    elif arguments.command == "revalidate-ranking-results":
        asyncio.run(revalidate_results())
    elif arguments.command == "init-secrets":
        initialized = initialize_platform_secrets(arguments.directory)
        print(f"Secret files initialized in {initialized.directory}")
        print(f"Login credential handoff: {initialized.credentials_file}")
    elif arguments.command == "seed-fake-agents":
        asyncio.run(seed_demo_agents())
    elif arguments.command == "queue-fake-run":
        asyncio.run(queue_demo_run(arguments.questions))
    elif arguments.command == "queue-real-run":
        asyncio.run(queue_real_evaluation(arguments.questions))
    elif arguments.command == "queue-pair-run":
        asyncio.run(queue_pair_evaluation(arguments.agent, arguments.questions))
    elif arguments.command == "wait-run":
        asyncio.run(wait_for_demo_run(arguments.run_id, arguments.timeout))
    elif arguments.command == "init-demo-env":
        output = write_demo_environment(arguments.secret_directory, arguments.output)
        print(f"Local demo environment initialized: {output}")
    elif arguments.command == "run-report":
        asyncio.run(print_run_report(arguments.run_id))
    elif arguments.command == "run-fake-suite":
        asyncio.run(run_fake_suite(arguments.timeout))
    elif arguments.command == "set-deepseek-key":
        key = getpass.getpass("DeepSeek API Key: ")
        path = store_runtime_secret(arguments.directory, "deepseek-api-key", key)
        print(f"DeepSeek API Key stored securely: {path}")
    elif arguments.command == "init-local-real-env":
        output = write_local_real_environment(arguments.secret_directory, arguments.output)
        print(f"Local real-evaluation environment initialized: {output}")
    elif arguments.command == "migrate-database":
        try:
            migration_report = asyncio.run(
                migrate_database(
                    source_url=arguments.source_url,
                    target_url=arguments.target_url,
                    batch_size=arguments.batch_size,
                    dry_run=arguments.dry_run,
                )
            )
        except DatabaseMigrationError as error:
            parser.exit(1, f"database migration failed: {error}\n")
        for table in migration_report.tables:
            print(f"{table.table}: rows={table.row_count}, sha256={table.sha256}")
        action = "dry-run verified" if migration_report.dry_run else "migration verified"
        print(
            f"{action}: tables={len(migration_report.tables)}, "
            f"rows={migration_report.total_rows}"
        )
    elif arguments.command == "sync-artifacts-to-s3":
        try:
            sync_report = asyncio.run(
                sync_local_artifacts_to_s3(
                    settings=Settings(),
                    source_local_dir=arguments.source_local_dir,
                    target_bucket=arguments.target_bucket,
                    target_prefix=arguments.target_prefix,
                    target_endpoint_url=arguments.target_endpoint_url,
                    target_region=arguments.target_region,
                    target_force_path_style=arguments.target_force_path_style,
                    dry_run=arguments.dry_run,
                    concurrency=arguments.concurrency,
                )
            )
        except (ArtifactSyncError, ValueError) as error:
            parser.exit(1, f"artifact sync failed: {error}\n")
        print(
            f"artifact sync: total={sync_report.total}, copied={sync_report.copied}, "
            f"existing={sync_report.existing}, pending={sync_report.pending}"
        )
    elif arguments.command == "export-artifact-archive":
        try:
            asyncio.run(export_configured_artifact_archive(arguments.output))
        except (ArtifactArchiveError, ValueError) as error:
            parser.exit(1, f"artifact archive export failed: {error}\n")
    elif arguments.command == "verify-artifact-archive":
        try:
            verify_configured_artifact_archive(arguments.path)
        except (ArtifactArchiveError, ValueError) as error:
            parser.exit(1, f"artifact archive verification failed: {error}\n")
    elif arguments.command == "restore-artifact-archive":
        try:
            restore_configured_artifact_archive(arguments.path)
        except (ArtifactArchiveError, ValueError) as error:
            parser.exit(1, f"artifact archive restore failed: {error}\n")


if __name__ == "__main__":
    main()
