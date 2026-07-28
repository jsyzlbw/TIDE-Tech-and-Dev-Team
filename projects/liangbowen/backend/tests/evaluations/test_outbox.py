import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.audit.model import AuditLog
from app.core.config import Settings
from app.db.types import Role
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.outbox import redrive_evaluation_outbox
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationJobFailed, EvaluationService
from app.evaluations.types import JobReason, JobStatus
from app.integrations.mattermost.client import MattermostPermanentError, MattermostRetryableError
from app.integrations.mattermost.delivery import deliver_report_notification
from app.integrations.mattermost.model import IntegrationEvent, MattermostIdentity
from app.submissions.model import Submission
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


@pytest.mark.asyncio
async def test_dispatch_outbox_survives_broker_failure_and_redrives_once(
    postgres_session,
    caplog,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    attempts: list[uuid.UUID] = []

    async def unavailable(job_id: uuid.UUID) -> None:
        attempts.append(job_id)
        raise ConnectionError("redis://secret@broker unavailable")

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=unavailable,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    await postgres_session.rollback()
    pending = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "dispatch",
            EvaluationOutbox.job_id == job.id,
        )
    )
    assert pending is not None
    assert pending.delivered_at is None
    assert pending.attempt_count == 1
    assert pending.last_error_type == "ConnectionError"
    assert "redis://secret" not in caplog.text

    delivered: list[uuid.UUID] = []

    async def recovered(job_id: uuid.UUID) -> None:
        delivered.append(job_id)

    await postgres_session.execute(
        update(EvaluationOutbox)
        .where(EvaluationOutbox.id == pending.id)
        .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await postgres_session.commit()
    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=recovered,
        notification=None,
        limit=10,
    )
    duplicate = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=recovered,
        notification=None,
        limit=10,
    )
    assert result.delivered == 1
    assert duplicate.delivered == 0
    assert attempts == [job.id]
    assert delivered == [job.id]


@pytest.mark.asyncio
async def test_concurrent_redrivers_skip_locked_and_publish_one_delivery(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    calls: list[uuid.UUID] = []

    async def slow_delivery(job_id: uuid.UUID) -> None:
        calls.append(job_id)
        await asyncio.sleep(0.05)

    results = await asyncio.gather(
        *(
            redrive_evaluation_outbox(
                postgres_session.bind,
                dispatch=slow_delivery,
                notification=None,
                limit=10,
            )
            for _ in range(4)
        )
    )
    assert calls == [job.id]
    assert sum(result.delivered for result in results) == 1


@pytest.mark.asyncio
async def test_slow_delivery_renews_short_claim_lease(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations import outbox as outbox_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    monkeypatch.setattr(outbox_module, "OUTBOX_CLAIM_TTL", timedelta(milliseconds=60))
    calls: list[uuid.UUID] = []

    async def slow_delivery(job_id: uuid.UUID) -> None:
        calls.append(job_id)
        await asyncio.sleep(0.18)

    first = asyncio.create_task(
        redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=slow_delivery,
            notification=None,
        )
    )
    await asyncio.sleep(0.09)
    second = asyncio.create_task(
        redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=slow_delivery,
            notification=None,
        )
    )
    results = await asyncio.gather(first, second)

    assert calls == [job.id]
    assert sum(result.delivered for result in results) == 1


@pytest.mark.asyncio
async def test_sync_slow_delivery_runs_off_loop_so_short_lease_is_renewed(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations import outbox as outbox_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    monkeypatch.setattr(outbox_module, "OUTBOX_CLAIM_TTL", timedelta(milliseconds=60))
    calls: list[uuid.UUID] = []
    first_started = threading.Event()

    def slow_delivery(job_id: uuid.UUID) -> None:
        calls.append(job_id)
        first_started.set()
        time.sleep(0.18)

    database_url = postgres_session.bind.url.render_as_string(hide_password=False)

    def competing_redriver() -> object:
        assert first_started.wait(timeout=2)
        time.sleep(0.09)

        async def run() -> object:
            engine = create_async_engine(database_url)
            try:
                return await redrive_evaluation_outbox(
                    engine,
                    dispatch=slow_delivery,
                    notification=None,
                )
            finally:
                await engine.dispose()

        return asyncio.run(run())

    competitor = asyncio.create_task(asyncio.to_thread(competing_redriver))
    first = asyncio.create_task(
        redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=slow_delivery,
            notification=None,
        )
    )
    results = await asyncio.gather(first, competitor)

    assert calls == [job.id]
    assert sum(result.delivered for result in results) == 1


