import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.dependencies import get_current_user
from app.core.config import get_settings
from app.db.session import get_session
from app.db.types import Role, SubmissionStatus
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService
from app.evaluations.types import JobReason, JobStatus
from app.main import create_app
from app.users.model import User
from tests.evaluations.helpers import render_test_database_url
from tests.evaluations.test_jobs import _seed_subject


@asynccontextmanager
async def api_client(current_user: User, postgres_session) -> AsyncIterator[AsyncClient]:
    async def override_current_user() -> User:
        return current_user

    async def override_session():
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_teacher_enqueues_single_and_reads_job_without_internal_key(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    async with api_client(teacher, postgres_session) as client:
        response = await client.post(f"/api/v1/submissions/{submission.id}/evaluations")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["provider"] == "mock"
        assert "idempotency_key" not in body

        fetched = await client.get(f"/api/v1/evaluation-jobs/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body


@pytest.mark.asyncio
async def test_evaluation_post_accepts_only_empty_or_explicit_initial_body(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    path = f"/api/v1/submissions/{submission.id}/evaluations"
    async with api_client(teacher, postgres_session) as client:
        empty = await client.post(path, json={})
        explicit = await client.post(path, json={"reason": "initial"})
        unknown = await client.post(path, json={"unexpected": True})
        provider_retry = await client.post(path, json={"reason": "provider_retry"})
        manual = await client.post(path, json={"reason": "manual_retry"})
        huge = await client.post(
            path,
            content=b'{"unexpected":"' + (b"x" * 8192) + b'"}',
            headers={"content-type": "application/json"},
        )
    assert empty.status_code == explicit.status_code == provider_retry.status_code == 202
    assert empty.json()["id"] == explicit.json()["id"]
    assert provider_retry.json()["id"] != explicit.json()["id"]
    assert provider_retry.json()["reason"] == "provider_retry"
    assert unknown.status_code == manual.status_code == 422
    assert huge.status_code == 413
    assert huge.json() == {"detail": "request body is too large"}


def test_assignment_and_submission_evaluation_bodies_have_separate_openapi_contracts() -> None:
    document = create_app().openapi()
    schemas = document["components"]["schemas"]
    assignment_body = schemas["AssignmentEvaluationCreate"]
    submission_body = schemas["SubmissionEvaluationCreate"]

    assert assignment_body["additionalProperties"] is False
    assert assignment_body["properties"]["reason"]["const"] == "initial"
    assert submission_body["additionalProperties"] is False
    assert submission_body["properties"]["reason"]["enum"] == ["initial", "provider_retry"]

    for path, schema_name in (
        ("/api/v1/assignments/{assignment_id}/evaluations", "AssignmentEvaluationCreate"),
        ("/api/v1/submissions/{submission_id}/evaluations", "SubmissionEvaluationCreate"),
    ):
        body_schema = document["paths"][path]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert body_schema["anyOf"] == [
            {"$ref": f"#/components/schemas/{schema_name}"},
            {"type": "null"},
        ]


@pytest.mark.asyncio
async def test_provider_retry_after_failed_initial_is_distinct_idempotent_and_has_one_outbox(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    initial = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == initial.id)
        .values(
            status=JobStatus.FAILED,
            attempt_count=1,
            error_code="timeout",
            error_message="evaluation provider timed out",
            finished_at=datetime.now(UTC),
        )
    )
    await postgres_session.commit()

    path = f"/api/v1/submissions/{submission.id}/evaluations"
    async with api_client(teacher, postgres_session) as client:
        first = await client.post(path, json={"reason": "provider_retry"})
        duplicate = await client.post(path, json={"reason": "provider_retry"})

    assert first.status_code == duplicate.status_code == 202
    assert first.json() == duplicate.json()
    assert first.json()["id"] != str(initial.id)
    assert first.json()["reason"] == "provider_retry"
    assert first.json()["status"] == "queued"
    assert (
        await postgres_session.scalar(
            select(func.count(EvaluationOutbox.id)).where(
                EvaluationOutbox.kind == "dispatch",
                EvaluationOutbox.job_id == uuid.UUID(first.json()["id"]),
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_provider_retry_rejects_non_teacher_withdrawn_invalid_and_bad_bodies(
    postgres_session,
) -> None:
    teacher, student, _, submission = await _seed_subject(postgres_session)
    path = f"/api/v1/submissions/{submission.id}/evaluations"
    async with api_client(student, postgres_session) as client:
        forbidden = await client.post(path, json={"reason": "provider_retry"})
    assert forbidden.status_code == 403

    submission.status = SubmissionStatus.WITHDRAWN
    await postgres_session.commit()
    async with api_client(teacher, postgres_session) as client:
        withdrawn = await client.post(path, json={"reason": "provider_retry"})
        invalid_uuid = await client.post(
            "/api/v1/submissions/not-a-uuid/evaluations",
            json={"reason": "provider_retry"},
        )
        invalid_reason = await client.post(path, json={"reason": "manual_retry"})
        extra = await client.post(path, json={"reason": "provider_retry", "raw": "secret"})
        assignment_retry = await client.post(
            f"/api/v1/assignments/{uuid.uuid4()}/evaluations",
            json={"reason": "provider_retry"},
        )
    assert withdrawn.status_code == 404
    assert invalid_uuid.status_code == invalid_reason.status_code == extra.status_code == 422
    assert assignment_retry.status_code == 422


@pytest.mark.asyncio
async def test_failed_provider_retry_starts_one_new_generation_and_deduplicates_it(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    path = f"/api/v1/submissions/{submission.id}/evaluations"
    async with api_client(teacher, postgres_session) as client:
        created = await client.post(path, json={"reason": "provider_retry"})
    retry_id = uuid.UUID(created.json()["id"])
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == retry_id)
        .values(
            status=JobStatus.FAILED,
            attempt_count=1,
            error_code="network",
            error_message="evaluation provider network unavailable",
            finished_at=datetime.now(UTC),
        )
    )
    await postgres_session.commit()

    async with api_client(teacher, postgres_session) as client:
        repeated = await client.post(path, json={"reason": "provider_retry"})
        duplicate = await client.post(path, json={"reason": "provider_retry"})
    assert repeated.status_code == duplicate.status_code == 202
    assert repeated.json() == duplicate.json()
    assert repeated.json()["id"] != str(retry_id)
    assert repeated.json()["status"] == "queued"
    assert (
        await postgres_session.scalar(
            select(func.count(EvaluationJob.id)).where(
                EvaluationJob.submission_id == submission.id,
                EvaluationJob.reason == JobReason.PROVIDER_RETRY,
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_cancelled_provider_retry_starts_a_new_generation(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    path = f"/api/v1/submissions/{submission.id}/evaluations"
    async with api_client(teacher, postgres_session) as client:
        created = await client.post(path, json={"reason": "provider_retry"})
    cancelled_id = uuid.UUID(created.json()["id"])
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == cancelled_id)
        .values(status=JobStatus.CANCELLED, finished_at=datetime.now(UTC))
    )
    await postgres_session.commit()

    async with api_client(teacher, postgres_session) as client:
        retried = await client.post(path, json={"reason": "provider_retry"})

    assert retried.status_code == 202
    assert retried.json()["id"] != str(cancelled_id)
    assert retried.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_eager_rest_dispatch_runs_off_event_loop_and_finishes_job(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations.worker import celery_app

    teacher, _, _, submission = await _seed_subject(postgres_session)
    monkeypatch.setenv(
        "DATABASE_URL",
        render_test_database_url(postgres_session.bind.url),
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_PROVIDER", "mock")
    monkeypatch.setenv("EVALUATION_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    get_settings.cache_clear()
    celery_app.conf.task_always_eager = True

    async with api_client(teacher, postgres_session) as client:
        response = await client.post(f"/api/v1/submissions/{submission.id}/evaluations")
    assert response.status_code == 202
    job_id = uuid.UUID(response.json()["id"])
    await postgres_session.rollback()
    job = await postgres_session.get(EvaluationJob, job_id)
    report_id = await postgres_session.scalar(
        select(EvaluationReport.id).where(EvaluationReport.job_id == job_id)
    )
    assert job.status is JobStatus.SUCCEEDED
    assert report_id is not None


@pytest.mark.asyncio
async def test_sync_testclient_eager_post_preserves_report_when_bot_is_unconfigured(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations.worker import celery_app

    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_url = render_test_database_url(postgres_session.bind.url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_PROVIDER", "mock")
    monkeypatch.setenv("EVALUATION_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    get_settings.cache_clear()
    celery_app.conf.task_always_eager = True

    async def override_current_user() -> User:
        return teacher

    async def override_session():
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                yield session
        finally:
            await engine.dispose()

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session

    def post() -> tuple[int, dict[str, object]]:
        with TestClient(application) as client:
            response = client.post(f"/api/v1/submissions/{submission.id}/evaluations")
            return response.status_code, response.json()

    status_code, body = await asyncio.to_thread(post)
    assert status_code == 202
    job_id = uuid.UUID(str(body["id"]))
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationJob, job_id)).status is JobStatus.SUCCEEDED
    report_id = await postgres_session.scalar(
        select(EvaluationReport.id).where(EvaluationReport.job_id == job_id)
    )
    notification = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.job_id == job_id,
        )
    )
    assert report_id is not None
    assert notification is not None
    assert notification.delivered_at is None
    assert notification.failed_at is not None
    assert notification.last_error_type == "configuration"


@pytest.mark.asyncio
async def test_provider_close_failure_does_not_override_enqueue_response(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.router as router_module

    teacher, _, _, submission = await _seed_subject(postgres_session)

    class CloseFailureProvider(MockEvaluationProvider):
        async def aclose(self) -> None:
            raise RuntimeError("provider-close-secret")

    monkeypatch.setattr(
        router_module,
        "create_evaluation_provider",
        lambda settings: CloseFailureProvider(),
    )
    async with api_client(teacher, postgres_session) as client:
        response = await client.post(f"/api/v1/submissions/{submission.id}/evaluations")
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_evaluation_post_and_job_get_are_teacher_only(postgres_session) -> None:
    _, student, _, submission = await _seed_subject(postgres_session)
    for actor in (
        student,
        User(
            id=uuid.uuid4(),
            username="admin",
            display_name="Admin",
            role=Role.ADMIN,
            password_hash="x",
            is_active=True,
        ),
    ):
        async with api_client(actor, postgres_session) as client:
            assert (
                await client.post(f"/api/v1/submissions/{submission.id}/evaluations")
            ).status_code == 403
            assert (await client.get(f"/api/v1/evaluation-jobs/{uuid.uuid4()}")).status_code == 403


@pytest.mark.asyncio
async def test_reports_hide_raw_and_students_cannot_enumerate_others(postgres_session) -> None:
    teacher, owner, _, submission = await _seed_subject(postgres_session)
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    outsider = User(
        username=f"outsider-{uuid.uuid4()}",
        display_name="Outsider",
        role=Role.STUDENT,
        password_hash="x",
        is_active=True,
    )
    postgres_session.add(outsider)
    await postgres_session.commit()

    async with api_client(owner, postgres_session) as client:
        response = await client.get(f"/api/v1/submissions/{submission.id}/reports")
        assert response.status_code == 200
        assert response.json()[0]["id"] == str(report.id)
        assert "raw_model_output" not in response.json()[0]
        assert "audit" not in response.json()[0]

    async with api_client(outsider, postgres_session) as client:
        existing = await client.get(f"/api/v1/submissions/{submission.id}/reports")
        missing = await client.get(f"/api/v1/submissions/{uuid.uuid4()}/reports")
        assert existing.status_code == missing.status_code == 404
        assert existing.json() == missing.json() == {"detail": "submission not found"}


@pytest.mark.asyncio
async def test_admin_cannot_read_submission_reports(postgres_session) -> None:
    _, _, _, submission = await _seed_subject(postgres_session)
    admin = User(
        username=f"admin-{uuid.uuid4()}",
        display_name="Admin",
        role=Role.ADMIN,
        password_hash="x",
        is_active=True,
    )
    postgres_session.add(admin)
    await postgres_session.commit()

    async with api_client(admin, postgres_session) as client:
        response = await client.get(f"/api/v1/submissions/{submission.id}/reports")

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient role"}
