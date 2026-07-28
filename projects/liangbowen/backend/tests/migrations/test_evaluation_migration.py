from __future__ import annotations

import asyncio
import importlib
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
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.reviews.model import ReviewAction
from app.submissions.model import Submission
from app.users.model import User

del (
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


def test_orm_and_released_migration_use_identical_v1_identifier_ddl() -> None:
    from app.evaluations import model

    migration = importlib.import_module("migrations.versions.0002_evaluations")

    assert (
        migration.CREATE_SAFE_IDENTIFIER_FUNCTION_SQL == model.CREATE_SAFE_IDENTIFIER_FUNCTION_SQL
    )


BACKEND_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_TABLES = {
    "evaluation_jobs",
    "evaluation_reports",
    "audit_logs",
    "evaluation_outbox",
    "review_actions",
}
EVALUATION_ENUMS = {
    "job_reason",
    "job_status",
    "report_origin",
    "review_status",
    "validation_status",
    "grade",
    "review_action_type",
}


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    return compare_metadata(context, Base.metadata)


async def _reset_test_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migration_database_url() -> str:
    import conftest as conftest_module

    database_url, explicit_database_url = conftest_module._resolve_test_database_url()
    engine = create_async_engine(database_url, connect_args={"timeout": 1})
    try:
        await conftest_module._probe_test_database(engine, explicit_database_url)
    finally:
        await engine.dispose()
    return database_url


@pytest.mark.asyncio
async def test_evaluation_migration_upgrades_existing_0001_and_roundtrips(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_test_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0001_domain")
    preserved_submission_id = uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            teacher_id, student_id, assignment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, password_hash) VALUES "
                    "(:teacher, 'existing-teacher', 'Teacher', 'teacher', 'hash'), "
                    "(:student, 'existing-student', 'Student', 'student', 'hash')"
                ),
                {"teacher": teacher_id, "student": student_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:id, 'HW-EXISTING', 'Existing', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                {"id": assignment_id, "teacher": teacher_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, "
                    "content_text, source) VALUES "
                    "(:id, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                {
                    "id": preserved_submission_id,
                    "assignment": assignment_id,
                    "student": student_id,
                },
            )
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            enums = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_enums()}
            )
            diff = await connection.run_sync(_diff)
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            trigger_names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(ARRAY["
                            "'trg_audit_logs_append_only', 'trg_audit_logs_evidence', "
                            "'trg_audit_logs_no_truncate', "
                            "'trg_evaluation_jobs_audited_immutable', "
                            "'trg_evaluation_reports_version_sequence', "
                            "'trg_evaluation_reports_no_truncate']) "
                            "AND NOT tgisinternal"
                        )
                    )
                )
                .scalars()
                .all()
            )
            audit_evidence_function = await connection.scalar(
                text("SELECT pg_get_functiondef('validate_audit_log_evidence()'::regprocedure)")
            )
            identifier_contract = (
                await connection.execute(
                    text(
                        "SELECT is_safe_identifier('模型😀', 128), "
                        "is_safe_identifier(U&'mock\\202Eoverride', 128), "
                        "is_safe_identifier(U&'Cafe\\0301', 128)"
                    )
                )
            ).one()
            preserved = await connection.scalar(
                text("SELECT content_text FROM submissions WHERE id = :id"),
                {"id": preserved_submission_id},
            )
        assert EVALUATION_TABLES <= tables
        assert EVALUATION_ENUMS <= enums
        assert revision == "0008_account_creation_events"
        assert trigger_names == {
            "trg_audit_logs_append_only",
            "trg_audit_logs_evidence",
            "trg_audit_logs_no_truncate",
            "trg_evaluation_jobs_audited_immutable",
            "trg_evaluation_reports_no_truncate",
            "trg_evaluation_reports_version_sequence",
        }
        assert "FOR UPDATE" in audit_evidence_function
        assert identifier_contract == (True, False, True)
        assert preserved == "answer"
        assert diff == []
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0001_domain")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            enums = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_enums()}
            )
            functions_exist = (
                (
                    await connection.execute(
                        text(
                            "SELECT to_regprocedure(name) IS NOT NULL FROM unnest(ARRAY["
                            "'prevent_audit_log_mutation()', "
                            "'prevent_evaluation_evidence_truncate()', "
                            "'is_safe_identifier(text,integer)', "
                            "'validate_audit_log_evidence()', "
                            "'protect_audited_evaluation_job()', "
                            "'enforce_evaluation_report_version_sequence()', "
                            "'validate_evaluation_report_lineage()', "
                            "'protect_evaluation_report_version()', "
                            "'validate_evaluation_job_requester()']) AS name"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert not EVALUATION_TABLES & tables
        assert not EVALUATION_ENUMS & enums
        assert not any(functions_exist)
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")


@pytest.mark.asyncio
async def test_delivery_migration_backfills_existing_queued_job_and_down_up_roundtrips(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_test_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0002_evaluations")
    teacher_id = uuid.uuid4()
    student_id = uuid.uuid4()
    assignment_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    job_id = uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, password_hash) VALUES "
                    "(:teacher, 'delivery-teacher', 'Teacher', 'teacher', 'hash'), "
                    "(:student, 'delivery-student', 'Student', 'student', 'hash')"
                ),
                {"teacher": teacher_id, "student": student_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, 'DELIVERY', 'Delivery', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                {"assignment": assignment_id, "teacher": teacher_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, "
                    "content_text, source) VALUES "
                    "(:submission, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                {
                    "submission": submission_id,
                    "assignment": assignment_id,
                    "student": student_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model) VALUES "
                    "(:job, :submission, :teacher, 'initial', 'queued', :key, 0, "
                    "'mock', 'fixture-v1')"
                ),
                {
                    "job": job_id,
                    "submission": submission_id,
                    "teacher": teacher_id,
                    "key": uuid.uuid4().hex + uuid.uuid4().hex,
                },
            )
    finally:
        await engine.dispose()

    for _ in range(2):
        await asyncio.to_thread(command.upgrade, config, "head")
        engine = create_async_engine(migration_database_url)
        try:
            async with engine.connect() as connection:
                outbox = (
                    await connection.execute(
                        text(
                            "SELECT kind, delivered_at FROM evaluation_outbox WHERE job_id = :job"
                        ),
                        {"job": job_id},
                    )
                ).one()
                assert outbox == ("dispatch", None)
        finally:
            await engine.dispose()
        await asyncio.to_thread(command.downgrade, config, "0002_evaluations")
        engine = create_async_engine(migration_database_url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT to_regclass('evaluation_outbox')")) is None
                )
        finally:
            await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")


@pytest.mark.asyncio
async def test_delivery_migration_downgrade_preserves_real_cancelled_provider_evidence(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_test_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    teacher_id = uuid.uuid4()
    student_id = uuid.uuid4()
    assignment_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    job_id = uuid.uuid4()
    raw_evidence = '{"cancelled":"after-provider"}'
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, username, display_name, role, password_hash) VALUES "
                    "(:teacher, 'cancelled-teacher', 'Teacher', 'teacher', 'hash'), "
                    "(:student, 'cancelled-student', 'Student', 'student', 'hash')"
                ),
                {"teacher": teacher_id, "student": student_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, 'CANCELLED-EVIDENCE', 'Cancelled', 'Question', "
                    "'{}'::jsonb, '2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                {"assignment": assignment_id, "teacher": teacher_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, "
                    "content_text, source) VALUES "
                    "(:submission, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                {
                    "submission": submission_id,
                    "assignment": assignment_id,
                    "student": student_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model, started_at, finished_at) VALUES "
                    "(:job, :submission, :teacher, 'initial', 'cancelled', :key, 1, "
                    "'mock', 'fixture-v1', now(), now())"
                ),
                {
                    "job": job_id,
                    "submission": submission_id,
                    "teacher": teacher_id,
                    "key": uuid.uuid4().hex + uuid.uuid4().hex,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, job_id, submission_id, provider, model, prompt_template_version, "
                    "schema_version, duration_ms, retry_count, raw_model_output, "
                    "validation_failures, error_type, error_message) VALUES "
                    "(:id, :job, :submission, 'mock', 'fixture-v1', 'evaluation-v1', "
                    "'1.0', 9, 0, :raw, '[]'::jsonb, 'cancelled', "
                    "'evaluation cancelled after provider execution')"
                ),
                {
                    "id": uuid.uuid4(),
                    "job": job_id,
                    "submission": submission_id,
                    "raw": raw_evidence,
                },
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0002_evaluations")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT raw_model_output FROM audit_logs WHERE job_id = :job"),
                    {"job": job_id},
                )
                == raw_evidence
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0002_evaluations"
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT raw_model_output FROM audit_logs WHERE job_id = :job"),
                    {"job": job_id},
                )
                == raw_evidence
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_evaluation_migration_offline_sql_contains_append_only_trigger() -> None:
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
    assert b"trg_audit_logs_append_only" in stdout
    assert b"trg_audit_logs_evidence" in stdout
    assert b"trg_audit_logs_no_truncate" in stdout
    assert b"trg_evaluation_jobs_audited_immutable" in stdout
    assert b"trg_evaluation_reports_no_truncate" in stdout
    assert b"trg_evaluation_reports_version_sequence" in stdout
    assert b"evaluation_outbox" in stdout
    assert b"evaluation cancelled after provider execution" in stdout
    assert b"unicode_assigned" not in stdout
    assert b"FOR UPDATE" in stdout
    assert b"secret" not in stdout + stderr
