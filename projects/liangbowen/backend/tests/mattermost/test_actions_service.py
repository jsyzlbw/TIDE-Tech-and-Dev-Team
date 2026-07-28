from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import get_session
from app.db.types import Role
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.outbox import redrive_evaluation_outbox
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService
from app.evaluations.types import JobReason, ReviewStatus
from app.integrations.mattermost.actions import parse_and_verify_action, sign_action
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.integrations.mattermost.service import handle_mattermost_action
from app.main import create_app
from app.reviews.model import ReviewAction
from app.reviews.schemas import ReevaluateRequest
from app.reviews.service import ReviewService
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        mattermost_action_secret=SecretStr("action-secret"),
        web_console_url="http://console.test",
    )


CHANNEL_ID = "chan0000000000000000000001"
POST_ID = "0123456789abcdefghijklmnop"


def _request(
    action: str,
    report_id: uuid.UUID,
    delivery_id: uuid.UUID,
    user_id: str,
    *,
    post_id: str = POST_ID,
):
    signature = sign_action(
        action,
        report_id,
        delivery_id,
        user_id,
        CHANNEL_ID,
        SecretStr("action-secret"),
    )
    body = json.dumps(
        {
            "user_id": user_id,
            "post_id": post_id,
            "channel_id": CHANNEL_ID,
            "team_id": "",
            "context": {
                "action": action,
                "report_id": str(report_id),
                "delivery_id": str(delivery_id),
                "expected_user_id": user_id,
                "expected_channel_id": CHANNEL_ID,
                "signature": signature,
            },
        }
    ).encode()
    return parse_and_verify_action(body, "application/json", SecretStr("action-secret"))


async def _seed_report(postgres_session):
    teacher, _, _, submission = await _seed_subject(postgres_session)
    teacher_id = uuid.UUID(str(teacher.id))
    mattermost_user_id = "teacher00000000000000000001"
    postgres_session.add(
        MattermostIdentity(
            user_id=teacher.id,
            mattermost_user_id=mattermost_user_id,
            mattermost_username="teacher",
        )
    )
    await postgres_session.commit()
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=lambda job_id, report_id: POST_ID,
        job_ids=(report.job_id,),
    )
    await postgres_session.rollback()
    outbox = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.job_id == report.job_id,
        )
    )
    assert outbox is not None and outbox.delivery_ref == POST_ID
    outbox_id = uuid.UUID(str(outbox.id))
    await postgres_session.rollback()
    return teacher_id, report, mattermost_user_id, outbox_id


@pytest.mark.asyncio
async def test_confirm_action_is_atomic_and_exactly_replayed(postgres_session) -> None:
    _, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request("confirm", report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        first = await handle_mattermost_action(postgres_session, request, _settings())
    async with postgres_session.begin():
        replay = await handle_mattermost_action(postgres_session, request, _settings())

    assert replay == first
    assert first["ephemeral_text"] == "已确认评估结果。"
    assert first["update"]["props"] == {"attachments": []}
    await postgres_session.rollback()
    current = await postgres_session.get(EvaluationReport, report.id)
    assert current is not None and current.review_status is ReviewStatus.CONFIRMED
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == 1
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reevaluate_action_only_persists_job_and_dispatch_outbox(postgres_session) -> None:
    _, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request("reevaluate", report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        response = await handle_mattermost_action(postgres_session, request, _settings())

    assert response["ephemeral_text"] == "重新评估已进入队列。"
    await postgres_session.rollback()
    job = await postgres_session.scalar(
        select(EvaluationJob).where(EvaluationJob.source_report_id == report.id)
    )
    assert job is not None
    outbox = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "dispatch",
            EvaluationOutbox.job_id == job.id,
        )
    )
    assert outbox is not None and outbox.delivered_at is None and outbox.attempt_count == 0


