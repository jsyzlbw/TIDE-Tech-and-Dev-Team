from __future__ import annotations

import asyncio
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

BACKEND_ROOT = Path(__file__).resolve().parents[2]


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


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _seed_v3_subject(
    database_url: str,
    *,
    include_source_report: bool,
) -> tuple[dict[str, uuid.UUID], uuid.UUID | None]:
    ids = {
        name: uuid.uuid4()
        for name in ("teacher", "student", "assignment", "submission", "initial_job")
    }
    source_report_id = uuid.uuid4() if include_source_report else None
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) VALUES "
                    "(:teacher, 'review-v3-teacher', 'Teacher', 'teacher', 'hash', true), "
                    "(:student, 'review-v3-student', 'Student', 'student', 'hash', true)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, 'REVIEW-V3', 'Review', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, content_text, source) "
                    "VALUES (:submission, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                ids,
            )
            if source_report_id is not None:
                await connection.execute(
                    text(
                        "INSERT INTO evaluation_jobs "
                        "(id, submission_id, requested_by, reason, status, idempotency_key, "
                        "attempt_count, provider, model, queued_at, started_at, finished_at) VALUES "
                        "(:initial_job, :submission, :teacher, 'initial', 'succeeded', :key, 1, "
                        "'mock', 'fixture-v1', '2026-07-26T01:00:00+00:00', "
                        "'2026-07-26T01:00:01+00:00', "
                        "'2026-07-26T01:01:00+00:00')"
                    ),
                    {**ids, "key": uuid.uuid4().hex + uuid.uuid4().hex},
                )
                await connection.execute(
                    text(
                        "INSERT INTO evaluation_reports "
                        "(id, submission_id, job_id, origin, version, schema_version, "
                        "completeness, correctness, major_issues, suggestions, score, grade, "
                        "confidence, limitations, raw_model_output, validation_status, "
                        "review_status, created_at) VALUES "
                        "(:report, :submission, :initial_job, 'agent', 1, '1.0', "
                        '\'{"level":"partial"}\'::jsonb, '
                        "'{\"judgment\":\"correct\"}'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                        "80, 'B', 0.8, '[]'::jsonb, '{}', 'valid', 'proposed', "
                        "'2026-07-26T01:01:00+00:00')"
                    ),
                    {**ids, "report": source_report_id},
                )
            manual_job_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model, queued_at) VALUES "
                    "(:job, :submission, :teacher, 'manual_retry', 'queued', :key, 0, "
                    "'mock', 'fixture-v1', '2026-07-26T02:00:00+00:00')"
                ),
                {
                    **ids,
                    "job": manual_job_id,
                    "key": uuid.uuid4().hex + uuid.uuid4().hex,
                },
            )
        ids["manual_job"] = manual_job_id
    finally:
        await engine.dispose()

    return ids, source_report_id