@pytest.mark.asyncio
async def test_sync_base_exception_stops_heartbeat_and_preserves_lease(
    postgres_session,
    monkeypatch,
) -> None:
    from app.evaluations import outbox as outbox_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    monkeypatch.setattr(outbox_module, "OUTBOX_CLAIM_TTL", timedelta(milliseconds=60))

    def crash_after_heartbeat(job_id: uuid.UUID) -> None:
        time.sleep(0.08)
        raise SystemExit("simulated synchronous worker loss")

    with pytest.raises(SystemExit, match="synchronous worker loss"):
        await redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=crash_after_heartbeat,
            notification=None,
            job_ids=(job.id,),
        )

    await postgres_session.rollback()
    before = (
        await postgres_session.execute(
            select(
                EvaluationOutbox.claim_token,
                EvaluationOutbox.claim_expires_at,
            ).where(EvaluationOutbox.job_id == job.id)
        )
    ).one()
    assert before.claim_token is not None
    await asyncio.sleep(0.08)
    await postgres_session.rollback()
    after = (
        await postgres_session.execute(
            select(
                EvaluationOutbox.claim_token,
                EvaluationOutbox.claim_expires_at,
            ).where(EvaluationOutbox.job_id == job.id)
        )
    ).one()
    assert after == before


@pytest.mark.asyncio
async def test_sync_callback_returning_awaitable_is_awaited(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    calls: list[uuid.UUID] = []

    def callback(job_id: uuid.UUID):
        async def complete() -> None:
            calls.append(job_id)

        return complete()

    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=callback,
        notification=None,
        job_ids=(job.id,),
    )
    assert result.delivered == 1
    assert calls == [job.id]


@pytest.mark.asyncio
async def test_outbox_publish_runs_after_claim_transaction_releases_row_lock(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    callback_observed_unlocked_row = False

    async def publish(job_id: uuid.UUID) -> None:
        nonlocal callback_observed_unlocked_row
        async with postgres_session.bind.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
            locked_id = await connection.scalar(
                select(EvaluationOutbox.id)
                .where(
                    EvaluationOutbox.kind == "dispatch",
                    EvaluationOutbox.job_id == job_id,
                )
                .with_for_update(nowait=True)
            )
            callback_observed_unlocked_row = locked_id is not None

    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=publish,
        notification=None,
        limit=10,
    )
    assert result.delivered == 1
    assert callback_observed_unlocked_row is True


@pytest.mark.asyncio
async def test_crash_after_publish_is_retried_only_after_claim_lease_expires(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    job = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).request(submission.id, JobReason.INITIAL)
    calls: list[uuid.UUID] = []

    async def publish_then_crash(job_id: uuid.UUID) -> None:
        calls.append(job_id)
        raise SystemExit("simulated worker loss after broker accepted publish")

    with pytest.raises(SystemExit, match="simulated worker loss"):
        await redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=publish_then_crash,
            notification=None,
            limit=10,
        )

    await postgres_session.rollback()
    claim = (
        await postgres_session.execute(
            select(
                EvaluationOutbox.claim_token,
                EvaluationOutbox.claim_expires_at,
                EvaluationOutbox.delivered_at,
            ).where(
                EvaluationOutbox.kind == "dispatch",
                EvaluationOutbox.job_id == job.id,
            )
        )
    ).one()
    assert claim.claim_token is not None
    assert claim.claim_expires_at > datetime.now(UTC)
    assert claim.delivered_at is None

    async def recovered(job_id: uuid.UUID) -> None:
        calls.append(job_id)

    still_leased = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=recovered,
        notification=None,
        limit=10,
    )
    assert still_leased.scanned == 0
    assert calls == [job.id]

    await postgres_session.execute(
        update(EvaluationOutbox)
        .where(EvaluationOutbox.job_id == job.id)
        .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await postgres_session.commit()
    expired = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=recovered,
        notification=None,
        limit=10,
    )
    assert expired.delivered == 1
    assert calls == [job.id, job.id]


