from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService
from app.evaluations.types import JobReason, ReviewStatus
from app.main import create_app
from app.reviews.model import ReviewAction
from app.reviews.service import ReviewService
from app.reviews.types import ReviewActionType
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


@asynccontextmanager
async def review_client(current_user: User, postgres_session) -> AsyncIterator[AsyncClient]:
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


def _assert_request_id(response: Response) -> str:
    request_id = response.headers["X-Request-ID"]
    assert str(uuid.UUID(request_id)) == request_id
    assert response.json()["request_id"] == request_id
    return request_id


async def _report(postgres_session, teacher: User, submission_id: uuid.UUID):
    return await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission_id, JobReason.INITIAL)


@pytest.mark.asyncio
async def test_review_endpoints_return_safe_reports_jobs_and_request_ids(
    postgres_session,
) -> None:
    confirm_teacher, confirm_student, _, confirm_submission = await _seed_subject(postgres_session)
    first = await _report(postgres_session, confirm_teacher, confirm_submission.id)
    incoming_request_id = "11111111-1111-4111-8111-111111111111"
    async with review_client(confirm_teacher, postgres_session) as client:
        confirm = await client.post(
            f"/api/v1/reports/{first.id}/confirm",
            json={"comment": "已核验"},
            headers={"X-Request-ID": incoming_request_id},
        )
        duplicate_confirm = await client.post(
            f"/api/v1/reports/{first.id}/confirm",
            json={"comment": "已核验"},
            headers={"X-Request-ID": incoming_request_id},
        )
    assert confirm.status_code == 200
    first_server_request_id = _assert_request_id(confirm)
    second_server_request_id = _assert_request_id(duplicate_confirm)
    assert first_server_request_id != incoming_request_id
    assert second_server_request_id != incoming_request_id
    assert first_server_request_id != second_server_request_id
    assert confirm.json()["review_status"] == "confirmed"
    assert "raw_model_output" not in confirm.json()

    modify_teacher, modify_student, _, modify_submission = await _seed_subject(postgres_session)
    second = await _report(postgres_session, modify_teacher, modify_submission.id)
    async with review_client(modify_teacher, postgres_session) as client:
        modify = await client.patch(
            f"/api/v1/reports/{second.id}",
            json={"score": 94, "comment": "调整"},
        )
    assert modify.status_code == 200
    _assert_request_id(modify)
    assert modify.json()["version"] == second.version + 1
    assert modify.json()["grade"] == "A"
    assert "raw_model_output" not in modify.json()

    async with review_client(modify_teacher, postgres_session) as client:
        teacher_list = await client.get(f"/api/v1/submissions/{modify_submission.id}/reports")
    assert teacher_list.status_code == 200
    assert [item["version"] for item in teacher_list.json()] == [2, 1]
    assert all("raw_model_output" not in item for item in teacher_list.json())

    async with review_client(modify_student, postgres_session) as client:
        owner_list = await client.get(f"/api/v1/submissions/{modify_submission.id}/reports")
    assert owner_list.status_code == 200
    assert [item["id"] for item in owner_list.json()] == [
        modify.json()["id"],
        str(second.id),
    ]

    async with review_client(confirm_student, postgres_session) as client:
        outsider_list = await client.get(f"/api/v1/submissions/{modify_submission.id}/reports")
    assert outsider_list.status_code == 404

    retry_teacher, _, _, retry_submission = await _seed_subject(postgres_session)
    third = await _report(postgres_session, retry_teacher, retry_submission.id)
    async with review_client(retry_teacher, postgres_session) as client:
        reevaluate = await client.post(
            f"/api/v1/reports/{third.id}/reevaluate",
            json={"comment": "重评"},
        )
    assert reevaluate.status_code == 202
    _assert_request_id(reevaluate)
    assert reevaluate.json()["reason"] == "manual_retry"
    assert reevaluate.json()["source_report_id"] == str(third.id)
    assert "idempotency_key" not in reevaluate.json()