@pytest.mark.asyncio
async def test_open_action_validates_current_report_without_review_mutation(
    postgres_session,
) -> None:
    _, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request("openreport", report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        response = await handle_mattermost_action(postgres_session, request, _settings())

    assert response == {
        "ephemeral_text": f"打开报告：http://console.test/teacher/reports/{report.id}"
    }
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_action_has_one_mutation_and_exact_response(
    postgres_session,
) -> None:
    _, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request("confirm", report.id, outbox_id, mattermost_user_id)
    sessions = async_sessionmaker(postgres_session.bind, expire_on_commit=False)

    async def invoke() -> dict[str, object]:
        async with sessions() as session, session.begin():
            return await handle_mattermost_action(session, request, _settings())

    first, second = await asyncio.gather(invoke(), invoke())
    assert first == second
    await postgres_session.rollback()
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == 1
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["post", "delivery", "other_teacher"])
async def test_forged_delivery_envelope_creates_no_action_or_event(
    postgres_session,
    forgery: str,
) -> None:
    from app.db.types import Role
    from app.integrations.mattermost.service import MattermostIdentityRejected

    _, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    delivery_id = outbox_id
    post_id = POST_ID
    actor_id = mattermost_user_id
    if forgery == "post":
        post_id = "otherpost000000000000000001"
    elif forgery == "delivery":
        delivery_id = uuid.uuid4()
    else:
        other = User(
            username=f"other-teacher-{uuid.uuid4()}",
            display_name="Other Teacher",
            role=Role.TEACHER,
            password_hash="hash",
            is_active=True,
        )
        postgres_session.add(other)
        await postgres_session.flush()
        actor_id = "otherteacher000000000000001"
        postgres_session.add(
            MattermostIdentity(
                user_id=other.id,
                mattermost_user_id=actor_id,
                mattermost_username="other",
            )
        )
        await postgres_session.commit()
    request = _request("confirm", report.id, delivery_id, actor_id, post_id=post_id)
    with pytest.raises(MattermostIdentityRejected):
        async with postgres_session.begin():
            await handle_mattermost_action(postgres_session, request, _settings())
    await postgres_session.rollback()
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == 0
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reevaluate", "openreport"])
async def test_action_replay_rechecks_current_report_before_cached_response(
    postgres_session,
    action: str,
) -> None:
    teacher_id, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request(action, report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        first = await handle_mattermost_action(postgres_session, request, _settings())
    current = await postgres_session.get(EvaluationReport, report.id)
    assert current is not None
    job_id = await postgres_session.scalar(
        select(EvaluationJob.id).where(EvaluationJob.source_report_id == report.id)
    )
    await postgres_session.rollback()
    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
        notification=None,
    )
    if job_id is None:
        job = await ReviewService(postgres_session).reevaluate(
            report.id,
            teacher_id,
            ReevaluateRequest(),
            evaluation,
        )
        job_id = job.id
    await evaluation.execute_job(job_id)
    async with postgres_session.begin():
        replay = await handle_mattermost_action(postgres_session, request, _settings())
    assert replay != first
    assert replay["error"]["message"] == "该评估已变更，请打开最新报告。"
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reevaluate", "openreport"])
async def test_action_replay_rechecks_active_teacher_before_cached_response(
    postgres_session,
    action: str,
) -> None:
    from app.integrations.mattermost.service import MattermostIdentityRejected

    teacher_id, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request(action, report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        await handle_mattermost_action(postgres_session, request, _settings())
    await postgres_session.execute(
        update(User).where(User.id == teacher_id).values(is_active=False)
    )
    await postgres_session.commit()

    with pytest.raises(MattermostIdentityRejected):
        async with postgres_session.begin():
            await handle_mattermost_action(postgres_session, request, _settings())
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reevaluate", "openreport"])
@pytest.mark.parametrize("role", [Role.STUDENT, Role.ADMIN])
async def test_bound_active_non_teacher_gets_exact_authorization_error_without_replay(
    postgres_session,
    action: str,
    role: Role,
) -> None:
    teacher_id, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request(action, report.id, outbox_id, mattermost_user_id)
    async with postgres_session.begin():
        old_success = await handle_mattermost_action(postgres_session, request, _settings())
    before_actions = await postgres_session.scalar(select(func.count()).select_from(ReviewAction))
    before_jobs = await postgres_session.scalar(select(func.count()).select_from(EvaluationJob))
    await postgres_session.execute(update(User).where(User.id == teacher_id).values(role=role))
    await postgres_session.commit()

    async with postgres_session.begin():
        first = await handle_mattermost_action(postgres_session, request, _settings())
    async with postgres_session.begin():
        replay = await handle_mattermost_action(postgres_session, request, _settings())

    assert first == replay
    assert first != old_success
    assert first == {
        "error": {"message": "当前账号没有教师审核权限。"},
        "ephemeral_text": "当前账号没有教师审核权限。",
    }
    assert await postgres_session.scalar(select(func.count()).select_from(ReviewAction)) == (
        before_actions
    )
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == (
        before_jobs
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(IntegrationEvent)
            .where(IntegrationEvent.event_type == "interactive_action")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_bound_active_non_teacher_action_is_http_200_json(postgres_session) -> None:
    teacher_id, report, mattermost_user_id, outbox_id = await _seed_report(postgres_session)
    request = _request("confirm", report.id, outbox_id, mattermost_user_id)
    await postgres_session.execute(
        update(User).where(User.id == teacher_id).values(role=Role.STUDENT)
    )
    await postgres_session.commit()

    async def override_session():
        yield postgres_session

    application = create_app(_settings())
    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/integrations/mattermost/actions",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "error": {"message": "当前账号没有教师审核权限。"},
        "ephemeral_text": "当前账号没有教师审核权限。",
    }
