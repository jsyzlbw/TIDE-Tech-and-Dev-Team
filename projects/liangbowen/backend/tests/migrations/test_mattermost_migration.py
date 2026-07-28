from __future__ import annotations

import ast
import asyncio
import inspect as python_inspect
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
from app.db.migration_gate import migration_gated_sessionmaker
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
LEGACY_MATTERMOST_ID_CHECK = (
    "octet_length(mattermost_user_id) BETWEEN 1 AND 128 "
    "AND mattermost_user_id = btrim(mattermost_user_id)"
)
LEGACY_QUIESCENCE_ENV = "MIGRATION_LEGACY_PROCESSES_STOPPED"


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


async def _replace_with_physical_legacy_id_constraint(connection) -> None:
    await connection.execute(
        text(
            "ALTER TABLE mattermost_identities "
            "DROP CONSTRAINT ck_mattermost_identities_user_id_safe"
        )
    )
    await connection.execute(
        text(
            "ALTER TABLE mattermost_identities ADD CONSTRAINT "
            "ck_mattermost_identities_user_id_safe CHECK (" + LEGACY_MATTERMOST_ID_CHECK + ")"
        )
    )


async def _assert_still_at_0005_without_a8_ddl(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0005_mattermost"
            )
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'evaluation_outbox' AND column_name = 'failed_at')"
                )
            )
    finally:
        await engine.dispose()