@pytest.mark.asyncio
async def test_crash_after_notification_acceptance_retries_same_durable_event(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def accepted_then_crash(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        calls.append((job_id, report_id))
        raise SystemExit("simulated worker loss after Mattermost accepted post")

    with pytest.raises(SystemExit, match="Mattermost accepted"):
        await redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=None,
            notification=accepted_then_crash,
            job_ids=(report.job_id,),
        )

    await postgres_session.rollback()
    event = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.job_id == report.job_id,
        )
    )
    assert event is not None and event.claim_token is not None
    assert event.delivered_at is None and event.attempt_count == 0
    await postgres_session.execute(
        update(EvaluationOutbox)
        .where(EvaluationOutbox.id == event.id)
        .values(claim_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await postgres_session.commit()

    async def recovered(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        calls.append((job_id, report_id))
        return "0123456789abcdefghijklmnop"

    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=recovered,
        job_ids=(report.job_id,),
    )
    assert result.delivered == 1
    assert calls == [(report.job_id, report.id), (report.job_id, report.id)]


@pytest.mark.asyncio
async def test_success_notification_is_durable_after_commit_and_redrives_without_duplicate(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    observed_committed: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def unavailable(job_id: uuid.UUID, report_id: uuid.UUID) -> None:
        async with postgres_session.bind.connect() as connection:
            assert (
                await connection.scalar(
                    select(EvaluationReport.id).where(EvaluationReport.id == report_id)
                )
                == report_id
            )
        observed_committed.append((job_id, report_id))
        raise OSError("notification broker secret")

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=unavailable,
    )
    report = await service.evaluate_now(submission.id, JobReason.INITIAL)
    await postgres_session.rollback()
    event = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.job_id == report.job_id,
        )
    )
    assert event is not None and event.report_id == report.id
    event_id = uuid.UUID(str(event.id))
    assert event.delivered_at is None
    assert observed_committed == [(report.job_id, report.id)]

    delivered: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def recovered(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        delivered.append((job_id, report_id))
        return "post0000000000000000000001"

    await postgres_session.execute(
        update(EvaluationOutbox)
        .where(EvaluationOutbox.id == event.id)
        .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await postgres_session.commit()
    await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=recovered,
        limit=10,
    )
    await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=recovered,
        limit=10,
    )
    assert delivered == [(report.job_id, report.id)]
    await postgres_session.rollback()
    postgres_session.expire_all()
    persisted = await postgres_session.get(EvaluationOutbox, event_id)
    assert persisted is not None
    assert persisted.delivery_ref == "post0000000000000000000001"
    evidence = await postgres_session.scalar(
        select(IntegrationEvent).where(
            IntegrationEvent.event_type == "notification_delivered",
            IntegrationEvent.business_refs["outbox_id"].astext == str(event_id),
        )
    )
    assert evidence is not None
    assert evidence.response["post_id"] == persisted.delivery_ref


