import asyncio
import hashlib
import json
import pickle
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
from sqlalchemy import event, select, text, update

from app.assignments.model import Assignment
from app.assignments.summary import build_assignment_summary
from app.audit.model import AuditLog
from app.core.config import Settings
from app.db.types import (
    AssignmentStatus,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.evaluations.engine import EngineFailureEvidence, EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.base import (
    EvaluationRequest,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderResult,
    ProviderUnavailable,
)
from app.evaluations.providers.factory import create_evaluation_provider
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import (
    EvaluationJobFailed,
    EvaluationRecoveryRequired,
    EvaluationService,
    EvaluationSubjectNotFound,
    evaluation_key,
)
from app.evaluations.types import JobReason, JobStatus, ReportOrigin, ReviewStatus
from app.reviews.schemas import ReevaluateRequest, ReportPatch
from app.reviews.service import ReviewService
from app.submissions.model import Submission
from app.users.model import User
from tests.evaluations.helpers import render_test_database_url


def test_evaluation_key_is_the_frozen_sha256_contract() -> None:
    submission_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    assert (
        evaluation_key(submission_id, 7, JobReason.INITIAL)
        == "c427d47e531759af77e1cfcb099c79ef0c81c9f77b05b4334b728d12700f97fe"
    )
    assert evaluation_key(submission_id, 7, JobReason.INITIAL) != evaluation_key(
        submission_id, 7, JobReason.PROVIDER_RETRY
    )
    assert evaluation_key(
        submission_id,
        7,
        JobReason.PROVIDER_RETRY,
        generation=1,
    ) != evaluation_key(submission_id, 7, JobReason.PROVIDER_RETRY)


def test_evaluation_key_rejects_constructed_or_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        evaluation_key("11111111-1111-1111-1111-111111111111", 1, JobReason.INITIAL)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evaluation_key(uuid.uuid4(), 0, JobReason.INITIAL)
    with pytest.raises(TypeError):
        evaluation_key(uuid.uuid4(), 1, "initial")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evaluation_key(uuid.uuid4(), 1, JobReason.PROVIDER_RETRY, generation=-1)


def test_provider_identity_is_strict_and_safe() -> None:
    identity = ProviderIdentity(provider="mock", model="fixture-v1")
    assert identity.provider == "mock"
    assert identity.model == "fixture-v1"
    with pytest.raises(ValueError):
        ProviderIdentity(provider="mock\nsecret", model="fixture-v1")


def test_mock_provider_exposes_the_exact_identity_persisted_on_jobs() -> None:
    provider = MockEvaluationProvider()
    assert provider.identity == ProviderIdentity(provider="mock", model="fixture-v1")


def test_provider_factory_requires_secrets_only_for_selected_real_provider() -> None:
    mock_settings = Settings(
        app_env="test",
        jwt_secret="test-only",
        agent_provider="mock",
        agent_api_key=None,
    )
    assert create_evaluation_provider(mock_settings).identity.provider == "mock"

    with pytest.raises(ValueError):
        Settings(
            app_env="test",
            jwt_secret="test-only",
            agent_provider="openai-compatible",
            agent_api_key=None,
        )


def test_provider_settings_hide_api_key() -> None:
    settings = Settings(
        app_env="test",
        jwt_secret="test-only",
        agent_provider="openai-compatible",
        agent_base_url="https://llm.test/v1",
        agent_model="grader-v1",
        agent_api_key="top-secret-api-key",
    )
    assert "top-secret-api-key" not in repr(settings)
    redis_settings = Settings(
        app_env="test",
        jwt_secret="test-only",
        redis_url="redis://user:redis-secret@localhost:6379/0",
    )
    assert "redis-secret" not in repr(redis_settings)


def test_provider_response_format_is_typed_and_forwarded() -> None:
    with pytest.raises(ValueError):
        Settings(agent_response_format="xml")  # type: ignore[arg-type]

    settings = Settings(
        app_env="test",
        jwt_secret="test-only",
        agent_provider="openai-compatible",
        agent_api_key="top-secret-api-key",
        agent_response_format="json-object",
    )
    provider = create_evaluation_provider(settings)
    assert provider.response_format == "json-object"


def test_celery_worker_uses_json_and_late_ack_contract() -> None:
    from app.evaluations.worker import celery_app, run_evaluation

    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert run_evaluation.name == "app.evaluations.worker.run_evaluation"


@pytest.mark.asyncio
async def test_engine_sealed_execution_preserves_prior_validation_evidence() -> None:
    class Provider:
        identity = ProviderIdentity(provider="scripted", model="v1")

        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(
                    provider="scripted",
                    model="v1",
                    raw_text="not-json",
                    duration_ms=7,
                )
            raise ProviderUnavailable(
                "evaluation provider timed out",
                code=ProviderErrorCode.TIMEOUT,
            )

    request = EvaluationRequest(
        assignment_title="A",
        question="Q",
        rubric={},
        student_answer="answer",
    )
    evidence = await EvaluationEngine(Provider()).execute_with_evidence(request)
    assert type(evidence) is EngineFailureEvidence
    assert evidence.code is ProviderErrorCode.TIMEOUT
    assert evidence.attempt_count == 2
    assert evidence.retry_count == 1
    assert [failure.code.value for failure in evidence.failures] == ["invalid_json"]
    assert evidence.duration_ms == 7
    assert evidence.last_raw_text == "not-json"
    assert "answer" not in repr(evidence)
    assert "not-json" not in repr(evidence)
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(evidence)
    with pytest.raises(ValueError):
        EngineFailureEvidence(
            error_type="timeout",
            code=ProviderErrorCode.TIMEOUT,
            attempt_count=0,
            retry_count=0,
            failures=(),
            duration_ms=0,
            last_raw_text=None,
            provider="scripted",
            model="v1",
        )


async def _seed_subject(postgres_session):
    teacher = User(
        username=f"teacher-{uuid.uuid4()}",
        display_name="Teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    student = User(
        username=f"student-{uuid.uuid4()}",
        display_name="Student",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add_all([teacher, student])
    await postgres_session.flush()
    assignment = Assignment(
        code=f"HW-{str(uuid.uuid4())[:8]}",
        title="Shortest paths",
        question="Explain Dijkstra.",
        notes="",
        rubric={"required_points": ["relaxation"]},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    postgres_session.add(assignment)
    await postgres_session.flush()
    submission = Submission(
        assignment_id=assignment.id,
        student_id=student.id,
        version=1,
        content_type=SubmissionContentType.TEXT,
        content_text="Use a priority queue.",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        source=SubmissionSource.WEB,
    )
    postgres_session.add(submission)
    await postgres_session.commit()
    return teacher, student, assignment, submission


@pytest.mark.asyncio
async def test_request_persists_before_dispatch_and_is_idempotent(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    observed: list[uuid.UUID] = []

    async def dispatch(job_id: uuid.UUID) -> None:
        # A separate transaction can see the job, proving dispatch is after commit.
        async with postgres_session.bind.connect() as connection:
            assert (
                await connection.scalar(select(EvaluationJob.id).where(EvaluationJob.id == job_id))
            ) == job_id
        observed.append(job_id)

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=dispatch,
    )
    first = await service.request(submission.id, JobReason.INITIAL)
    duplicate = await service.request(submission.id, JobReason.INITIAL)

    assert first.id == duplicate.id
    assert first.provider == "mock"
    assert first.model == "fixture-v1"
    assert observed == [first.id]


@pytest.mark.asyncio
async def test_request_revalidates_teacher_and_hides_missing_or_withdrawn(postgres_session) -> None:
    teacher, student, _, submission = await _seed_subject(postgres_session)
    student_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=student.id,
        dispatch=None,
    )
    with pytest.raises(PermissionError):
        await student_service.request(submission.id, JobReason.INITIAL)

    teacher_service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    with pytest.raises(EvaluationSubjectNotFound):
        await teacher_service.request(uuid.uuid4(), JobReason.INITIAL)

    submission.status = SubmissionStatus.WITHDRAWN
    await postgres_session.commit()
    with pytest.raises(EvaluationSubjectNotFound):
        await teacher_service.request(submission.id, JobReason.INITIAL)


@pytest.mark.asyncio
async def test_two_failed_provider_generations_can_recover_without_mutating_history(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    failing = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key=f"missing-{uuid.uuid4().hex}",
    )
    failed_ids: list[uuid.UUID] = []
    for _ in range(2):
        job = await failing.request(submission.id, JobReason.PROVIDER_RETRY)
        failed_ids.append(job.id)
        with pytest.raises(EvaluationJobFailed):
            await failing.execute_job(job.id)

    recovered = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    recovered_job = await recovered.request(submission.id, JobReason.PROVIDER_RETRY)
    report = await recovered.execute_job(recovered_job.id)
    duplicate = await recovered.request(submission.id, JobReason.PROVIDER_RETRY)

    assert len({*failed_ids, recovered_job.id}) == 3
    assert duplicate.id == recovered_job.id
    assert report.job_id == recovered_job.id
    await postgres_session.rollback()
    jobs = (
        (
            await postgres_session.execute(
                select(EvaluationJob)
                .where(
                    EvaluationJob.submission_id == submission.id,
                    EvaluationJob.reason == JobReason.PROVIDER_RETRY,
                )
                .order_by(EvaluationJob.queued_at, EvaluationJob.id)
            )
        )
        .scalars()
        .all()
    )
    assert [job.status.value for job in jobs] == ["failed", "failed", "succeeded"]
    assert len({job.idempotency_key for job in jobs}) == 3


@pytest.mark.asyncio
async def test_evaluate_now_runs_same_job_state_machine_and_persists_audit(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    notifications: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def notify(job_id: uuid.UUID, report_id: uuid.UUID) -> None:
        # The terminal transaction is visible before notification.
        async with postgres_session.bind.connect() as connection:
            assert (
                await connection.scalar(
                    select(EvaluationJob.status).where(EvaluationJob.id == job_id)
                )
                == "succeeded"
            )
            assert (
                await connection.scalar(
                    select(EvaluationReport.id).where(EvaluationReport.id == report_id)
                )
                == report_id
            )
        notifications.append((job_id, report_id))

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        notification=notify,
    )
    report = await service.evaluate_now(submission.id, JobReason.INITIAL)

    await postgres_session.rollback()
    job = await postgres_session.get(EvaluationJob, report.job_id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert job.status.value == "succeeded"
    assert job.attempt_count == 1
    assert report.version == 1
    assert report.score == 72
    assert report.grade.value == "C"
    assert report.review_status.value == "proposed"
    assert audit.final_report_id == report.id
    assert audit.retry_count == 0
    assert notifications == [(job.id, report.id)]


@pytest.mark.asyncio
async def test_manual_retry_requires_an_authoritative_same_submission_source_and_scopes_jobs(
    postgres_session,
) -> None:
    teacher, _, assignment, submission = await _seed_subject(postgres_session)
    other_student = User(
        username=f"manual-other-{uuid.uuid4()}",
        display_name="Other Student",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(other_student)
    await postgres_session.flush()
    other_submission = Submission(
        assignment_id=assignment.id,
        student_id=other_student.id,
        version=1,
        content_type=SubmissionContentType.TEXT,
        content_text="Other answer",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        source=SubmissionSource.WEB,
    )
    postgres_session.add(other_submission)
    await postgres_session.commit()

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    report_one = await service.evaluate_now(submission.id, JobReason.INITIAL)
    other_report = await service.evaluate_now(other_submission.id, JobReason.INITIAL)

    for unavailable_source in (None, uuid.uuid4(), other_report.id):
        with pytest.raises(EvaluationSubjectNotFound, match="evaluation source not found"):
            await service.request(
                submission.id,
                JobReason.MANUAL_RETRY,
                source_report_id=unavailable_source,
            )
    with pytest.raises(ValueError, match="source_report_id is only valid for manual retry"):
        await service.request(
            submission.id,
            JobReason.PROVIDER_RETRY,
            source_report_id=report_one.id,
        )

    review = ReviewService(postgres_session)
    job_two = await review.reevaluate(
        report_one.id,
        teacher.id,
        ReevaluateRequest(),
        service,
    )
    report_two = await service.execute_job(job_two.id)
    duplicate = await review.reevaluate(
        report_one.id,
        teacher.id,
        ReevaluateRequest(),
        service,
    )
    job_three = await review.reevaluate(
        report_two.id,
        teacher.id,
        ReevaluateRequest(),
        service,
    )
    report_three = await service.execute_job(job_three.id)

    assert duplicate.id == report_two.job_id
    assert len({report_one.job_id, report_two.job_id, report_three.job_id}) == 3
    assert [report_one.version, report_two.version, report_three.version] == [1, 2, 3]
    jobs = (
        (
            await postgres_session.execute(
                select(EvaluationJob)
                .where(EvaluationJob.id.in_([report_two.job_id, report_three.job_id]))
                .order_by(EvaluationJob.queued_at, EvaluationJob.id)
            )
        )
        .scalars()
        .all()
    )
    expected_two = hashlib.sha256(
        f"{submission.id}:1:manual_retry:{report_one.id}".encode()
    ).hexdigest()
    expected_three = hashlib.sha256(
        f"{submission.id}:1:manual_retry:{report_two.id}".encode()
    ).hexdigest()
    assert {job.idempotency_key for job in jobs} == {expected_two, expected_three}
    assert all(job.reason is JobReason.MANUAL_RETRY for job in jobs)
    assert {job.source_report_id for job in jobs} == {report_one.id, report_two.id}
    await postgres_session.rollback()
    assert (await postgres_session.get(EvaluationReport, report_one.id)).review_status is (
        ReviewStatus.SUPERSEDED
    )
    assert (await postgres_session.get(EvaluationReport, report_two.id)).review_status is (
        ReviewStatus.SUPERSEDED
    )


@pytest.mark.asyncio
async def test_manual_retry_accepts_current_modified_teacher_report_with_agent_lineage(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    agent_report = await service.evaluate_now(submission.id, JobReason.INITIAL)
    review = ReviewService(postgres_session)
    teacher_report = await review.modify(
        agent_report.id,
        teacher.id,
        ReportPatch(comment="teacher correction"),
    )
    job = await review.reevaluate(
        teacher_report.id,
        teacher.id,
        ReevaluateRequest(),
        service,
    )
    retried = await service.execute_job(job.id)
    assert retried.version == 3
    assert retried.origin is ReportOrigin.AGENT
    await postgres_session.rollback()
    refreshed_teacher_report = await postgres_session.scalar(
        select(EvaluationReport)
        .where(EvaluationReport.id == teacher_report.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed_teacher_report.review_status is ReviewStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_manual_retry_rejects_historical_or_superseded_source_without_enumeration(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    historical = await service.evaluate_now(submission.id, JobReason.INITIAL)
    current = await service.evaluate_now(submission.id, JobReason.PROVIDER_RETRY)

    with pytest.raises(EvaluationSubjectNotFound, match="evaluation source not found"):
        await service.request(
            submission.id,
            JobReason.MANUAL_RETRY,
            source_report_id=historical.id,
        )
    await ReviewService(postgres_session).modify(
        current.id,
        teacher.id,
        ReportPatch(comment="legal replacement"),
    )
    with pytest.raises(EvaluationSubjectNotFound, match="evaluation source not found"):
        await service.request(
            submission.id,
            JobReason.MANUAL_RETRY,
            source_report_id=current.id,
        )


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_call_provider_twice(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    provider = MockEvaluationProvider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    first = await service.execute_job(job.id)
    duplicate = await service.execute_job(job.id)
    assert first.id == duplicate.id
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        == first.id
    )


@pytest.mark.asyncio
async def test_provider_failure_after_validation_persists_exact_terminal_evidence(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="v1")

        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(
                    provider="scripted",
                    model="v1",
                    raw_text="not-json",
                    duration_ms=9,
                )
            raise ProviderUnavailable(
                "safe timeout",
                code=ProviderErrorCode.TIMEOUT,
            )

    provider = Provider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=teacher.id,
        dispatch=None,
    )
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await service.evaluate_now(submission.id, JobReason.INITIAL)

    await postgres_session.rollback()
    job = await service.get_latest_job(submission.id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert provider.calls == 2
    assert job.status.value == "failed"
    assert job.error_code == "timeout"
    assert job.error_message == "evaluation provider timed out"
    assert job.attempt_count == 2
    assert audit.retry_count == 1
    assert audit.validation_failures == ["invalid_json"]
    assert audit.duration_ms == 9
    assert audit.raw_model_output == "not-json"


@pytest.mark.asyncio
async def test_validation_exhaustion_persists_three_failures(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="invalid-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            return ProviderResult(
                provider="scripted",
                model="invalid-v1",
                raw_text=json.dumps({"bad": True}),
                duration_ms=2,
            )

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(Provider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await service.evaluate_now(submission.id, JobReason.INITIAL)

    await postgres_session.rollback()
    job = await service.get_latest_job(submission.id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert job.error_code == "validation_exhausted"
    assert job.attempt_count == 3
    assert audit.retry_count == 2
    assert audit.validation_failures == ["schema", "schema", "schema"]
    assert audit.duration_ms == 6


@pytest.mark.asyncio
async def test_concurrent_requests_return_one_job_and_dispatch_once(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    dispatched: list[uuid.UUID] = []

    async def dispatch(job_id: uuid.UUID) -> None:
        dispatched.append(job_id)

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=dispatch,
    )
    jobs = await asyncio.gather(
        *(service.request(submission.id, JobReason.INITIAL) for _ in range(12))
    )
    assert len({job.id for job in jobs}) == 1
    assert dispatched == [jobs[0].id]


@pytest.mark.asyncio
async def test_batch_requests_only_latest_submitted_version_per_student(postgres_session) -> None:
    teacher, student, assignment, first = await _seed_subject(postgres_session)
    first.status = SubmissionStatus.WITHDRAWN
    latest = Submission(
        assignment_id=assignment.id,
        student_id=student.id,
        version=2,
        content_type=SubmissionContentType.TEXT,
        content_text="Latest answer",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        source=SubmissionSource.WEB,
    )
    second_student = User(
        username=f"student-{uuid.uuid4()}",
        display_name="Second",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add_all([latest, second_student])
    await postgres_session.flush()
    second = Submission(
        assignment_id=assignment.id,
        student_id=second_student.id,
        version=1,
        content_type=SubmissionContentType.TEXT,
        content_text="Second answer",
        content_json=None,
        status=SubmissionStatus.SUBMITTED,
        source=SubmissionSource.WEB,
    )
    postgres_session.add(second)
    await postgres_session.commit()

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    batch = await service.request_assignment(assignment.id, JobReason.INITIAL)
    assert batch.queued == 2
    assert batch.skipped == 0
    jobs = (
        (
            await postgres_session.execute(
                select(EvaluationJob).where(EvaluationJob.id.in_(batch.job_ids))
            )
        )
        .scalars()
        .all()
    )
    assert {job.submission_id for job in jobs} == {latest.id, second.id}

    duplicate = await service.request_assignment(assignment.id, JobReason.INITIAL)
    assert duplicate.queued == 0
    assert duplicate.skipped == 2
    assert set(duplicate.job_ids) == set(batch.job_ids)


@pytest.mark.asyncio
async def test_assignment_enqueue_is_caller_owned_and_never_dispatches(postgres_session) -> None:
    teacher, _, assignment, _ = await _seed_subject(postgres_session)
    dispatched: list[uuid.UUID] = []

    async def dispatch(job_id: uuid.UUID) -> None:
        dispatched.append(job_id)

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=dispatch,
    )
    batch = await service.enqueue_assignment_in_transaction(
        postgres_session,
        assignment.id,
        JobReason.INITIAL,
    )
    assert batch.queued == 1
    assert batch.skipped == 0
    assert dispatched == []
    assert (
        await postgres_session.scalar(
            select(EvaluationJob.id).where(EvaluationJob.id.in_(batch.job_ids))
        )
        in batch.job_ids
    )
    assert (
        await postgres_session.scalar(
            select(EvaluationOutbox.id).where(EvaluationOutbox.job_id.in_(batch.job_ids))
        )
        is not None
    )

    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(EvaluationJob.id).where(EvaluationJob.id.in_(batch.job_ids))
        )
        is None
    )
    assert (
        await postgres_session.scalar(
            select(EvaluationOutbox.id).where(EvaluationOutbox.job_id.in_(batch.job_ids))
        )
        is None
    )


@pytest.mark.asyncio
async def test_assignment_enqueue_handles_empty_and_mixed_existing_batches(
    postgres_session,
) -> None:
    teacher, _, assignment, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    existing = await service.request(submission.id, JobReason.INITIAL)
    second_student = User(
        username=f"transaction-batch-{uuid.uuid4()}",
        display_name="Second",
        role=Role.STUDENT,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(second_student)
    await postgres_session.flush()
    postgres_session.add(
        Submission(
            assignment_id=assignment.id,
            student_id=second_student.id,
            version=1,
            content_type=SubmissionContentType.TEXT,
            content_text="Second answer",
            status=SubmissionStatus.SUBMITTED,
            source=SubmissionSource.MATTERMOST,
        )
    )
    empty = Assignment(
        code=f"HW-{uuid.uuid4().hex[:8]}",
        title="Empty",
        question="No answers",
        notes="",
        rubric={},
        due_at=datetime.now(UTC) + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        created_by=teacher.id,
        published_at=datetime.now(UTC),
    )
    postgres_session.add(empty)
    await postgres_session.commit()

    mixed = await service.enqueue_assignment_in_transaction(
        postgres_session,
        assignment.id,
        JobReason.INITIAL,
    )
    assert mixed.queued == 1
    assert mixed.skipped == 1
    assert existing.id in mixed.job_ids

    zero = await service.enqueue_assignment_in_transaction(
        postgres_session,
        empty.id,
        JobReason.INITIAL,
    )
    assert zero.queued == zero.skipped == 0
    assert zero.job_ids == ()
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_batch_handles_ten_thousand_latest_submissions_in_bounded_queries(
    postgres_session,
) -> None:
    teacher, _, assignment, _ = await _seed_subject(postgres_session)
    await postgres_session.execute(
        text(
            "INSERT INTO users "
            "(id, username, display_name, role, password_hash, is_active) "
            "SELECT md5('batch-student-' || value::text)::uuid, "
            "'batch-student-' || value::text, 'Batch Student', 'student', 'hash', true "
            "FROM generate_series(2, 10000) AS value"
        )
    )
    await postgres_session.execute(
        text(
            "INSERT INTO submissions "
            "(id, assignment_id, student_id, version, content_type, content_text, "
            "status, source) "
            "SELECT md5('batch-submission-' || value::text)::uuid, :assignment_id, "
            "md5('batch-student-' || value::text)::uuid, 1, 'text', 'answer', "
            "'submitted', 'web' FROM generate_series(2, 10000) AS value"
        ),
        {"assignment_id": assignment.id},
    )
    await postgres_session.commit()
    statements = 0

    def count_statement(connection, cursor, statement, parameters, context, executemany) -> None:
        del connection, cursor, statement, parameters, context, executemany
        nonlocal statements
        statements += 1

    event.listen(postgres_session.bind.sync_engine, "before_cursor_execute", count_statement)
    try:
        batch = await EvaluationService(
            postgres_session,
            EvaluationEngine(MockEvaluationProvider()),
            requested_by=teacher.id,
            dispatch=None,
        ).enqueue_assignment_in_transaction(
            postgres_session,
            assignment.id,
            JobReason.INITIAL,
        )
    finally:
        event.remove(postgres_session.bind.sync_engine, "before_cursor_execute", count_statement)

    assert batch.queued == len(batch.job_ids) == 10_000
    assert batch.skipped == 0
    assert len(set(batch.job_ids)) == 10_000
    assert statements <= 70


@pytest.mark.asyncio
async def test_one_failed_batch_job_does_not_affect_two_successful_jobs(postgres_session) -> None:
    teacher, first_student, assignment, first = await _seed_subject(postgres_session)
    first.content_text = "first"
    students = [first_student]
    submissions = [first]
    for index in (2, 3):
        student = User(
            username=f"batch-{index}-{uuid.uuid4()}",
            display_name=f"Batch {index}",
            role=Role.STUDENT,
            password_hash="hash",
            is_active=True,
        )
        postgres_session.add(student)
        await postgres_session.flush()
        submission = Submission(
            assignment_id=assignment.id,
            student_id=student.id,
            version=1,
            content_type=SubmissionContentType.TEXT,
            content_text=("fail-explicit" if index == 2 else "third"),
            content_json=None,
            status=SubmissionStatus.SUBMITTED,
            source=SubmissionSource.WEB,
        )
        postgres_session.add(submission)
        students.append(student)
        submissions.append(submission)
    await postgres_session.commit()

    class ScenarioProvider:
        identity = ProviderIdentity(provider="scenario", model="explicit-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            if request.student_answer == "fail-explicit":
                raise ProviderUnavailable(
                    "offline",
                    code=ProviderErrorCode.NETWORK,
                )
            return ProviderResult(
                provider="scenario",
                model="explicit-v1",
                raw_text=(
                    files("fixtures.agent_outputs").joinpath("complete.json").read_text("utf-8")
                ),
                duration_ms=1,
            )

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(ScenarioProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    batch = await service.request_assignment(assignment.id, JobReason.INITIAL)
    jobs = (
        (
            await postgres_session.execute(
                select(EvaluationJob).where(EvaluationJob.id.in_(batch.job_ids))
            )
        )
        .scalars()
        .all()
    )
    job_by_submission = {job.submission_id: job for job in jobs}
    outcomes: list[str] = []
    for submission in submissions:
        try:
            await service.execute_job(job_by_submission[submission.id].id)
        except EvaluationJobFailed:
            outcomes.append("failed")
        else:
            outcomes.append("succeeded")
    assert outcomes == ["succeeded", "failed", "succeeded"]
    assert len(students) == 3


@pytest.mark.asyncio
async def test_summary_projects_latest_job_and_report_without_raw_output(postgres_session) -> None:
    teacher, _, assignment, submission = await _seed_subject(postgres_session)
    assignment_id = assignment.id
    submission_id = submission.id
    student_id = submission.student_id
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    report = await service.evaluate_now(submission_id, JobReason.INITIAL)
    await postgres_session.rollback()

    summary = await build_assignment_summary(postgres_session, assignment_id, limit=50, offset=0)
    assert summary.pending_review == 1
    assert summary.reviewed == summary.failed == summary.evaluating == 0
    row = next(item for item in summary.students if item.student_id == student_id)
    assert row.evaluation_status == "succeeded"
    assert row.report_status == "proposed"
    assert row.score == report.score
    assert row.grade == report.grade
    assert row.evaluation_error is None
    assert row.evaluation_error_code is None
    assert row.latest_report_id == report.id
    assert summary.queued == 0

    await service.request(submission_id, JobReason.PROVIDER_RETRY)
    await postgres_session.rollback()
    pending = await build_assignment_summary(postgres_session, assignment_id, limit=50, offset=0)
    assert pending.pending_evaluation == 1
    assert pending.pending_review == 0
    assert pending.queued == 1
    assert pending.students[0].evaluation_status == "queued"


@pytest.mark.asyncio
async def test_withdrawn_after_queue_cancels_without_calling_provider(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="v1")

        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.calls += 1
            raise AssertionError("provider must not be called")

    provider = Provider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    submission.status = SubmissionStatus.WITHDRAWN
    await postgres_session.commit()

    with pytest.raises(EvaluationJobFailed):
        await service.execute_job(job.id)
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status.value == "cancelled"
    assert provider.calls == 0
    assert (
        await postgres_session.scalar(select(AuditLog.id).where(AuditLog.job_id == job.id)) is None
    )


@pytest.mark.asyncio
async def test_withdrawn_after_provider_cancels_terminal_without_report_or_redelivery(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="withdraw-race-v1")

        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            self.calls += 1
            async with database_engine.begin() as connection:
                await connection.execute(
                    update(Submission)
                    .where(Submission.id == submission.id)
                    .values(status=SubmissionStatus.WITHDRAWN)
                )
            return ProviderResult(
                provider="scripted",
                model="withdraw-race-v1",
                raw_text=(
                    files("fixtures.agent_outputs").joinpath("complete.json").read_text("utf-8")
                ),
                duration_ms=1,
            )

    provider = Provider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)

    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await service.execute_job(job.id)
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await service.execute_job(job.id)

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert provider.calls == 1
    assert persisted.status is JobStatus.CANCELLED
    assert persisted.finished_at is not None
    assert persisted.error_code is None
    assert persisted.error_message is None
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert audit.error_type == "cancelled"
    assert audit.raw_model_output
    assert audit.final_report_id is None


@pytest.mark.asyncio
async def test_withdrawal_terminal_compensation_retries_one_failed_commit(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind
    fail_compensation = False
    failed_commits = 0

    def fail_first_compensation_commit(connection) -> None:
        del connection
        nonlocal failed_commits
        if fail_compensation and failed_commits == 0:
            failed_commits += 1
            raise RuntimeError("injected cancellation commit failure")

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="withdraw-retry-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            nonlocal fail_compensation
            async with database_engine.begin() as connection:
                await connection.execute(
                    update(Submission)
                    .where(Submission.id == submission.id)
                    .values(status=SubmissionStatus.WITHDRAWN)
                )
            fail_compensation = True
            return ProviderResult(
                provider="scripted",
                model="withdraw-retry-v1",
                raw_text=(
                    files("fixtures.agent_outputs").joinpath("complete.json").read_text("utf-8")
                ),
                duration_ms=1,
            )

    event.listen(database_engine.sync_engine, "commit", fail_first_compensation_commit)
    try:
        service = EvaluationService(
            postgres_session,
            EvaluationEngine(Provider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        job = await service.request(submission.id, JobReason.INITIAL)
        with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
            await service.execute_job(job.id)
    finally:
        event.remove(database_engine.sync_engine, "commit", fail_first_compensation_commit)

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert failed_commits == 1
    assert persisted.status is JobStatus.CANCELLED
    assert persisted.finished_at is not None


@pytest.mark.asyncio
async def test_non_withdrawal_subject_failure_is_failed_not_cancelled(
    postgres_session,
) -> None:
    teacher, _, assignment, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="assignment-race-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            async with database_engine.begin() as connection:
                await connection.execute(
                    update(Assignment)
                    .where(Assignment.id == assignment.id)
                    .values(status=AssignmentStatus.DRAFT)
                )
            return ProviderResult(
                provider="scripted",
                model="assignment-race-v1",
                raw_text=(
                    files("fixtures.agent_outputs").joinpath("complete.json").read_text("utf-8")
                ),
                duration_ms=1,
            )

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
    assert persisted.status is JobStatus.FAILED
    assert persisted.error_code == "internal_error"


@pytest.mark.asyncio
async def test_cancellation_marks_running_job_cancelled_and_propagates(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    entered = asyncio.Event()

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="slow-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(Provider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    task = asyncio.create_task(service.execute_job(job.id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status.value == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_persistence_exhaustion_requires_worker_recovery(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind
    entered = asyncio.Event()
    fail_cancellation = False
    failed_commits = 0

    def fail_three_cancellation_commits(connection) -> None:
        del connection
        nonlocal failed_commits
        if fail_cancellation and failed_commits < 3:
            failed_commits += 1
            raise OSError("postgresql://worker:CANCEL-SECRET@database/grader")

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="cancel-outage-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            del request
            entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    event.listen(database_engine.sync_engine, "commit", fail_three_cancellation_commits)
    try:
        service = EvaluationService(
            postgres_session,
            EvaluationEngine(Provider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        job = await service.request(submission.id, JobReason.INITIAL)
        execution = asyncio.create_task(service.execute_job(job.id))
        await entered.wait()
        fail_cancellation = True
        execution.cancel()
        with pytest.raises(EvaluationRecoveryRequired, match="evaluation recovery required"):
            await execution
    finally:
        event.remove(database_engine.sync_engine, "commit", fail_three_cancellation_commits)

    assert failed_commits == 3
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.RUNNING
    assert persisted.execution_token is not None


@pytest.mark.asyncio
async def test_withdrawal_evidence_persistence_exhaustion_requires_recovery_then_redelivery(
    postgres_session,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    database_engine = postgres_session.bind
    fail_evidence = False
    failed_commits = 0

    def fail_three_evidence_commits(connection) -> None:
        del connection
        nonlocal failed_commits
        if fail_evidence and failed_commits < 3:
            failed_commits += 1
            raise OSError("postgresql://worker:EVIDENCE-SECRET@database/grader")

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="withdraw-outage-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            del request
            nonlocal fail_evidence
            async with database_engine.begin() as connection:
                await connection.execute(
                    update(Submission)
                    .where(Submission.id == submission.id)
                    .values(status=SubmissionStatus.WITHDRAWN)
                )
            fail_evidence = True
            return ProviderResult(
                provider="scripted",
                model="withdraw-outage-v1",
                raw_text=(
                    files("fixtures.agent_outputs").joinpath("complete.json").read_text("utf-8")
                ),
                duration_ms=1,
            )

    event.listen(database_engine.sync_engine, "commit", fail_three_evidence_commits)
    try:
        service = EvaluationService(
            postgres_session,
            EvaluationEngine(Provider()),
            requested_by=teacher.id,
            dispatch=None,
        )
        job = await service.request(submission.id, JobReason.INITIAL)
        with pytest.raises(EvaluationRecoveryRequired, match="evaluation recovery required"):
            await service.execute_job(job.id)
    finally:
        event.remove(database_engine.sync_engine, "commit", fail_three_evidence_commits)

    assert failed_commits == 3
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status is JobStatus.RUNNING
    assert persisted.execution_token is not None
    assert (
        await postgres_session.scalar(select(AuditLog.id).where(AuditLog.job_id == job.id)) is None
    )

    redelivery = EvaluationService.for_persisted_job(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        job_id=job.id,
        requested_by=teacher.id,
        redelivered=True,
        notification=None,
    )
    with pytest.raises(EvaluationJobFailed, match="evaluation failed"):
        await redelivery.execute_job(job.id)
    await postgres_session.rollback()
    recovered = await postgres_session.get(EvaluationJob, job.id)
    assert recovered.status is JobStatus.CANCELLED
    assert recovered.execution_token is None


@pytest.mark.asyncio
async def test_worker_lost_redelivery_reclaims_only_a_stale_running_job(postgres_session) -> None:
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
            status="running",
            execution_token=uuid.uuid4(),
            execution_generation=1,
            queued_at=datetime.now(UTC) - timedelta(hours=2),
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await postgres_session.commit()

    report = await service.execute_job(job.id)
    assert report.job_id == job.id
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    assert persisted.status.value == "succeeded"


@pytest.mark.asyncio
async def test_tampered_job_identity_fails_configuration_before_provider(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    provider = MockEvaluationProvider()
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(provider),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    await postgres_session.execute(
        update(EvaluationJob).where(EvaluationJob.id == job.id).values(model="tampered-v1")
    )
    await postgres_session.commit()

    with pytest.raises(EvaluationJobFailed):
        await service.execute_job(job.id)
    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationJob, job.id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert persisted.status.value == "failed"
    assert persisted.error_code == "configuration"
    assert persisted.attempt_count == 0
    assert audit.retry_count == 0
    assert audit.validation_failures == []


@pytest.mark.asyncio
async def test_provider_configuration_failure_is_zero_attempt_terminal(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)

    class Provider:
        identity = ProviderIdentity(provider="scripted", model="config-v1")

        async def evaluate(self, request: EvaluationRequest) -> ProviderResult:
            raise ProviderUnavailable(
                "do not persist this detail",
                code=ProviderErrorCode.CONFIGURATION,
            )

    service = EvaluationService(
        postgres_session,
        EvaluationEngine(Provider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    with pytest.raises(EvaluationJobFailed):
        await service.evaluate_now(submission.id, JobReason.INITIAL)
    await postgres_session.rollback()
    job = await service.get_latest_job(submission.id)
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert job.error_code == "configuration"
    assert job.error_message == "evaluation provider configuration is invalid"
    assert job.attempt_count == 0
    assert audit.retry_count == 0


@pytest.mark.asyncio
async def test_report_and_job_roll_back_together_when_audit_flush_fails(
    postgres_session,
    monkeypatch,
) -> None:
    import app.evaluations.service as service_module

    teacher, _, _, submission = await _seed_subject(postgres_session)
    original_append = service_module.append_audit_log
    calls = 0

    async def fail_first_audit(session, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected audit failure")
        return await original_append(session, data)

    monkeypatch.setattr(service_module, "append_audit_log", fail_first_audit)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    with pytest.raises(EvaluationJobFailed):
        await service.evaluate_now(submission.id, JobReason.INITIAL)

    await postgres_session.rollback()
    job = await service.get_latest_job(submission.id)
    assert job.status.value == "failed"
    assert job.error_code == "internal_error"
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert audit.error_type == "internal_error"


@pytest.mark.asyncio
async def test_terminal_commit_failure_does_not_leave_a_partial_report(postgres_session) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    commit_count = 0

    def fail_second_commit(connection) -> None:
        del connection
        nonlocal commit_count
        commit_count += 1
        # Advisory-lock acquisition commits its lock SELECT before the claim and
        # terminal transactions, so the terminal commit is now the third commit.
        if commit_count == 3:
            raise RuntimeError("injected terminal commit failure")

    event.listen(postgres_session.bind.sync_engine, "commit", fail_second_commit)
    try:
        with pytest.raises(EvaluationJobFailed):
            await service.execute_job(job.id)
    finally:
        event.remove(postgres_session.bind.sync_engine, "commit", fail_second_commit)

    await postgres_session.rollback()
    persisted = await service.get_latest_job(submission.id)
    assert persisted.status.value == "failed"
    assert persisted.error_code == "internal_error"
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.id).where(EvaluationReport.job_id == job.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_celery_eager_task_accepts_only_json_uuid_and_runs_mock_job(
    postgres_session,
    monkeypatch,
) -> None:
    from app.core.config import get_settings
    from app.evaluations.worker import celery_app, run_evaluation

    teacher, _, _, submission = await _seed_subject(postgres_session)
    service = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await service.request(submission.id, JobReason.INITIAL)
    database_url = render_test_database_url(postgres_session.bind.url)
    monkeypatch.setenv("DATABASE_URL", database_url)
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
    assert persisted.status.value == "succeeded"

    with pytest.raises(ValueError, match="UUID string"):
        run_evaluation.run(job.id)
    get_settings.cache_clear()
