from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.assignments.model import Assignment
from app.audit.model import AuditLog
from app.db.base import Base
from app.db.migration_gate import (
    LEGACY_QUIESCENCE_CONFIRMATION_ENV,
    LEGACY_QUIESCENCE_OFFLINE_COMMENT,
    MIGRATION_GATE_EXCLUSIVE_SQL_TEXT,
    MIGRATION_GATE_LOCK_KEY_V1,
)
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.reviews.model import ReviewAction
from app.submissions.model import Submission
from app.users.creation_event import AccountCreationEvent
from app.users.model import User

del (
    AccountCreationEvent,
    Assignment,
    AuditLog,
    EvaluationJob,
    EvaluationOutbox,
    EvaluationReport,
    IntegrationEvent,
    MattermostIdentity,
    ReviewAction,
    Submission,
    User,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_TABLES = {"users", "assignments", "submissions"}
DOMAIN_ENUMS = {
    "role",
    "assignment_status",
    "submission_content_type",
    "submission_status",
    "submission_source",
    "job_reason",
    "job_status",
    "report_origin",
    "review_status",
    "validation_status",
    "grade",
    "review_action_type",
    "integration_event_status",
}


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def _reset_domain_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            if await connection.scalar(
                text("SELECT to_regclass('public.account_creation_events') IS NOT NULL")
            ):
                for trigger_name in (
                    "trg_account_creation_events_no_update",
                    "trg_account_creation_events_no_delete",
                    "trg_account_creation_events_no_truncate",
                ):
                    await connection.execute(
                        text(f'DROP TRIGGER IF EXISTS "{trigger_name}" ON account_creation_events')
                    )
            for table_name in (
                "account_creation_events",
                "integration_events",
                "mattermost_identities",
                "review_actions",
                "audit_logs",
                "evaluation_outbox",
                "evaluation_reports",
                "evaluation_jobs",
                "submissions",
                "assignments",
                "users",
                "alembic_version",
            ):
                await connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
            await connection.execute(text("DROP FUNCTION IF EXISTS protect_review_action()"))
            await connection.execute(
                text("DROP FUNCTION IF EXISTS guard_integration_event_evidence()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS prevent_integration_event_truncate()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS guard_mattermost_identity_authority()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS prevent_mattermost_identity_truncate()")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS validate_review_action()"))
            await connection.execute(text("DROP FUNCTION IF EXISTS prevent_audit_log_mutation()"))
            await connection.execute(
                text("DROP FUNCTION IF EXISTS prevent_evaluation_evidence_truncate()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS is_safe_identifier(text, integer)")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS validate_audit_log_evidence()"))
            await connection.execute(
                text("DROP FUNCTION IF EXISTS protect_audited_evaluation_job()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS enforce_evaluation_report_version_sequence()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS validate_evaluation_report_lineage()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS protect_evaluation_report_version()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS validate_evaluation_job_requester()")
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS prevent_account_creation_event_mutation()")
            )
            await connection.execute(text("DROP SEQUENCE IF EXISTS assignment_code_seq CASCADE"))
            for enum_name in DOMAIN_ENUMS:
                await connection.execute(text(f'DROP TYPE IF EXISTS "{enum_name}" CASCADE'))
    finally:
        await engine.dispose()


def _schema_diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
        },
    )
    return compare_metadata(context, Base.metadata)


def _domain_objects(connection: Connection) -> tuple[set[str], set[str], set[str]]:
    inspector = inspect(connection)
    tables = DOMAIN_TABLES.intersection(inspector.get_table_names())
    enums = {enum["name"] for enum in inspector.get_enums() if enum["name"] in DOMAIN_ENUMS}
    sequences = {
        row.name
        for row in connection.execute(
            text(
                "SELECT c.relname AS name FROM pg_class AS c "
                "WHERE c.relkind = 'S' AND c.relname = 'assignment_code_seq'"
            )
        )
    }
    return tables, enums, sequences


@pytest_asyncio.fixture
async def migration_database_url() -> str:
    import conftest as conftest_module

    database_url, explicit_database_url = conftest_module._resolve_test_database_url()
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 1},
    )
    try:
        await conftest_module._probe_test_database(engine, explicit_database_url)
    finally:
        await engine.dispose()
    return database_url