def test_published_0005_keeps_the_a7_physical_identity_constraint() -> None:
    source = (BACKEND_ROOT / "migrations/versions/0005_mattermost.py").read_text()
    assert '"octet_length(mattermost_user_id) BETWEEN 1 AND 128 "' in source
    assert '"AND mattermost_user_id = btrim(mattermost_user_id)"' in source
    assert "MATTERMOST_ID_SQL" not in source


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
async def test_0006_upgrades_a_physical_legacy_constraint_with_numeric_id(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    user_id = uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await _replace_with_physical_legacy_id_constraint(connection)
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'Legacy', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"legacy-valid-{uuid.uuid4().hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO mattermost_identities "
                    "(user_id, mattermost_user_id, mattermost_username) "
                    "VALUES (:user_id, '0123456789abcdefghijklmnop', 'Legacy')"
                ),
                {"user_id": user_id},
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            definition = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_mattermost_identities_user_id_safe'"
                )
            )
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0008_account_creation_events"
        assert "[A-Za-z0-9]" in definition
        assert LEGACY_MATTERMOST_ID_CHECK not in definition
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "confirmation",
    [None, "", "1", "TRUE", "yes", " true ", "migration-confirm-secret"],
)
@pytest.mark.asyncio
async def test_0006_requires_exact_legacy_process_confirmation_before_ddl(
    migration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str | None,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    if confirmation is None:
        monkeypatch.delenv(LEGACY_QUIESCENCE_ENV, raising=False)
    else:
        monkeypatch.setenv(LEGACY_QUIESCENCE_ENV, confirmation)

    with pytest.raises(Exception, match="legacy migration quiescence confirmation required") as exc:
        await asyncio.to_thread(command.upgrade, config, "head")

    assert "migration-confirm-secret" not in str(exc.value)
    await _assert_still_at_0005_without_a8_ddl(migration_database_url)


@pytest.mark.parametrize("connection_state", ["idle", "active"])
@pytest.mark.asyncio
async def test_0006_confirmed_bootstrap_refuses_other_client_backend_before_locks(
    migration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    connection_state: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    monkeypatch.setenv(LEGACY_QUIESCENCE_ENV, "true")
    engine = create_async_engine(migration_database_url)
    old_connection = await engine.connect()
    active_query = None
    try:
        if connection_state == "active":
            active_query = asyncio.create_task(old_connection.execute(text("SELECT pg_sleep(0.5)")))
            await asyncio.sleep(0.05)
        else:
            await old_connection.execute(text("SELECT 1"))
            await old_connection.commit()

        with pytest.raises(
            Exception,
            match=r"legacy migration quiescence check failed: other client connection count=1$",
        ) as exc:
            await asyncio.wait_for(
                asyncio.to_thread(command.upgrade, config, "head"),
                timeout=1,
            )
        error = str(exc.value)
        assert migration_database_url not in error
        assert "SELECT pg_sleep" not in error
        assert "grader" not in error
    finally:
        if active_query is not None:
            await active_query
            await old_connection.commit()
        await old_connection.close()
        await engine.dispose()

    await _assert_still_at_0005_without_a8_ddl(migration_database_url)


@pytest.mark.parametrize("legacy_id", ["legacy/user", "_bad-leading", "bad user"])
@pytest.mark.asyncio
async def test_0006_rejects_invalid_legacy_ids_before_any_ddl(
    migration_database_url: str,
    legacy_id: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    user_id = uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await _replace_with_physical_legacy_id_constraint(connection)
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'Legacy', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"legacy-invalid-{uuid.uuid4().hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO mattermost_identities "
                    "(user_id, mattermost_user_id, mattermost_username) "
                    "VALUES (:user_id, :mattermost_user_id, 'Legacy')"
                ),
                {"user_id": user_id, "mattermost_user_id": legacy_id},
            )
    finally:
        await engine.dispose()

    with pytest.raises(Exception, match="invalid Mattermost identity count: 1"):
        await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            preserved = await connection.scalar(
                text(
                    "SELECT mattermost_user_id FROM mattermost_identities WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
            definition = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_mattermost_identities_user_id_safe'"
                )
            )
            has_a8_column = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'evaluation_outbox' AND column_name = 'failed_at')"
                )
            )
        assert revision == "0005_mattermost"
        assert preserved == legacy_id
        assert "octet_length" in definition
        assert "btrim" in definition
        assert "~" not in definition
        assert has_a8_column is False

        # The failed migration transaction must release its exclusive gate so
        # a new application transaction can immediately take the shared gate.
        factory = migration_gated_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            assert await asyncio.wait_for(session.scalar(text("SELECT 1")), timeout=0.5) == 1
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0006_preflight_refuses_inflight_legacy_write_before_locks(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    user_id = uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    writer = None
    writer_transaction = None
    try:
        async with engine.begin() as connection:
            await _replace_with_physical_legacy_id_constraint(connection)
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'Inflight', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"inflight-{uuid.uuid4().hex}"},
            )

        writer = await engine.connect()
        writer_transaction = await writer.begin()
        await writer.execute(
            text(
                "INSERT INTO mattermost_identities "
                "(user_id, mattermost_user_id, mattermost_username) "
                "VALUES (:user_id, 'legacy/user', 'Inflight')"
            ),
            {"user_id": user_id},
        )
        with pytest.raises(
            Exception,
            match=r"legacy migration quiescence check failed: other client connection count=1$",
        ):
            await asyncio.wait_for(
                asyncio.to_thread(command.upgrade, config, "head"),
                timeout=1,
            )

        await writer_transaction.commit()

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0005_mattermost"
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT mattermost_user_id FROM mattermost_identities "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
                == "legacy/user"
            )
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'evaluation_outbox' AND column_name = 'failed_at')"
                )
            )
    finally:
        if writer_transaction is not None and writer_transaction.is_active:
            await writer_transaction.rollback()
        if writer is not None:
            await writer.close()
        await engine.dispose()


