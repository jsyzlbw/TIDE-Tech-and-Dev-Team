from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.assignments.model import Assignment
from app.core.config import Settings
from app.db.session import get_session
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
)
from app.evaluations.model import EvaluationJob, EvaluationOutbox
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.integrations.mattermost.schemas import MattermostRequest
from app.integrations.mattermost.security import request_hash
from app.integrations.mattermost.service import handle_mattermost_command
from app.main import create_app
from app.submissions.model import Submission
from app.users.model import User

PATH = "/api/v1/integrations/mattermost/commands"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _settings() -> Settings:
    return Settings(
        app_env="test",
        jwt_secret="test-only",
        mattermost_command_token=SecretStr("slash-secret"),
        mattermost_demo_setup_key=None,
    )


def _request(
    text: str,
    trigger: str,
    *,
    user_id: str = "mm-teacher",
    user_name: str = "teacher-mm",
) -> MattermostRequest:
    return MattermostRequest(
        team_id="team-1",
        team_domain="course",
        channel_id="channel-1",
        channel_name="homework",
        user_id=user_id,
        user_name=user_name,
        command="/hw",
        text=text,
        trigger_id=trigger,
        response_url="https://mattermost.invalid/hooks/response",
    )


def _body(request: MattermostRequest) -> bytes:
    return urlencode({"token": "slash-secret", **request.model_dump()}).encode()


async def _teacher(session) -> User:
    teacher = User(
        username=f"idempotency-teacher-{uuid.uuid4().hex}",
        display_name="Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    session.add(teacher)
    await session.flush()
    session.add(
        MattermostIdentity(
            user_id=teacher.id,
            mattermost_user_id="mm-teacher",
            mattermost_username="teacher-mm",
        )
    )
    await session.commit()
    return teacher


async def _student(session) -> User:
    student = User(
        username=f"idempotency-student-{uuid.uuid4().hex}",
        display_name="Student",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    session.add(student)
    await session.flush()
    session.add(
        MattermostIdentity(
            user_id=student.id,
            mattermost_user_id="mm-student",
            mattermost_username="student-mm",
        )
    )
    await session.commit()
    return student


async def _published_assignment(session, teacher: User) -> Assignment:
    now = datetime.now(SHANGHAI)
    assignment = Assignment(
        code="HW-4321",
        title="Replay fixture",
        question="Explain idempotency",
        notes="",
        rubric={},
        due_at=now + timedelta(days=2),
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id="channel-1",
        created_by=teacher.id,
        published_at=now,
    )
    session.add(assignment)
    await session.commit()
    return assignment


async def _submitted_answer(
    session,
    assignment: Assignment,
    student: User,
) -> Submission:
    submission = Submission(
        assignment_id=assignment.id,
        student_id=student.id,
        version=1,
        content_type=SubmissionContentType.TEXT,
        content_text="ready for evaluation",
        content_json=None,
        source=SubmissionSource.MATTERMOST,
    )
    session.add(submission)
    await session.commit()
    return submission


@pytest.mark.asyncio
async def test_request_hash_is_computed_before_identity_lookup(monkeypatch) -> None:
    calls: list[str] = []
    request = _request("help", "ordering-trigger")
    real_request_hash = request_hash

    def observed_hash(value: MattermostRequest) -> str:
        calls.append("hash")
        return real_request_hash(value)

    async def observed_identity(session, mattermost_user_id):
        del session, mattermost_user_id
        calls.append("identity")
        raise RuntimeError("stop after identity lookup")

    monkeypatch.setattr(
        "app.integrations.mattermost.service.request_hash",
        observed_hash,
    )
    monkeypatch.setattr(
        "app.integrations.mattermost.service.resolve_mattermost_actor",
        observed_identity,
    )

    with pytest.raises(RuntimeError, match="stop after identity lookup"):
        await handle_mattermost_command(None, request, _settings())

    assert calls == ["hash", "identity"]


@pytest.mark.parametrize("replay_mode", ["serial", "concurrent"], ids=lambda value: value)
@pytest.mark.parametrize(
    "command_name",
    ["publish", "submit", "evaluate"],
    ids=lambda value: value,
)
@pytest.mark.asyncio
async def test_business_commands_replay_exactly_once_across_independent_connections(
    postgres_session,
    monkeypatch,
    command_name: str,
    replay_mode: str,
) -> None:
    teacher = await _teacher(postgres_session)
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    if command_name == "publish":
        request = _request(
            f'publish --title "Replay" --due "{due}" --question "Explain once"',
            f"{replay_mode}-publish",
        )
    else:
        student = await _student(postgres_session)
        assignment = await _published_assignment(postgres_session, teacher)
        if command_name == "submit":
            request = _request(
                'submit HW-4321 --text "only one version"',
                f"{replay_mode}-submit",
                user_id="mm-student",
                user_name="student-mm",
            )
        else:
            await _submitted_answer(postgres_session, assignment, student)
            request = _request("evaluate HW-4321", f"{replay_mode}-evaluate")

            async def provider_must_not_run(*args, **kwargs):
                del args, kwargs
                raise AssertionError("evaluate command must only enqueue durable work")

            monkeypatch.setattr(
                "app.evaluations.providers.mock.MockEvaluationProvider.evaluate",
                provider_must_not_run,
            )

    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    barrier = asyncio.Barrier(2) if replay_mode == "concurrent" else None

    async def invoke():
        async with factory() as session, session.begin():
            await session.connection()
            if barrier is not None:
                await barrier.wait()
            return await handle_mattermost_command(session, request, _settings())

    if replay_mode == "serial":
        first = await invoke()
        second = await invoke()
    else:
        first, second = await asyncio.wait_for(
            asyncio.gather(invoke(), invoke()),
            timeout=5,
        )

    assert first.model_dump_json() == second.model_dump_json()
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 1
    if command_name == "publish":
        assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 1
    elif command_name == "submit":
        submissions = (
            await postgres_session.scalars(select(Submission).order_by(Submission.version))
        ).all()
        assert [item.version for item in submissions] == [1]
    else:
        assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 1
        assert (
            await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox)) == 1
        )