async def _run_alembic_upgrade(database_url: str) -> tuple[int, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(BACKEND_ROOT / "alembic.ini"),
        "upgrade",
        "head",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, (stdout + stderr).decode()


@pytest.mark.asyncio
async def test_offline_upgrade_emits_quiescence_prerequisite_and_gate_without_credentials() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "postgresql+asyncpg://offline:secret@example.invalid/offline",
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(BACKEND_ROOT / "alembic.ini"),
        "upgrade",
        "head",
        "--sql",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    assert LEGACY_QUIESCENCE_OFFLINE_COMMENT.encode() in stdout
    assert stdout.count(MIGRATION_GATE_EXCLUSIVE_SQL_TEXT.encode()) == 1
    assert b"secret" not in stdout + stderr


@pytest.mark.asyncio
async def test_offline_upgrade_requires_explicit_quiescence_confirmation() -> None:
    environment = os.environ.copy()
    environment.pop(LEGACY_QUIESCENCE_CONFIRMATION_ENV, None)
    environment.update(
        {
            "DATABASE_URL": "postgresql+asyncpg://offline:secret@example.invalid/offline",
            "PYTHONPATH": str(BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(BACKEND_ROOT / "alembic.ini"),
        "upgrade",
        "head",
        "--sql",
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode != 0
    assert b"legacy migration quiescence confirmation required" in stderr
    assert b"secret" not in stdout + stderr


@pytest.mark.asyncio
async def test_two_steady_state_database_upgrades_are_serialized(
    migration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = migration_database_url
    monkeypatch.setenv("DATABASE_URL", database_url)
    await _reset_domain_schema(database_url)
    bootstrap = await _run_alembic_upgrade(database_url)
    assert bootstrap[0] == 0, bootstrap[1]
    monkeypatch.delenv(LEGACY_QUIESCENCE_CONFIRMATION_ENV)

    first, second = await asyncio.gather(
        _run_alembic_upgrade(database_url),
        _run_alembic_upgrade(database_url),
    )

    assert first[0] == 0, first[1]
    assert second[0] == 0, second[1]
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revisions = (
                (await connection.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
            )
            diff = await connection.run_sync(_schema_diff)
            assert revisions == ["0008_account_creation_events"]
            assert diff == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_lock_timeout_does_not_leak_database_credentials(
    migration_database_url: str,
) -> None:
    database_url = migration_database_url
    await _reset_domain_schema(database_url)
    bootstrap = await _run_alembic_upgrade(database_url)
    assert bootstrap[0] == 0, bootstrap[1]
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": MIGRATION_GATE_LOCK_KEY_V1},
            )
            protected_url = database_url.replace("@", ":migration-lock-secret@", 1)
            separator = "&" if "?" in protected_url else "?"
            protected_url = f"{protected_url}{separator}command_timeout=0.1"
            return_code, output = await _run_alembic_upgrade(protected_url)

            assert return_code != 0
            assert "migration-lock-secret" not in output
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_domain_ddl_rolls_back_version_types_and_sequence(
    migration_database_url: str,
) -> None:
    database_url = migration_database_url
    await _reset_domain_schema(database_url)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE users (sentinel integer NOT NULL)"))
    finally:
        await engine.dispose()

    return_code, _ = await _run_alembic_upgrade(database_url)
    assert return_code != 0

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables, enums, sequences = await connection.run_sync(_domain_objects)
            assert tables == {"users"}
            assert enums == set()
            assert sequences == set()
            assert not await connection.scalar(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            )
            sentinel_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("users")
                }
            )
            assert sentinel_columns == {"sentinel"}
    finally:
        await engine.dispose()
    await _reset_domain_schema(database_url)


@pytest.mark.asyncio
async def test_initial_migration_matches_metadata_and_roundtrips(
    migration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = migration_database_url
    monkeypatch.setenv("DATABASE_URL", database_url)
    await _reset_domain_schema(database_url)
    config = _alembic_config(database_url)

    await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            diff = await connection.run_sync(_schema_diff)
            assert diff == []

            teacher_id = uuid.UUID("10000000-0000-4000-8000-000000000001")
            student_id = uuid.UUID("10000000-0000-4000-8000-000000000002")
            assignment_id = uuid.UUID("20000000-0000-4000-8000-000000000001")
            submission_id = uuid.UUID("30000000-0000-4000-8000-000000000001")
            await connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, password_hash) "
                    "VALUES (:teacher_id, 'teacher-roundtrip', 'Teacher', 'teacher', 'hash'), "
                    "(:student_id, 'student-roundtrip', 'Student', 'student', 'hash')"
                ),
                {"teacher_id": teacher_id, "student_id": student_id},
            )
            user_defaults = (
                await connection.execute(
                    text("SELECT is_active, created_at FROM users WHERE id = :teacher_id"),
                    {"teacher_id": teacher_id},
                )
            ).one()
            assert user_defaults.is_active is True
            assert user_defaults.created_at.tzinfo is not None

            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) "
                    "VALUES (:id, 'HW-9000', 'Dijkstra', 'Explain', "
                    "CAST(:rubric AS jsonb), '2099-01-01T00:00:00+00:00', "
                    "'published', :teacher_id)"
                ),
                {
                    "id": assignment_id,
                    "teacher_id": teacher_id,
                    "rubric": '{"required_points":["nonnegative","relaxation"]}',
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, "
                    "content_text, content_json, source) "
                    "VALUES (:id, :assignment_id, :student_id, 1, 'structured', "
                    "'answer', CAST(:content_json AS jsonb), 'web')"
                ),
                {
                    "id": submission_id,
                    "assignment_id": assignment_id,
                    "student_id": student_id,
                    "content_json": '{"distance":{"A":0,"B":3}}',
                },
            )
            submission = (
                await connection.execute(
                    text(
                        "SELECT content_json, status, submitted_at FROM submissions WHERE id = :id"
                    ),
                    {"id": submission_id},
                )
            ).one()
            assert submission.content_json == {"distance": {"A": 0, "B": 3}}
            assert submission.status == "submitted"
            assert submission.submitted_at.tzinfo is not None

            with pytest.raises(Exception, match="ck_submissions_version_positive"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO submissions "
                            "(id, assignment_id, student_id, version, content_type, "
                            "content_text, source) VALUES "
                            "(gen_random_uuid(), :assignment_id, :student_id, 0, "
                            "'text', 'bad', 'web')"
                        ),
                        {"assignment_id": assignment_id, "student_id": student_id},
                    )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "base")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables, enums, sequences = await connection.run_sync(_domain_objects)
            assert tables == set()
            assert enums == set()
            assert sequences == set()
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")