async def _seed_v3_completed_manual_retry_chain(
    database_url: str,
) -> tuple[dict[str, uuid.UUID], tuple[uuid.UUID, uuid.UUID]]:
    ids, first_report_id = await _seed_v3_subject(
        database_url,
        include_source_report=True,
    )
    assert first_report_id is not None
    first_manual_job_id = ids["manual_job"]
    second_manual_job_id = uuid.uuid4()
    second_report_id = uuid.uuid4()
    third_report_id = uuid.uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE evaluation_jobs SET status = 'succeeded', attempt_count = 1, "
                    "started_at = '2026-07-26T02:00:01+00:00', "
                    "finished_at = '2026-07-26T02:01:00+00:00' WHERE id = :job"
                ),
                {"job": first_manual_job_id},
            )
            await connection.execute(
                text(
                    "UPDATE evaluation_reports SET review_status = 'superseded' WHERE id = :report"
                ),
                {"report": first_report_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_reports "
                    "(id, submission_id, job_id, origin, version, schema_version, "
                    "completeness, correctness, major_issues, suggestions, score, grade, "
                    "confidence, limitations, raw_model_output, validation_status, "
                    "review_status, created_at) VALUES "
                    "(:report, :submission, :job, 'agent', 2, '1.0', "
                    '\'{"level":"partial"}\'::jsonb, '
                    "'{\"judgment\":\"correct\"}'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                    "80, 'B', 0.8, '[]'::jsonb, '{}', 'valid', 'superseded', "
                    "'2026-07-26T02:01:00+00:00')"
                ),
                {
                    **ids,
                    "job": first_manual_job_id,
                    "report": second_report_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, job_id, submission_id, provider, model, prompt_template_version, "
                    "schema_version, duration_ms, retry_count, raw_model_output, "
                    "validation_status, validation_failures, final_report_id) VALUES "
                    "(:audit, :job, :submission, 'mock', 'fixture-v1', 'evaluation-v1', "
                    "'1.0', 10, 0, '{}', 'valid', '[]'::jsonb, :report)"
                ),
                {
                    **ids,
                    "audit": uuid.uuid4(),
                    "job": first_manual_job_id,
                    "report": second_report_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model, queued_at, started_at, finished_at) VALUES "
                    "(:job, :submission, :teacher, 'manual_retry', 'succeeded', :key, 1, "
                    "'mock', 'fixture-v1', '2026-07-26T03:00:00+00:00', "
                    "'2026-07-26T03:00:01+00:00', '2026-07-26T03:01:00+00:00')"
                ),
                {
                    **ids,
                    "job": second_manual_job_id,
                    "key": uuid.uuid4().hex + uuid.uuid4().hex,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_reports "
                    "(id, submission_id, job_id, origin, version, schema_version, "
                    "completeness, correctness, major_issues, suggestions, score, grade, "
                    "confidence, limitations, raw_model_output, validation_status, "
                    "review_status, created_at) VALUES "
                    "(:report, :submission, :job, 'agent', 3, '1.0', "
                    '\'{"level":"partial"}\'::jsonb, '
                    "'{\"judgment\":\"correct\"}'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                    "80, 'B', 0.8, '[]'::jsonb, '{}', 'valid', 'proposed', "
                    "'2026-07-26T03:01:00+00:00')"
                ),
                {
                    **ids,
                    "job": second_manual_job_id,
                    "report": third_report_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, job_id, submission_id, provider, model, prompt_template_version, "
                    "schema_version, duration_ms, retry_count, raw_model_output, "
                    "validation_status, validation_failures, final_report_id) VALUES "
                    "(:audit, :job, :submission, 'mock', 'fixture-v1', 'evaluation-v1', "
                    "'1.0', 10, 0, '{}', 'valid', '[]'::jsonb, :report)"
                ),
                {
                    **ids,
                    "audit": uuid.uuid4(),
                    "job": second_manual_job_id,
                    "report": third_report_id,
                },
            )
    finally:
        await engine.dispose()
    ids["second_manual_job"] = second_manual_job_id
    return ids, (second_report_id, third_report_id)


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
async def test_review_migration_fresh_empty_down_up_matches_metadata(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            enums = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_enums()}
            )
            columns = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_columns("evaluation_jobs")}
            )
            diff = await connection.run_sync(_diff)
        assert revision == "0008_account_creation_events"
        assert "review_actions" in tables
        assert "review_action_type" in enums
        assert "source_report_id" in columns
        assert diff == []
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0003_evaluation_delivery")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0003_evaluation_delivery"
            )
            assert await connection.scalar(text("SELECT to_regclass('review_actions')")) is None
            assert (
                await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
                        "WHERE attrelid = 'evaluation_jobs'::regclass "
                        "AND attname = 'source_report_id' AND NOT attisdropped)"
                    )
                )
                is False
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")