@pytest.mark.parametrize(
    "topology", ["delivery_outbox_then_identity", "action_identity_then_outbox"]
)
@pytest.mark.asyncio
async def test_0006_preflight_refuses_legacy_reverse_lock_topologies_without_attempt(
    migration_database_url: str,
    topology: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    ids = {
        name: uuid.uuid4()
        for name in ("teacher", "student", "assignment", "submission", "job", "outbox")
    }
    engine = create_async_engine(migration_database_url)
    application_connection = None
    application_transaction = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) VALUES "
                    "(:teacher, :teacher_name, 'Teacher', 'teacher', 'hash', true), "
                    "(:student, :student_name, 'Student', 'student', 'hash', true)"
                ),
                {
                    **ids,
                    "teacher_name": f"lock-teacher-{uuid.uuid4().hex}",
                    "student_name": f"lock-student-{uuid.uuid4().hex}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, :code, 'Lock order', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                {**ids, "code": f"LOCK-{uuid.uuid4().hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, content_text, source) "
                    "VALUES (:submission, :assignment, :student, 1, 'text', 'answer', 'web')"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model) VALUES "
                    "(:job, :submission, :teacher, 'initial', 'queued', :key, 0, "
                    "'mock', 'fixture-v1')"
                ),
                {**ids, "key": uuid.uuid4().hex + uuid.uuid4().hex},
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_outbox "
                    "(id, kind, job_id, report_id, attempt_count, next_attempt_at) "
                    "VALUES (:outbox, 'dispatch', :job, NULL, 0, now())"
                ),
                ids,
            )

        application_connection = await engine.connect()
        application_transaction = await application_connection.begin()
        if topology == "delivery_outbox_then_identity":
            assert (
                await application_connection.scalar(
                    text(
                        "SELECT attempt_count FROM evaluation_outbox WHERE id = :outbox FOR UPDATE"
                    ),
                    ids,
                )
                == 0
            )
        else:
            await application_connection.execute(
                text(
                    "INSERT INTO mattermost_identities "
                    "(user_id, mattermost_user_id, mattermost_username) "
                    "VALUES (:teacher, 'lock-safe-user', 'Lock Safe')"
                ),
                ids,
            )

        with pytest.raises(
            Exception,
            match=r"legacy migration quiescence check failed: other client connection count=1$",
        ):
            await asyncio.wait_for(
                asyncio.to_thread(command.upgrade, config, "head"),
                timeout=1,
            )

        if topology == "delivery_outbox_then_identity":
            await application_connection.execute(
                text(
                    "INSERT INTO mattermost_identities "
                    "(user_id, mattermost_user_id, mattermost_username) "
                    "VALUES (:teacher, 'lock-safe-user', 'Lock Safe')"
                ),
                ids,
            )
        else:
            assert (
                await application_connection.scalar(
                    text(
                        "SELECT attempt_count FROM evaluation_outbox WHERE id = :outbox FOR UPDATE"
                    ),
                    ids,
                )
                == 0
            )
        await application_transaction.commit()

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0005_mattermost"
            )
            assert (
                await connection.scalar(
                    text("SELECT attempt_count FROM evaluation_outbox WHERE id = :outbox"),
                    ids,
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT mattermost_user_id FROM mattermost_identities "
                        "WHERE user_id = :teacher"
                    ),
                    ids,
                )
                == "lock-safe-user"
            )
    finally:
        if application_transaction is not None and application_transaction.is_active:
            await application_transaction.rollback()
        if application_connection is not None:
            await application_connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_mattermost_migration_fresh_empty_down_up_matches_metadata(
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
            diff = await connection.run_sync(_diff)
        assert revision == "0008_account_creation_events"
        assert {"mattermost_identities", "integration_events"} <= tables
        assert "integration_event_status" in enums
        assert diff == []
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0004_reviews")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0004_reviews"
            )
            assert await connection.scalar(text("SELECT to_regclass('integration_events')")) is None
            assert (
                await connection.scalar(text("SELECT to_regclass('mattermost_identities')")) is None
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")


def test_mattermost_downgrade_locks_evidence_tables_before_empty_check() -> None:
    migration = __import__(
        "migrations.versions.0005_mattermost",
        fromlist=["downgrade"],
    )
    source = python_inspect.getsource(migration.downgrade)
    lock_offset = source.index("LOCK TABLE integration_events, mattermost_identities")
    check_offset = source.index("IF EXISTS (SELECT 1 FROM integration_events)")
    assert lock_offset < check_offset


def test_delivery_downgrade_locks_evidence_tables_before_empty_check() -> None:
    migration = __import__(
        "migrations.versions.0006_mattermost_delivery",
        fromlist=["downgrade"],
    )
    source = python_inspect.getsource(migration.downgrade)
    lock_offset = source.index("LOCK TABLE evaluation_outbox, integration_events")
    check_offset = source.index("IF EXISTS (")
    assert lock_offset < check_offset


def test_delivery_revision_takes_gate_then_consistently_ordered_table_lock() -> None:
    migration = __import__(
        "migrations.versions.0006_mattermost_delivery",
        fromlist=["upgrade"],
    )
    lock = (
        "LOCK TABLE evaluation_outbox, integration_events, mattermost_identities "
        "IN ACCESS EXCLUSIVE MODE"
    )
    for operation in (migration.upgrade, migration.downgrade):
        source = python_inspect.getsource(operation)
        function = ast.parse(source).body[0]
        first_call = function.body[0].value  # type: ignore[attr-defined]
        second_call = function.body[1].value  # type: ignore[attr-defined]
        assert first_call.args[0].id == "MIGRATION_GATE_EXCLUSIVE_SQL_TEXT"
        assert ast.literal_eval(second_call.args[0]) == lock
        assert source.count("LOCK TABLE") == 1


@pytest.mark.asyncio
async def test_mattermost_downgrade_waits_for_inflight_evidence_then_refuses(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    user_id = uuid.uuid4()
    writer = None
    writer_transaction = None
    downgrade_task = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'Mattermost', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"race-{uuid.uuid4().hex}"},
            )

        writer = await engine.connect()
        writer_transaction = await writer.begin()
        await writer.execute(
            text(
                "INSERT INTO mattermost_identities "
                "(user_id, mattermost_user_id, mattermost_username) "
                "VALUES (:user_id, 'mm-inflight', 'Inflight')"
            ),
            {"user_id": user_id},
        )
        downgrade_task = asyncio.create_task(
            asyncio.to_thread(command.downgrade, config, "0004_reviews")
        )

        observed_lock_wait = False
        async with engine.connect() as observer:
            for _ in range(100):
                observed_lock_wait = bool(
                    await observer.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_locks AS locks "
                            "JOIN pg_class AS relations ON relations.oid = locks.relation "
                            "WHERE NOT locks.granted "
                            "AND locks.mode = 'AccessExclusiveLock' "
                            "AND relations.relname IN "
                            "('users', 'integration_events', 'mattermost_identities'))"
                        )
                    )
                )
                if observed_lock_wait:
                    break
                await asyncio.sleep(0.02)
        assert observed_lock_wait

        await writer_transaction.commit()
        with pytest.raises(Exception, match="Mattermost integration evidence exists"):
            await asyncio.wait_for(downgrade_task, timeout=3)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            assert await connection.scalar(text("SELECT count(*) FROM mattermost_identities")) == 1
    finally:
        if writer_transaction is not None and writer_transaction.is_active:
            await writer_transaction.rollback()
        if writer is not None:
            await writer.close()
        if downgrade_task is not None and not downgrade_task.done():
            await asyncio.gather(downgrade_task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("evidence", ["identity", "event"])
@pytest.mark.asyncio
async def test_mattermost_migration_refuses_downgrade_and_preserves_evidence(
    migration_database_url: str,
    evidence: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    user_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'Mattermost', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"mm-{uuid.uuid4().hex}"},
            )
            if evidence == "identity":
                await connection.execute(
                    text(
                        "INSERT INTO mattermost_identities "
                        "(user_id, mattermost_user_id, mattermost_username) "
                        "VALUES (:user_id, 'mm-authority', 'Display')"
                    ),
                    {"user_id": user_id},
                )
            else:
                await connection.execute(
                    text(
                        "INSERT INTO integration_events "
                        "(id, request_hash, actor_user_id) VALUES (:id, :hash, :user_id)"
                    ),
                    {"id": evidence_id, "hash": "a" * 64, "user_id": user_id},
                )
    finally:
        await engine.dispose()

    with pytest.raises(Exception, match="Mattermost integration evidence exists"):
        await asyncio.to_thread(command.downgrade, config, "0004_reviews")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            table = "mattermost_identities" if evidence == "identity" else "integration_events"
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_migration_backfills_legacy_rows_without_touching_dispatch(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "0005_mattermost")
    ids = {
        name: uuid.uuid4()
        for name in (
            "teacher",
            "student",
            "assignment",
            "submission_one",
            "submission_two",
            "job_one",
            "job_two",
            "report_one",
            "report_two",
            "delivered",
            "pending",
            "dispatch",
        )
    }
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) VALUES "
                    "(:teacher, 'a8-migration-teacher', 'Teacher', 'teacher', 'hash', true), "
                    "(:student, 'a8-migration-student', 'Student', 'student', 'hash', true)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO assignments "
                    "(id, code, title, question, rubric, due_at, status, created_by) VALUES "
                    "(:assignment, 'A8-MIGRATION', 'A8', 'Question', '{}'::jsonb, "
                    "'2099-01-01T00:00:00+00:00', 'published', :teacher)"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions "
                    "(id, assignment_id, student_id, version, content_type, content_text, source) "
                    "VALUES "
                    "(:submission_one, :assignment, :student, 1, 'text', 'one', 'web'), "
                    "(:submission_two, :assignment, :student, 2, 'text', 'two', 'web')"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_jobs "
                    "(id, submission_id, requested_by, reason, status, idempotency_key, "
                    "attempt_count, provider, model, queued_at, started_at, finished_at) VALUES "
                    "(:job_one, :submission_one, :teacher, 'initial', 'succeeded', :key_one, 1, "
                    "'mock', 'fixture-v1', now(), now(), now()), "
                    "(:job_two, :submission_two, :teacher, 'initial', 'succeeded', :key_two, 1, "
                    "'mock', 'fixture-v1', now(), now(), now())"
                ),
                {
                    **ids,
                    "key_one": uuid.uuid4().hex + uuid.uuid4().hex,
                    "key_two": uuid.uuid4().hex + uuid.uuid4().hex,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_reports "
                    "(id, submission_id, job_id, origin, version, schema_version, completeness, "
                    "correctness, major_issues, suggestions, score, grade, confidence, limitations, "
                    "raw_model_output, validation_status, review_status) VALUES "
                    "(:report_one, :submission_one, :job_one, 'agent', 1, '1.0', "
                    "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 80, 'B', 0.8, "
                    "'[]'::jsonb, '{}', 'valid', 'proposed'), "
                    "(:report_two, :submission_two, :job_two, 'agent', 1, '1.0', "
                    "'{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 80, 'B', 0.8, "
                    "'[]'::jsonb, '{}', 'valid', 'proposed')"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluation_outbox "
                    "(id, kind, job_id, report_id, attempt_count, next_attempt_at, "
                    "delivered_at, last_error_type, created_at) VALUES "
                    "(:delivered, 'notification', :job_one, :report_one, 999, now(), now(), "
                    "'OSError', now()), "
                    "(:pending, 'notification', :job_two, :report_two, 999, now(), NULL, "
                    "'OSError', now()), "
                    "(:dispatch, 'dispatch', :job_one, NULL, 999, now(), NULL, "
                    "'OSError', now())"
                ),
                ids,
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            rows = {
                row.id: row
                for row in (
                    await connection.execute(
                        text(
                            "SELECT id, attempt_count, delivery_ref, last_error_type, "
                            "last_error_summary FROM evaluation_outbox "
                            "WHERE id IN (:delivered, :pending, :dispatch)"
                        ),
                        ids,
                    )
                )
            }
        assert rows[ids["delivered"]][1:] == (3, "legacy-unverified", None, None)
        assert rows[ids["pending"]][1:] == (
            2,
            None,
            "OSError",
            "Mattermost delivery failed",
        )
        assert rows[ids["dispatch"]][1:] == (999, None, "OSError", None)
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "0005_mattermost")
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0005_mattermost"
            )
            columns = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_columns("evaluation_outbox")
                }
            )
            attempts = (
                await connection.execute(
                    text(
                        "SELECT id, attempt_count FROM evaluation_outbox "
                        "WHERE id IN (:delivered, :pending, :dispatch)"
                    ),
                    ids,
                )
            ).all()
        assert {"failed_at", "delivery_ref", "last_error_summary"}.isdisjoint(columns)
        assert {row.id: row.attempt_count for row in attempts} == {
            ids["delivered"]: 3,
            ids["pending"]: 2,
            ids["dispatch"]: 999,
        }
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")