@pytest.mark.asyncio
async def test_api_reevaluate_then_confirm_serializes_without_deadlock(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _report(postgres_session, teacher, submission.id)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    source_locked = asyncio.Event()
    release_reevaluate = asyncio.Event()
    original_lock_report = ReviewService._lock_report

    async def pause_reevaluate_after_lock(session, report_id):
        locked = await original_lock_report(session, report_id)
        if asyncio.current_task().get_name() == "api-reevaluate":
            await session.execute(text("SET LOCAL lock_timeout = '3s'"))
            source_locked.set()
            await release_reevaluate.wait()
        return locked

    monkeypatch.setattr(
        ReviewService,
        "_lock_report",
        staticmethod(pause_reevaluate_after_lock),
    )

    async def override_current_user() -> User:
        return teacher

    async def override_session():
        async with factory() as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        reevaluate_task = asyncio.create_task(
            client.post(f"/api/v1/reports/{report.id}/reevaluate", json={}),
            name="api-reevaluate",
        )
        await asyncio.wait_for(source_locked.wait(), timeout=3)
        confirm_task = asyncio.create_task(
            client.post(f"/api/v1/reports/{report.id}/confirm", json={}),
            name="api-confirm",
        )
        await asyncio.sleep(0.2)
        assert not confirm_task.done()
        release_reevaluate.set()
        reevaluate, confirm = await asyncio.wait_for(
            asyncio.gather(reevaluate_task, confirm_task),
            timeout=5,
        )

    assert reevaluate.status_code == 202
    assert confirm.status_code == 409
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
        )
        is ReviewStatus.PROPOSED
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationJob)
            .where(EvaluationJob.source_report_id == report.id)
        )
        == 1
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_review_errors_are_strict_bounded_and_request_correlated(
    postgres_session,
) -> None:
    teacher, student, _, submission = await _seed_subject(postgres_session)
    report = await _report(postgres_session, teacher, submission.id)
    async with review_client(teacher, postgres_session) as client:
        malformed = await client.post("/api/v1/reports/not-a-uuid/confirm", json={})
        missing = await client.post(f"/api/v1/reports/{uuid.uuid4()}/confirm", json={})
        unknown = await client.post(
            f"/api/v1/reports/{report.id}/confirm",
            json={"unknown": True},
        )
        too_many_characters = await client.post(
            f"/api/v1/reports/{report.id}/confirm",
            json={"comment": "x" * 4_001},
        )
        too_many_utf8_bytes = await client.post(
            f"/api/v1/reports/{report.id}/confirm",
            content=('{"comment":"' + "😀" * 2_001 + '"}').encode(),
            headers={"content-type": "application/json"},
        )
        oversized = await client.patch(
            f"/api/v1/reports/{report.id}",
            content=b'{"comment":"' + b"x" * (17 * 1024) + b'"}',
            headers={"content-type": "application/json"},
        )
    assert malformed.status_code == 422
    assert missing.status_code == 404
    assert unknown.status_code == 422
    assert too_many_characters.status_code == 422
    assert too_many_utf8_bytes.status_code == 422
    assert oversized.status_code == 413
    for response in (
        malformed,
        missing,
        unknown,
        too_many_characters,
        too_many_utf8_bytes,
        oversized,
    ):
        _assert_request_id(response)

    async with review_client(student, postgres_session) as client:
        forbidden = await client.post(f"/api/v1/reports/{report.id}/confirm", json={})
    assert forbidden.status_code == 403
    _assert_request_id(forbidden)

    async def override_session():
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        unauthorized = await client.post(f"/api/v1/reports/{report.id}/confirm", json={})
    assert unauthorized.status_code == 401
    _assert_request_id(unauthorized)


@pytest.mark.asyncio
async def test_request_id_header_is_global_but_legacy_error_body_is_unchanged(
    postgres_session,
) -> None:
    teacher, _, _, _ = await _seed_subject(postgres_session)
    incoming_request_id = "22222222-2222-4222-8222-222222222222"
    async with review_client(teacher, postgres_session) as client:
        first = await client.post(
            "/api/v1/submissions/not-a-uuid/evaluations",
            json={},
            headers={"X-Request-ID": incoming_request_id},
        )
        second = await client.post(
            "/api/v1/submissions/not-a-uuid/evaluations",
            json={},
            headers={"X-Request-ID": incoming_request_id},
        )
    assert first.status_code == 422
    first_id = first.headers["X-Request-ID"]
    second_id = second.headers["X-Request-ID"]
    assert str(uuid.UUID(first_id)) == first_id
    assert str(uuid.UUID(second_id)) == second_id
    assert first_id != incoming_request_id
    assert second_id != incoming_request_id
    assert first_id != second_id
    assert "request_id" not in first.json()


def test_openapi_contains_all_review_routes() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/reports/{report_id}/confirm" in paths
    assert "/api/v1/reports/{report_id}" in paths
    assert "/api/v1/reports/{report_id}/reevaluate" in paths


@pytest.mark.asyncio
async def test_unexpected_errors_keep_global_header_and_safe_a6_body(
    postgres_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, _ = await _seed_subject(postgres_session)

    async def explode(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("provider secret")

    monkeypatch.setattr(ReviewService, "confirm", explode)

    async def override_current_user() -> User:
        return teacher

    async def override_session():
        yield postgres_session

    application = create_app()
    application.dependency_overrides[get_current_user] = override_current_user
    application.dependency_overrides[get_session] = override_session

    @application.get("/legacy-crash")
    async def legacy_crash() -> None:
        raise RuntimeError("legacy secret")

    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        incoming_request_id = "33333333-3333-4333-8333-333333333333"
        review_error = await client.post(
            f"/api/v1/reports/{uuid.uuid4()}/confirm",
            json={},
            headers={"X-Request-ID": incoming_request_id},
        )
        legacy_error = await client.get(
            "/legacy-crash",
            headers={"X-Request-ID": incoming_request_id},
        )

    assert review_error.status_code == 500
    review_request_id = _assert_request_id(review_error)
    assert review_request_id != incoming_request_id
    assert review_error.json()["detail"] == "internal server error"
    assert "secret" not in review_error.text
    assert legacy_error.status_code == 500
    assert (
        str(uuid.UUID(legacy_error.headers["X-Request-ID"])) == legacy_error.headers["X-Request-ID"]
    )
    assert legacy_error.headers["X-Request-ID"] != incoming_request_id
    assert legacy_error.headers["X-Request-ID"] != review_request_id
    assert legacy_error.text == "Internal Server Error"