@pytest.mark.asyncio
async def test_notification_retries_at_most_three_times_then_persists_failure_evidence(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)

    async def unavailable(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        raise MattermostRetryableError(error_type="network", retry_after_seconds=1)

    for expected_attempt in (1, 2, 3):
        await postgres_session.execute(
            update(EvaluationOutbox)
            .where(EvaluationOutbox.job_id == report.job_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await postgres_session.commit()
        result = await redrive_evaluation_outbox(
            postgres_session.bind,
            dispatch=None,
            notification=unavailable,
            job_ids=(report.job_id,),
        )
        assert result.failed == 1
        await postgres_session.rollback()
        postgres_session.expire_all()
        event = await postgres_session.scalar(
            select(EvaluationOutbox)
            .where(EvaluationOutbox.job_id == report.job_id)
            .where(EvaluationOutbox.kind == "notification")
        )
        assert event is not None and event.attempt_count == expected_attempt

    assert event.failed_at is not None
    assert event.delivered_at is None
    assert event.last_error_type == "network"
    duplicate = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=unavailable,
        job_ids=(report.job_id,),
    )
    assert duplicate.scanned == 0
    evidence = await postgres_session.scalar(
        select(IntegrationEvent).where(
            IntegrationEvent.event_type == "notification_failed",
            IntegrationEvent.business_refs["outbox_id"].astext == str(event.id),
        )
    )
    assert evidence is not None
    job = await postgres_session.get(EvaluationJob, report.job_id)
    assert job is not None and job.status is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_permanent_notification_failure_is_terminal_on_first_attempt(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)

    async def rejected(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        raise MattermostPermanentError(error_type="recipient")

    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=rejected,
        job_ids=(report.job_id,),
    )
    assert result.failed == 1
    await postgres_session.rollback()
    postgres_session.expire_all()
    event = await postgres_session.scalar(
        select(EvaluationOutbox)
        .where(EvaluationOutbox.job_id == report.job_id)
        .where(EvaluationOutbox.kind == "notification")
    )
    assert event is not None and event.attempt_count == 1 and event.failed_at is not None


@pytest.mark.asyncio
async def test_notification_recipient_is_evaluation_requester_not_assignment_creator(
    postgres_session,
) -> None:
    creator, _, _, submission = await _seed_subject(postgres_session)
    requester = User(
        username=f"requester-{uuid.uuid4()}",
        display_name="Requesting Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(requester)
    await postgres_session.flush()
    creator_mattermost_id = "creator00000000000000000001"
    requester_mattermost_id = "requester000000000000000001"
    postgres_session.add_all(
        [
            MattermostIdentity(
                user_id=creator.id,
                mattermost_user_id=creator_mattermost_id,
                mattermost_username="creator",
            ),
            MattermostIdentity(
                user_id=requester.id,
                mattermost_user_id=requester_mattermost_id,
                mattermost_username="requester",
            ),
        ]
    )
    await postgres_session.commit()
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=requester.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    direct_recipients: list[list[str]] = []
    post_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/channels/direct"):
            direct_recipients.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "channel000000000000000001"})
        post_payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "post0000000000000000000001"})

    post_id = await deliver_report_notification(
        postgres_session.bind,
        job_id=report.job_id,
        report_id=report.id,
        settings=Settings(
            _env_file=None,
            app_env="test",
            mattermost_url="http://mattermost.test",
            mattermost_bot_token=SecretStr("bot-token"),
            mattermost_bot_user_id="bot0000000000000000000001",
            mattermost_action_secret=SecretStr("action-secret"),
            mattermost_action_url=("http://api.test/api/v1/integrations/mattermost/actions"),
            web_console_url="http://console.test",
        ),
        transport=httpx.MockTransport(handler),
    )
    assert post_id == "post0000000000000000000001"
    assert direct_recipients == [["bot0000000000000000000001", requester_mattermost_id]]
    assert post_payloads[0]["channel_id"] == "channel000000000000000001"
    actions = post_payloads[0]["props"]["attachments"][0]["actions"]  # type: ignore[index]
    assert all(
        action["integration"]["context"]["expected_channel_id"]  # type: ignore[index]
        == "channel000000000000000001"
        for action in actions
    )


@pytest.mark.asyncio
async def test_missing_requester_identity_is_permanent_and_persists_safe_evidence(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    runtime = Settings(
        _env_file=None,
        app_env="test",
        mattermost_url="http://mattermost.test",
        mattermost_bot_token=SecretStr("bot-token"),
        mattermost_bot_user_id="bot0000000000000000000001",
        mattermost_action_secret=SecretStr("action-secret"),
        mattermost_action_url="http://api.test/api/v1/integrations/mattermost/actions",
        web_console_url="http://console.test",
    )

    async def notification(job_id: uuid.UUID, report_id: uuid.UUID) -> str:
        return await deliver_report_notification(
            postgres_session.bind,
            job_id=job_id,
            report_id=report_id,
            settings=runtime,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("HTTP must not run"))
            ),
        )

    result = await redrive_evaluation_outbox(
        postgres_session.bind,
        dispatch=None,
        notification=notification,
        job_ids=(report.job_id,),
    )
    assert result.failed == 1
    await postgres_session.rollback()
    row = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.kind == "notification",
            EvaluationOutbox.job_id == report.job_id,
        )
    )
    assert row is not None and row.failed_at is not None and row.attempt_count == 1
    assert row.last_error_type == "recipient"
    evidence = await postgres_session.scalar(
        select(IntegrationEvent).where(
            IntegrationEvent.event_type == "notification_failed",
            IntegrationEvent.business_refs["outbox_id"].astext == str(row.id),
        )
    )
    assert evidence is not None
    assert "bot-token" not in json.dumps(evidence.response)


@pytest.mark.asyncio
async def test_withdraw_after_provider_persists_cancelled_evidence_without_report(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind

    class Provider(MockEvaluationProvider):
        async def evaluate(self, request):
            result = await super().evaluate(request)
            async with database_engine.begin() as connection:
                await connection.execute(
                    update(Submission)
                    .where(Submission.id == submission.id)
                    .values(status="withdrawn")
                )
            return result

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(Provider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await service.execute_job(job.id)

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert persisted.status is JobStatus.CANCELLED
    assert persisted.attempt_count == 1
    assert persisted.error_code is None and persisted.error_message is None
    assert audit.error_type == "cancelled"
    assert audit.error_message == "evaluation cancelled after provider execution"
    assert audit.raw_model_output
    assert audit.duration_ms >= 0
    assert audit.final_report_id is None
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )
