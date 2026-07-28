import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from celery import states
from celery.exceptions import Reject
from celery.result import EagerResult
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.db.types import Role
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationInProgress, EvaluationService
from app.evaluations.types import JobReason, JobStatus
from app.users.model import User
from tests.evaluations.helpers import (
    database_credentials_are_hidden,
    render_test_database_url,
)
from tests.evaluations.test_jobs import _seed_subject


def _noop_notification(job_id: uuid.UUID, report_id: uuid.UUID) -> None:
    del job_id, report_id


def test_database_credentials_are_hidden_detects_password_and_complete_url() -> None:
    password = "synthetic-" + "credential:/@"
    url = URL.create(
        "postgresql+asyncpg",
        username="worker",
        password=password,
        host="database.invalid",
        database="grader_test",
    )

    for leaked_message in (
        "connection failed: password=" + password,
        "connection failed: " + render_test_database_url(url),
    ):
        credentials_are_hidden = database_credentials_are_hidden(leaked_message, url)
        assert not credentials_are_hidden

    credentials_are_hidden = database_credentials_are_hidden(
        "evaluation recovery required",
        url,
    )
    assert credentials_are_hidden


@pytest.mark.parametrize(
    "failure",
    [
        OSError("postgresql://worker:DB-SECRET@database/grader"),
        OperationalError(
            "SELECT DB-STATEMENT-SECRET",
            {},
            OSError("postgresql://worker:DB-SECRET@database/grader"),
        ),
    ],
)
def test_worker_boundary_rejects_recoverable_infrastructure_failure_without_leaking(
    monkeypatch,
    failure: BaseException,
) -> None:
    import app.evaluations.worker as worker_module

    async def unavailable(job_id: uuid.UUID, *, redelivered: bool) -> str:
        del job_id, redelivered
        raise failure

    monkeypatch.setattr(worker_module, "_run_evaluation", unavailable)
    with pytest.raises(Reject) as captured:
        worker_module.run_evaluation.run(str(uuid.uuid4()))

    assert captured.value.requeue is True
    assert captured.value.reason == "evaluation recovery required"
    assert "SECRET" not in str(captured.value)


@pytest.mark.asyncio
async def test_worker_boundary_rejects_real_terminated_postgres_connection(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    database_url = render_test_database_url(postgres_session.bind.url)
    victim_engine = create_async_engine(database_url, pool_pre_ping=True)
    killer_engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with victim_engine.connect() as victim:
            victim_pid = await victim.scalar(text("SELECT pg_backend_pid()"))
            async with killer_engine.begin() as killer:
                assert await killer.scalar(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": victim_pid},
                )
            with pytest.raises(DBAPIError) as database_failure:
                await victim.scalar(text("SELECT 1"))
    finally:
        await victim_engine.dispose()
        await killer_engine.dispose()

    assert database_failure.value.connection_invalidated is True

    async def unavailable(job_id: uuid.UUID, *, redelivered: bool) -> str:
        del job_id, redelivered
        raise database_failure.value

    monkeypatch.setattr(worker_module, "_run_evaluation", unavailable)
    with pytest.raises(Reject) as captured:
        await asyncio.to_thread(worker_module.run_evaluation.run, str(uuid.uuid4()))

    assert captured.value.reason == "evaluation recovery required"
    assert captured.value.requeue is True
    credentials_are_hidden = database_credentials_are_hidden(
        str(captured.value),
        postgres_session.bind.url,
    )
    assert credentials_are_hidden


def test_worker_eager_redelivery_rejects_invalidated_dbapi_error_without_leaking(
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    failure = DBAPIError(
        "SELECT DBAPI-STATEMENT-SECRET",
        {},
        RuntimeError("postgresql://worker:DBAPI-SECRET@database/grader"),
        connection_invalidated=True,
    )

    async def unavailable(job_id: uuid.UUID, *, redelivered: bool) -> str:
        del job_id, redelivered
        raise failure

    monkeypatch.setattr(worker_module, "_run_evaluation", unavailable)
    worker_module.celery_app.conf.task_always_eager = True

    first = worker_module.run_evaluation.apply(args=[str(uuid.uuid4())], throw=True)
    redelivered = worker_module.run_evaluation.apply(
        args=[str(uuid.uuid4())],
        throw=True,
        headers={"redelivered": True},
    )

    for rejected in (first, redelivered):
        assert rejected.state == states.REJECTED
        assert isinstance(rejected.result, Reject)
        assert rejected.result.reason == "evaluation recovery required"
        assert rejected.result.requeue is True
        assert "SECRET" not in str(rejected.result)


def test_worker_eager_redelivery_rejects_repeated_outage_without_leaking(
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    async def unavailable(job_id: uuid.UUID, *, redelivered: bool) -> str:
        del job_id, redelivered
        raise OSError("postgresql://worker:REDELIVERY-SECRET@database/grader")

    monkeypatch.setattr(worker_module, "_run_evaluation", unavailable)
    worker_module.celery_app.conf.task_always_eager = True

    first = worker_module.run_evaluation.apply(args=[str(uuid.uuid4())], throw=True)
    second = worker_module.run_evaluation.apply(
        args=[str(uuid.uuid4())],
        throw=True,
        headers={"redelivered": True},
    )

    for rejected in (first, second):
        assert rejected.state == states.REJECTED
        assert isinstance(rejected.result, Reject)
        assert rejected.result.requeue is True
        assert rejected.result.reason == "evaluation recovery required"
        assert "SECRET" not in str(rejected.result)


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("teacher permission denied"),
        TypeError("programming defect"),
        IntegrityError(
            "INSERT",
            {},
            RuntimeError("constraint defect"),
            connection_invalidated=True,
        ),
        ProgrammingError(
            "SELECT broken",
            {},
            RuntimeError("query defect"),
            connection_invalidated=True,
        ),
    ],
)
def test_worker_boundary_does_not_requeue_business_or_programming_failures(
    monkeypatch,
    failure: Exception,
) -> None:
    import app.evaluations.worker as worker_module

    async def fail(job_id: uuid.UUID, *, redelivered: bool) -> str:
        del job_id, redelivered
        raise failure

    monkeypatch.setattr(worker_module, "_run_evaluation", fail)
    with pytest.raises(type(failure)) as captured:
        worker_module.run_evaluation.run(str(uuid.uuid4()))
    assert not isinstance(captured.value, Reject)


@pytest.mark.asyncio
async def test_live_execution_lock_fences_redelivered_provider_call(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class SlowProvider(MockEvaluationProvider):
        calls = 0

        async def evaluate(self, request):
            self.calls += 1
            provider_started.set()
            await release_provider.wait()
            return await super().evaluate(request)

    class RedeliveredProvider(MockEvaluationProvider):
        calls = 0

        async def evaluate(self, request):
            self.calls += 1
            return await super().evaluate(request)

    slow_provider = SlowProvider()
    redelivered_provider = RedeliveredProvider()
    first = EvaluationService.for_persisted_job(
        postgres_session,
        EvaluationEngine(slow_provider),
        job_id=job.id,
        requested_by=teacher.id,
        redelivered=False,
        notification=_noop_notification,
    )
    duplicate = EvaluationService.for_persisted_job(
        postgres_session,
        EvaluationEngine(redelivered_provider),
        job_id=job.id,
        requested_by=teacher.id,
        redelivered=True,
        notification=_noop_notification,
    )
    first_task = asyncio.create_task(first.execute_job(job.id))
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    try:
        with pytest.raises(EvaluationInProgress, match="already running"):
            await duplicate.execute_job(job.id)
        assert redelivered_provider.calls == 0
    finally:
        release_provider.set()
        await asyncio.gather(first_task, return_exceptions=True)

    await postgres_session.rollback()
    assert slow_provider.calls == 1
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_database_session_loss_releases_fence_for_immediate_redelivery(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(
            status=JobStatus.RUNNING,
            started_at=datetime.now(UTC),
            execution_token=uuid.uuid4(),
            execution_generation=1,
        )
    )
    await postgres_session.commit()
    owner_connection = await postgres_session.bind.connect()
    await owner_connection.execute(
        text("SELECT pg_advisory_lock(hashtextextended(:job_id, 0))"),
        {"job_id": str(job.id)},
    )
    await owner_connection.commit()

    class CountingProvider(MockEvaluationProvider):
        calls = 0

        async def evaluate(self, request):
            self.calls += 1
            return await super().evaluate(request)

    provider = CountingProvider()
    redelivery = EvaluationService.for_persisted_job(
        postgres_session,
        EvaluationEngine(provider),
        job_id=job.id,
        requested_by=teacher.id,
        redelivered=True,
        notification=_noop_notification,
    )
    try:
        with pytest.raises(EvaluationInProgress, match="already running"):
            await redelivery.execute_job(job.id)
        assert provider.calls == 0
    finally:
        # Dropping the physical DB session (not merely returning it to the pool)
        # models worker-process loss and releases its session advisory lock.
        await owner_connection.invalidate()
        await owner_connection.close()

    report = await redelivery.execute_job(job.id)
    assert report.job_id == job.id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_stale_execution_token_cannot_finalize_or_insert_report(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class SlowProvider(MockEvaluationProvider):
        async def evaluate(self, request):
            provider_started.set()
            await release_provider.wait()
            return await super().evaluate(request)

    execution = EvaluationService.for_persisted_job(
        postgres_session,
        EvaluationEngine(SlowProvider()),
        job_id=job.id,
        requested_by=teacher.id,
        redelivered=False,
        notification=_noop_notification,
    )
    execution_task = asyncio.create_task(execution.execute_job(job.id))
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    replacement_token = uuid.uuid4()
    async with postgres_session.bind.begin() as connection:
        await connection.execute(
            update(EvaluationJob)
            .where(EvaluationJob.id == job.id)
            .values(
                execution_token=replacement_token,
                execution_generation=EvaluationJob.execution_generation + 1,
            )
        )
    release_provider.set()
    with pytest.raises(EvaluationInProgress, match="no longer running"):
        await execution_task

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.RUNNING
    assert persisted.execution_token == replacement_token
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_worker_executes_persisted_job_after_requester_role_changes(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations.worker import celery_app, run_evaluation

    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    await postgres_session.execute(
        update(User).where(User.id == teacher.id).values(role=Role.STUDENT, is_active=False)
    )
    await postgres_session.commit()

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_PROVIDER", "mock")
    get_settings.cache_clear()
    celery_app.conf.task_always_eager = True
    result = await asyncio.to_thread(
        lambda: run_evaluation.apply(args=[str(job.id)], throw=True).get()
    )
    assert uuid.UUID(result)

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.SUCCEEDED
    assert await postgres_session.scalar(
        select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
    )


@pytest.mark.asyncio
async def test_only_worker_lost_redelivery_immediately_reclaims_fresh_running_job(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations.worker import celery_app, run_evaluation

    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(
            status=JobStatus.RUNNING,
            started_at=datetime.now(UTC),
            execution_token=uuid.uuid4(),
            execution_generation=1,
        )
    )
    await postgres_session.commit()

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_PROVIDER", "mock")
    get_settings.cache_clear()
    celery_app.conf.task_always_eager = True

    ordinary = await asyncio.to_thread(
        lambda: run_evaluation.apply(args=[str(job.id)], throw=True).get()
    )
    assert uuid.UUID(ordinary) == job.id
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.RUNNING
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )

    redelivered = await asyncio.to_thread(
        lambda: run_evaluation.apply(
            args=[str(job.id)],
            throw=True,
            headers={"redelivered": True},
        ).get()
    )
    assert uuid.UUID(redelivered)
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_worker_compensates_system_exit_without_leaking_message(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)

    class FatalProvider(MockEvaluationProvider):
        async def evaluate(self, request):
            raise SystemExit("SYSTEM-EXIT-SECRET")

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    get_settings.cache_clear()
    monkeypatch.setattr(
        worker_module,
        "create_evaluation_provider",
        lambda settings: FatalProvider(),
    )
    with pytest.raises(RuntimeError) as captured:
        await worker_module._run_evaluation(job.id, redelivered=False)
    assert "SYSTEM-EXIT-SECRET" not in str(captured.value)

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.FAILED
    assert persisted.error_code == "internal_error"


@pytest.mark.asyncio
async def test_fatal_compensation_retries_one_database_failure_before_terminal_state(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)

    class FatalProvider(MockEvaluationProvider):
        async def evaluate(self, request):
            raise SystemExit("FATAL-PROVIDER-SECRET")

    original_compensation = EvaluationService.fail_worker_job_internal
    attempts = 0

    async def flaky_compensation(service: EvaluationService) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("postgresql://DB-CREDENTIAL-SECRET")
        await original_compensation(service)

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    get_settings.cache_clear()
    monkeypatch.setattr(EvaluationService, "fail_worker_job_internal", flaky_compensation)
    monkeypatch.setattr(
        worker_module,
        "create_evaluation_provider",
        lambda settings: FatalProvider(),
    )

    with pytest.raises(RuntimeError, match="evaluation failed") as captured:
        await worker_module._run_evaluation(job.id, redelivered=False)
    assert attempts == 2
    assert "SECRET" not in str(captured.value)
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.FAILED
    assert persisted.error_code == "internal_error"


@pytest.mark.asyncio
async def test_persistent_fatal_compensation_failure_requeues_then_recovers(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)

    class FatalProvider(MockEvaluationProvider):
        async def evaluate(self, request):
            raise MemoryError("FATAL-MEMORY-SECRET")

    original_compensation = EvaluationService.fail_worker_job_internal

    async def unavailable_compensation(service: EvaluationService) -> None:
        del service
        raise ConnectionError("postgresql://DB-CREDENTIAL-SECRET")

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    get_settings.cache_clear()
    monkeypatch.setattr(EvaluationService, "fail_worker_job_internal", unavailable_compensation)
    monkeypatch.setattr(
        worker_module,
        "create_evaluation_provider",
        lambda settings: FatalProvider(),
    )

    rejected = await asyncio.to_thread(
        lambda: worker_module.run_evaluation.apply(
            args=[str(job.id)],
            throw=True,
        )
    )
    assert rejected.state == states.REJECTED, (
        rejected.state,
        type(rejected.result).__name__,
        repr(rejected.result),
    )
    assert isinstance(rejected.result, Reject)
    assert rejected.result.requeue is True
    assert "SECRET" not in str(rejected.result)
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.RUNNING

    monkeypatch.setattr(EvaluationService, "fail_worker_job_internal", original_compensation)
    monkeypatch.setattr(
        worker_module,
        "create_evaluation_provider",
        lambda settings: MockEvaluationProvider(),
    )
    recovered = await asyncio.to_thread(
        lambda: worker_module.run_evaluation.apply(
            args=[str(job.id)],
            throw=True,
            headers={"redelivered": True},
        ).get()
    )
    assert uuid.UUID(recovered)
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.SUCCEEDED


def test_worker_delivery_tasks_and_queues_are_registered() -> None:
    from app.evaluations.worker import (
        celery_app,
        deliver_evaluation_notification,
        redrive_evaluation_delivery,
        run_evaluation,
    )

    assert run_evaluation.acks_late is True
    assert run_evaluation.reject_on_worker_lost is True
    assert deliver_evaluation_notification.name in celery_app.tasks
    assert redrive_evaluation_delivery.name in celery_app.tasks
    assert celery_app.conf.task_routes[run_evaluation.name]["queue"] == "evaluations"
    assert (
        celery_app.conf.task_routes[deliver_evaluation_notification.name]["queue"]
        == "notifications"
    )
    assert celery_app.conf.beat_schedule["redrive-evaluation-delivery"]["task"] == (
        redrive_evaluation_delivery.name
    )


@pytest.mark.asyncio
async def test_enqueue_checks_failed_eager_result_without_leaking_exception(
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    failed = EagerResult(
        str(uuid.uuid4()),
        RuntimeError("EAGER-RESULT-SECRET"),
        states.FAILURE,
    )
    monkeypatch.setattr(worker_module.run_evaluation, "apply_async", lambda *args, **kwargs: failed)
    with pytest.raises(RuntimeError, match="eager task failed") as captured:
        await worker_module.enqueue_evaluation_task(uuid.uuid4())
    assert "EAGER-RESULT-SECRET" not in str(captured.value)


@pytest.mark.asyncio
async def test_worker_provider_close_failure_cannot_override_success_and_engine_disposes(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.worker as worker_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    disposed = 0
    original_dispose = AsyncEngine.dispose

    async def tracked_dispose(engine, *args, **kwargs):
        nonlocal disposed
        disposed += 1
        return await original_dispose(engine, *args, **kwargs)

    class CloseFailureProvider(MockEvaluationProvider):
        async def aclose(self) -> None:
            raise RuntimeError("CLOSE-SECRET")

    monkeypatch.setenv("DATABASE_URL", render_test_database_url(postgres_session.bind.url))
    get_settings.cache_clear()
    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    monkeypatch.setattr(
        worker_module,
        "create_evaluation_provider",
        lambda settings: CloseFailureProvider(),
    )
    result = await worker_module._run_evaluation(job.id, redelivered=False)
    assert uuid.UUID(result)
    # A8 eager mode also executes and disposes the actual notification-delivery engine.
    assert disposed == 2
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job.id)).status is JobStatus.SUCCEEDED