@pytest.mark.asyncio
async def test_concurrent_publish_replays_exact_response_and_one_business_effect(
    postgres_session,
) -> None:
    await _teacher(postgres_session)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    request = _request(
        f'publish --title "Concurrent" --due "{due}" --question "Explain locks"',
        "same-trigger",
    )

    async def invoke():
        async with factory() as session, session.begin():
            return await handle_mattermost_command(session, request, _settings())

    first, second = await asyncio.gather(invoke(), invoke())
    assert first.model_dump_json() == second.model_dump_json()
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 1

    fresh = request.model_copy(update={"trigger_id": "new-trigger"})
    async with factory() as session, session.begin():
        await handle_mattermost_command(session, fresh, _settings())
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 2
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 2


@pytest.mark.asyncio
async def test_rollback_removes_claim_and_business_then_retry_succeeds(
    postgres_session,
) -> None:
    await _teacher(postgres_session)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    due = (datetime.now(SHANGHAI) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    request = _request(
        f'publish --title "Rollback" --due "{due}" --question "Atomic?"',
        "rollback-trigger",
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        async with factory() as session, session.begin():
            await handle_mattermost_command(session, request, _settings())
            raise RuntimeError("simulated commit failure")

    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 0
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 0

    async with factory() as session, session.begin():
        response = await handle_mattermost_command(session, request, _settings())
    assert "HW-" in response.text
    assert await postgres_session.scalar(select(func.count()).select_from(Assignment)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 1


@pytest.mark.asyncio
async def test_terminal_deterministic_replay_skips_parser(postgres_session, monkeypatch) -> None:
    await _teacher(postgres_session)
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    request = _request("publish --title missing-fields", "parse-trigger")
    async with factory() as session, session.begin():
        first = await handle_mattermost_command(session, request, _settings())

    def fail_parser(text: str):
        del text
        raise AssertionError("terminal replay must not parse")

    monkeypatch.setattr("app.integrations.mattermost.service.parse_command", fail_parser)
    async with factory() as session, session.begin():
        replay = await handle_mattermost_command(session, request, _settings())
    assert first.model_dump_json() == replay.model_dump_json()


@pytest.mark.asyncio
async def test_unique_lock_timeout_returns_retryable_503_without_event(postgres_session) -> None:
    teacher = await _teacher(postgres_session)
    teacher_id = teacher.id
    request = _request("help", "locked-trigger")
    factory = async_sessionmaker(postgres_session.bind, expire_on_commit=False)
    locker = factory()
    await locker.begin()
    locker.add(
        IntegrationEvent(
            request_hash=request_hash(request),
            actor_user_id=teacher_id,
            business_refs={},
        )
    )
    await locker.flush()

    async def override_session():
        async with factory() as session:
            yield session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    started = time.monotonic()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                PATH,
                content=_body(request),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
    finally:
        await locker.rollback()
        await locker.close()

    assert 2 <= time.monotonic() - started < 3
    assert response.status_code == 503
    assert response.json()["response_type"] == "ephemeral"
    assert await postgres_session.scalar(select(func.count()).select_from(IntegrationEvent)) == 0