@pytest.mark.asyncio
async def test_review_migration_refuses_data_bearing_downgrade_atomically(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    ids = {name: uuid.uuid4() for name in ("teacher", "student", "assignment", "submission")}
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) VALUES "
                    "(:teacher, 'review-migration-teacher', 'Teacher', 'teacher', 'hash', true), "
                    "(:student, 'review-migration-student', 'Student', 'student', 'hash', true)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, 'REVIEW-MIGRATION', 'Review', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, content_text, source) "
                    "VALUES (:submission, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                ids,
            )
            job_id, report_id, action_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model) VALUES "
                    "(:job, :submission, :teacher, 'initial', 'queued', :key, 0, "
                    "'mock', 'fixture-v1')"
                ),
                {**ids, "job": job_id, "key": uuid.uuid4().hex + uuid.uuid4().hex},
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_reports "
                    "(id, submission_id, job_id, origin, version, schema_version, completeness, "
                    "correctness, major_issues, suggestions, score, grade, confidence, limitations, "
                    "raw_model_output, validation_status, review_status) VALUES "
                    "(:report, :submission, :job, 'agent', 1, '1.0', "
                    '\'{"level":"partial"}\'::jsonb, \'{"judgment":"correct"}\'::jsonb, '
                    "'[]'::jsonb, '[]'::jsonb, 80, 'B', 0.8, '[]'::jsonb, '{}', "
                    "'valid', 'proposed')"
                ),
                {**ids, "job": job_id, "report": report_id},
            )
            await connection.execute(
                text(
                    "UPDATE evaluation_reports SET review_status = 'confirmed' WHERE id = :report"
                ),
                {"report": report_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO review_actions "
                    "(id, report_id, teacher_id, action, changes, comment) VALUES "
                    "(:action, :report, :teacher, 'confirm', "
                    '\'{"review_status":{"before":"proposed","after":"confirmed"}}\'::jsonb, \'\')'
                ),
                {**ids, "action": action_id, "report": report_id},
            )
    finally:
        await engine.dispose()

    with pytest.raises(Exception, match="review evidence exists"):
        await asyncio.to_thread(command.downgrade, config, "0003_evaluation_delivery")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM review_actions WHERE id = :id"),
                    {"id": action_id},
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_review_migration_backfills_provable_v3_manual_retry_source(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0003_evaluation_delivery")
    ids, source_report_id = await _seed_v3_subject(
        migration_database_url,
        include_source_report=True,
    )

    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT source_report_id FROM evaluation_jobs WHERE id = :job"),
                    {"job": ids["manual_job"]},
                )
                == source_report_id
            )
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            action = (
                await connection.execute(
                    text(
                        "SELECT report_id, teacher_id, changes "
                        "FROM review_actions WHERE action = 'reevaluate'"
                    )
                )
            ).one()
            assert action.report_id == source_report_id
            assert action.teacher_id == ids["teacher"]
            assert action.changes == {
                "evaluation_job_id": {
                    "before": None,
                    "after": str(ids["manual_job"]),
                },
                "source_review_status": {
                    "before": "proposed",
                    "after": "proposed",
                },
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_review_migration_accepts_completed_multi_retry_history(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0003_evaluation_delivery")
    ids, report_ids = await _seed_v3_completed_manual_retry_chain(migration_database_url)

    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            actions = (
                await connection.execute(
                    text(
                        "SELECT changes #>> '{evaluation_job_id,after}' AS job_id "
                        "FROM review_actions WHERE action = 'reevaluate' ORDER BY job_id"
                    )
                )
            ).scalars()
            assert set(actions) == {
                str(ids["manual_job"]),
                str(ids["second_manual_job"]),
            }
            statuses = (
                await connection.execute(
                    text(
                        "SELECT review_status FROM evaluation_reports "
                        "WHERE id = ANY(:ids) ORDER BY version"
                    ),
                    {"ids": list(report_ids)},
                )
            ).scalars()
            assert list(statuses) == ["superseded", "proposed"]
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_review_migration_aborts_unprovable_v3_manual_retry_atomically(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0003_evaluation_delivery")
    ids, _ = await _seed_v3_subject(
        migration_database_url,
        include_source_report=False,
    )

    with pytest.raises(Exception, match="cannot prove source report"):
        await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0003_evaluation_delivery"
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
                        "WHERE attrelid = 'evaluation_jobs'::regclass "
                        "AND attname = 'source_report_id' AND NOT attisdropped)"
                    )
                )
                is False
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM evaluation_jobs WHERE id = :job"),
                    {"job": ids["manual_job"]},
                )
                == 1
            )
            await connection.execute(
                text("DELETE FROM evaluation_jobs WHERE id = :job"),
                {"job": ids["manual_job"]},
            )
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.upgrade, config, "head")