@pytest.mark.parametrize(
    "event_type",
    ["interactive_action", "notification_delivered", "notification_failed"],
)
@pytest.mark.asyncio
async def test_delivery_migration_refuses_downgrade_with_a8_event_atomically(
    migration_database_url: str,
    event_type: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    user_id, event_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'A8', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"a8-event-{uuid.uuid4().hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO integration_events "
                    "(id, request_hash, event_type, status, actor_user_id, response, completed_at) "
                    "VALUES (:id, :hash, :event_type, 'completed', :user_id, "
                    "'{}'::jsonb, now())"
                ),
                {
                    "id": event_id,
                    "hash": uuid.uuid4().hex + uuid.uuid4().hex,
                    "event_type": event_type,
                    "user_id": user_id,
                },
            )
    finally:
        await engine.dispose()

    with pytest.raises(Exception, match="cannot downgrade while A8 Mattermost evidence exists"):
        await asyncio.to_thread(command.downgrade, config, "0005_mattermost")

    engine = create_async_engine(migration_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            assert (
                await connection.scalar(
                    text("SELECT event_type FROM integration_events WHERE id = :id"),
                    {"id": event_id},
                )
                == event_type
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_downgrade_waits_for_inflight_a8_event_then_refuses(
    migration_database_url: str,
) -> None:
    config = _config(migration_database_url)
    await _reset_schema(migration_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(migration_database_url)
    user_id = uuid.uuid4()
    writer = None
    writer_transaction = None
    downgrade_task = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, display_name, role, password_hash, is_active) "
                    "VALUES (:id, :username, 'A8 race', 'teacher', 'hash', true)"
                ),
                {"id": user_id, "username": f"a8-race-{uuid.uuid4().hex}"},
            )

        writer = await engine.connect()
        writer_transaction = await writer.begin()
        event_id = uuid.uuid4()
        await writer.execute(
            text(
                "INSERT INTO integration_events "
                "(id, request_hash, event_type, status, actor_user_id, response, completed_at) "
                "VALUES (:id, :hash, 'interactive_action', 'completed', :user_id, "
                "'{}'::jsonb, now())"
            ),
            {
                "id": event_id,
                "hash": uuid.uuid4().hex + uuid.uuid4().hex,
                "user_id": user_id,
            },
        )
        downgrade_task = asyncio.create_task(
            asyncio.to_thread(command.downgrade, config, "0005_mattermost")
        )

        observed_lock_wait = False
        async with engine.connect() as observer:
            for _ in range(100):
                observed_lock_wait = bool(
                    await observer.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_locks AS locks "
                            "JOIN pg_class AS relations ON relations.oid = locks.relation "
                            "WHERE NOT locks.granted "
                            "AND locks.mode = 'AccessExclusiveLock' "
                            "AND relations.relname IN "
                            "('users', 'evaluation_outbox', 'integration_events'))"
                        )
                    )
                )
                if observed_lock_wait:
                    break
                await asyncio.sleep(0.02)
        assert observed_lock_wait

        await writer_transaction.commit()
        with pytest.raises(Exception, match="cannot downgrade while A8 Mattermost evidence exists"):
            await asyncio.wait_for(downgrade_task, timeout=3)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_account_creation_events"
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM integration_events WHERE id = :id"),
                    {"id": event_id},
                )
                == 1
            )
    finally:
        if writer_transaction is not None and writer_transaction.is_active:
            await writer_transaction.rollback()
        if writer is not None:
            await writer.close()
        if downgrade_task is not None and not downgrade_task.done():
            await asyncio.gather(downgrade_task, return_exceptions=True)
        await engine.dispose()
