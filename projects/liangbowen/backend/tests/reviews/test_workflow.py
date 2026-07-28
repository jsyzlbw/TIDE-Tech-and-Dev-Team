from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.types import Grade, Role
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationJobFailed, EvaluationService
from app.evaluations.types import JobReason, ReportOrigin, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.schemas import ConfirmRequest, ReevaluateRequest, ReportPatch
from app.reviews.service import ReviewConflict, ReviewForbidden, ReviewService
from app.reviews.types import ReviewActionType
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


@contextmanager
def _reject_review_action_inserts(engine: AsyncEngine):
    def reject(connection, cursor, statement, parameters, context, executemany):
        del connection, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("INSERT INTO REVIEW_ACTIONS"):
            raise RuntimeError("injected review action insert failure")

    event.listen(engine.sync_engine, "before_cursor_execute", reject)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject)


async def _agent_report(session: AsyncSession, teacher_id: uuid.UUID, submission_id: uuid.UUID):
    return await EvaluationService(
        session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher_id,
        dispatch=None,
    ).evaluate_now(submission_id, JobReason.INITIAL)


def test_review_request_schemas_are_strict_and_normalize_comments() -> None:
    assert ConfirmRequest(comment="line one\r\n<b>line two</b>").comment == (
        "line one\n<b>line two</b>"
    )
    assert ReportPatch(score=94).score == 94

    invalid_payloads = (
        {},
        {"score": True},
        {"score": 101},
        {"grade": "A"},
        {"unknown": "value"},
        {"comment": "x" * 4_001},
        {"comment": "😀" * 2_001},
        {"comment": "contains\x00null"},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            ReportPatch.model_validate(payload)
    with pytest.raises(ValidationError):
        ConfirmRequest.model_validate({"unknown": True})
    with pytest.raises(ValidationError):
        ConfirmRequest(comment="contains\x00null")


@pytest.mark.asyncio
async def test_confirm_is_atomic_and_same_teacher_idempotent(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    service = ReviewService(postgres_session)

    first = await service.confirm(report.id, teacher.id, ConfirmRequest(comment="已核验"))
    duplicate = await service.confirm(report.id, teacher.id, ConfirmRequest(comment="忽略重复备注"))

    assert first.id == duplicate.id == report.id
    assert first.review_status is ReviewStatus.CONFIRMED
    actions = (
        (
            await postgres_session.execute(
                select(ReviewAction).where(ReviewAction.report_id == report.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(actions) == 1
    assert actions[0].teacher_id == teacher.id
    assert actions[0].action is ReviewActionType.CONFIRM
    assert actions[0].comment == "已核验"
    assert actions[0].changes == {"review_status": {"before": "proposed", "after": "confirmed"}}


@pytest.mark.asyncio
async def test_confirm_by_another_teacher_conflicts(postgres_session: AsyncSession) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    other_teacher = User(
        username=f"other-teacher-{uuid.uuid4().hex}",
        display_name="Other teacher",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=True,
    )
    postgres_session.add(other_teacher)
    await postgres_session.commit()
    report = await _agent_report(postgres_session, teacher.id, submission.id)
    service = ReviewService(postgres_session)

    await service.confirm(report.id, teacher.id, ConfirmRequest())
    with pytest.raises(ReviewConflict):
        await service.confirm(report.id, other_teacher.id, ConfirmRequest())


@pytest.mark.asyncio
async def test_modify_clones_unchanged_evidence_and_records_field_diff(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    source_id = source.id
    service = ReviewService(postgres_session)

    modified = await service.modify(
        source_id,
        teacher.id,
        ReportPatch(
            score=94,
            limitations=["未运行程序。"],
            comment="<b>原样保存</b>",
        ),
    )

    await postgres_session.rollback()
    persisted_source = await postgres_session.get(EvaluationReport, source_id)
    action = await postgres_session.scalar(
        select(ReviewAction).where(
            ReviewAction.report_id == source_id,
            ReviewAction.action == ReviewActionType.MODIFY,
        )
    )
    assert modified.version == source.version + 1
    assert modified.origin is ReportOrigin.TEACHER
    assert modified.source_report_id == source_id
    assert modified.created_by_teacher_id == teacher.id
    assert modified.review_status is ReviewStatus.MODIFIED
    assert modified.score == 94
    assert modified.grade is Grade.A
    assert modified.completeness == source.completeness
    assert modified.correctness == source.correctness
    assert modified.major_issues == source.major_issues
    assert modified.suggestions == source.suggestions
    assert modified.confidence == source.confidence
    assert modified.validation_status == source.validation_status
    assert modified.raw_model_output is None
    assert modified.limitations == ["未运行程序。"]
    assert persisted_source is not None
    assert persisted_source.review_status is ReviewStatus.SUPERSEDED
    assert action is not None
    assert action.comment == "<b>原样保存</b>"
    assert action.changes["score"] == {"before": 72, "after": 94}
    assert action.changes["grade"] == {"before": "C", "after": "A"}
    assert action.changes["review_status"] == {
        "before": "proposed",
        "after": "superseded",
    }
    assert action.changes["result_report_id"] == {
        "before": None,
        "after": str(modified.id),
    }


@pytest.mark.asyncio
async def test_modify_preserves_large_valid_before_evidence_in_action(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    base = await _agent_report(postgres_session, teacher.id, submission.id)
    job = EvaluationJob(
        submission_id=submission.id,
        requested_by=teacher.id,
        source_report_id=None,
        reason=JobReason.PROVIDER_RETRY,
        status="queued",
        idempotency_key=uuid.uuid4().hex + uuid.uuid4().hex,
        attempt_count=0,
        provider="mock",
        model="fixture-v1",
    )
    postgres_session.add(job)
    await postgres_session.flush([job])
    large_issues = [
        {
            "code": f"LARGE_{index}",
            "title": f"Issue {index}",
            "evidence": "".join(uuid.uuid4().hex for _ in range(125)),
            "impact": "".join(uuid.uuid4().hex for _ in range(63))[:2_000],
        }
        for index in range(20)
    ]
    source = EvaluationReport(
        submission_id=submission.id,
        job_id=job.id,
        source_report_id=None,
        origin=ReportOrigin.AGENT,
        created_by_teacher_id=None,
        version=base.version + 1,
        schema_version=base.schema_version,
        completeness=base.completeness,
        correctness=base.correctness,
        major_issues=large_issues,
        suggestions=base.suggestions,
        score=base.score,
        grade=base.grade,
        confidence=base.confidence,
        limitations=base.limitations,
        raw_model_output="{}",
        validation_status=base.validation_status,
        review_status=ReviewStatus.PROPOSED,
    )
    postgres_session.add(source)
    await postgres_session.commit()

    modified = await ReviewService(postgres_session).modify(
        source.id,
        teacher.id,
        ReportPatch(
            major_issues=[
                {
                    "code": "FIXED",
                    "title": "Fixed",
                    "evidence": "Fixed evidence",
                    "impact": "Fixed impact",
                }
            ]
        ),
    )

    action_size = await postgres_session.scalar(
        select(func.pg_column_size(ReviewAction.changes)).where(
            ReviewAction.report_id == source.id,
            ReviewAction.action == ReviewActionType.MODIFY,
        )
    )
    assert modified.version == source.version + 1
    assert action_size is not None
    assert 65_536 < action_size <= 2_097_152


@pytest.mark.asyncio
async def test_modify_records_only_actual_score_difference_when_grade_is_unchanged(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)

    modified = await ReviewService(postgres_session).modify(
        source.id,
        teacher.id,
        ReportPatch(score=74),
    )

    action = await postgres_session.scalar(
        select(ReviewAction).where(
            ReviewAction.report_id == source.id,
            ReviewAction.action == ReviewActionType.MODIFY,
        )
    )
    assert modified.grade is source.grade is Grade.C
    assert action is not None
    assert action.changes["score"] == {"before": 72, "after": 74}
    assert "grade" not in action.changes


@pytest.mark.asyncio
async def test_inactive_teacher_and_historical_report_are_rejected(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    service = ReviewService(postgres_session)
    teacher.is_active = False
    await postgres_session.commit()

    with pytest.raises(ReviewForbidden):
        await service.confirm(source.id, teacher.id, ConfirmRequest())

    teacher.is_active = True
    await postgres_session.commit()
    newer = await service.modify(
        source.id,
        teacher.id,
        ReportPatch(score=88, comment="large score adjustment"),
    )
    with pytest.raises(ReviewConflict):
        await service.confirm(source.id, teacher.id, ConfirmRequest())
    assert newer.version == 2
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationReport)
            .where(EvaluationReport.submission_id == submission.id)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_reevaluate_commits_job_outbox_and_action_once_then_supersedes_on_success(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    teacher_id = teacher.id
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    observed: list[uuid.UUID] = []

    async def dispatch(job_id: uuid.UUID) -> None:
        async with postgres_session.bind.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count(EvaluationJob.id)).where(EvaluationJob.id == job_id)
                )
                == 1
            )
            assert (
                await connection.scalar(
                    select(func.count(EvaluationOutbox.id)).where(EvaluationOutbox.job_id == job_id)
                )
                == 1
            )
            assert (
                await connection.scalar(
                    select(func.count(ReviewAction.id)).where(
                        ReviewAction.changes["evaluation_job_id"]["after"].as_string()
                        == str(job_id)
                    )
                )
                == 1
            )
        observed.append(job_id)

    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=dispatch,
    )
    reviews = ReviewService(postgres_session)
    job = await reviews.reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="重新评分"),
        evaluation,
    )
    duplicate = await reviews.reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="重复请求"),
        evaluation,
    )

    assert duplicate.id == job.id
    assert job.reason is JobReason.MANUAL_RETRY
    assert job.source_report_id == source.id
    assert observed == [job.id]
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == source.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 1
    )
    with pytest.raises(ReviewConflict, match="pending"):
        await reviews.confirm(source.id, teacher.id, ConfirmRequest())

    result = await evaluation.execute_job(job.id)
    await postgres_session.rollback()
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    assert result.version == source.version + 1
    assert persisted_source is not None
    assert persisted_source.review_status is ReviewStatus.SUPERSEDED

    duplicate_after_success = await reviews.reevaluate(
        source.id,
        teacher_id,
        ReevaluateRequest(comment="完成后重复请求"),
        evaluation,
    )
    assert duplicate_after_success.id == job.id
    assert observed == [job.id]


@pytest.mark.asyncio
async def test_failed_reevaluation_keeps_source_current(postgres_session: AsyncSession) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    failing_evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key=f"missing-{uuid.uuid4().hex}",
    )
    job = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(),
        failing_evaluation,
    )

    with pytest.raises(EvaluationJobFailed):
        await failing_evaluation.execute_job(job.id)

    await postgres_session.rollback()
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    persisted_job = await postgres_session.get(EvaluationJob, job.id)
    assert persisted_source is not None
    assert persisted_source.review_status is ReviewStatus.PROPOSED
    assert persisted_job is not None
    assert persisted_job.status.value == "failed"


@pytest.mark.asyncio
async def test_failed_reevaluation_can_append_a_new_action_and_eventually_succeed(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    failing = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
        mock_fixture_key=f"missing-{uuid.uuid4().hex}",
    )
    first = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="first attempt"),
        failing,
    )
    with pytest.raises(EvaluationJobFailed):
        await failing.execute_job(first.id)

    recovered = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    second = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="retry after outage"),
        recovered,
    )
    duplicate = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(comment="duplicate click"),
        recovered,
    )
    result = await recovered.execute_job(second.id)

    assert second.id == duplicate.id
    assert second.id != first.id
    assert result.job_id == second.id
    await postgres_session.rollback()
    jobs = (
        (
            await postgres_session.execute(
                select(EvaluationJob)
                .where(
                    EvaluationJob.source_report_id == source.id,
                    EvaluationJob.reason == JobReason.MANUAL_RETRY,
                )
                .order_by(EvaluationJob.queued_at, EvaluationJob.id)
            )
        )
        .scalars()
        .all()
    )
    actions = (
        (
            await postgres_session.execute(
                select(ReviewAction)
                .where(
                    ReviewAction.report_id == source.id,
                    ReviewAction.action == ReviewActionType.REEVALUATE,
                )
                .order_by(ReviewAction.created_at, ReviewAction.id)
            )
        )
        .scalars()
        .all()
    )
    assert [job.status.value for job in jobs] == ["failed", "succeeded"]
    assert [action.comment for action in actions] == ["first attempt", "retry after outage"]


@pytest.mark.asyncio
async def test_reevaluation_cannot_overwrite_a_newer_report(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    job = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(),
        evaluation,
    )
    newer = await evaluation.evaluate_now(submission.id, JobReason.PROVIDER_RETRY)

    with pytest.raises(EvaluationJobFailed):
        await evaluation.execute_job(job.id)

    await postgres_session.rollback()
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    persisted_newer = await postgres_session.get(EvaluationReport, newer.id)
    persisted_job = await postgres_session.get(EvaluationJob, job.id)
    assert persisted_source is not None
    assert persisted_source.review_status is ReviewStatus.PROPOSED
    assert persisted_newer is not None
    assert persisted_newer.review_status is ReviewStatus.PROPOSED
    assert persisted_job is not None
    assert persisted_job.status.value == "failed"
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationReport)
            .where(EvaluationReport.submission_id == submission.id)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_reevaluation_broker_failure_leaves_durable_pending_outbox(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)

    async def unavailable_broker(job_id: uuid.UUID) -> None:
        del job_id
        raise RuntimeError("broker secret must not escape")

    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=unavailable_broker,
    )
    job = await ReviewService(postgres_session).reevaluate(
        source.id,
        teacher.id,
        ReevaluateRequest(),
        evaluation,
    )

    await postgres_session.rollback()
    outbox = await postgres_session.scalar(
        select(EvaluationOutbox).where(
            EvaluationOutbox.job_id == job.id,
            EvaluationOutbox.kind == "dispatch",
        )
    )
    assert outbox is not None
    assert outbox.delivered_at is None
    assert outbox.attempt_count == 1
    assert outbox.last_error_type == "RuntimeError"
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == source.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reevaluation_enqueue_failure_rolls_back_all_three_rows(
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    original = evaluation.enqueue_in_transaction

    async def fail_after_enqueue(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("planned enqueue failure")

    monkeypatch.setattr(evaluation, "enqueue_in_transaction", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="planned enqueue failure"):
        await ReviewService(postgres_session).reevaluate(
            source.id,
            teacher.id,
            ReevaluateRequest(),
            evaluation,
        )

    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(EvaluationJob)
            .where(EvaluationJob.source_report_id == source.id)
        )
        == 0
    )
    assert (
        await postgres_session.scalar(
            select(func.count())
            .select_from(ReviewAction)
            .where(
                ReviewAction.report_id == source.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
        )
        == 0
    )


@pytest.mark.asyncio
async def test_confirm_action_insert_failure_rolls_back_status_and_action(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)

    with (
        _reject_review_action_inserts(postgres_session.bind),
        pytest.raises(RuntimeError, match="injected review action insert failure"),
    ):
        await ReviewService(postgres_session).confirm(
            source.id,
            teacher.id,
            ConfirmRequest(comment="confirm"),
        )

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationReport, source.id)
    action_count = await postgres_session.scalar(
        select(func.count()).select_from(ReviewAction).where(ReviewAction.report_id == source.id)
    )
    assert persisted is not None
    assert persisted.review_status is ReviewStatus.PROPOSED
    assert action_count == 0


@pytest.mark.asyncio
async def test_modify_action_insert_failure_rolls_back_source_result_and_version(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)

    with (
        _reject_review_action_inserts(postgres_session.bind),
        pytest.raises(RuntimeError, match="injected review action insert failure"),
    ):
        await ReviewService(postgres_session).modify(
            source.id,
            teacher.id,
            ReportPatch(score=94, comment="modify"),
        )

    await postgres_session.rollback()
    persisted = await postgres_session.get(EvaluationReport, source.id)
    versions = (
        await postgres_session.scalars(
            select(EvaluationReport.version)
            .where(EvaluationReport.submission_id == submission.id)
            .order_by(EvaluationReport.version)
        )
    ).all()
    action_count = await postgres_session.scalar(
        select(func.count()).select_from(ReviewAction).where(ReviewAction.report_id == source.id)
    )
    assert persisted is not None
    assert persisted.review_status is ReviewStatus.PROPOSED
    assert versions == [source.version]
    assert action_count == 0


@pytest.mark.asyncio
async def test_reevaluate_action_insert_failure_rolls_back_job_outbox_and_dispatch(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    source = await _agent_report(postgres_session, teacher.id, submission.id)
    dispatched: list[uuid.UUID] = []
    baseline_outbox_count = await postgres_session.scalar(
        select(func.count()).select_from(EvaluationOutbox)
    )

    async def dispatch(job_id: uuid.UUID) -> None:
        dispatched.append(job_id)

    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=dispatch,
    )
    with (
        _reject_review_action_inserts(postgres_session.bind),
        pytest.raises(RuntimeError, match="injected review action insert failure"),
    ):
        await ReviewService(postgres_session).reevaluate(
            source.id,
            teacher.id,
            ReevaluateRequest(comment="retry"),
            evaluation,
        )

    await postgres_session.rollback()
    job_count = await postgres_session.scalar(
        select(func.count())
        .select_from(EvaluationJob)
        .where(EvaluationJob.source_report_id == source.id)
    )
    outbox_count = await postgres_session.scalar(select(func.count()).select_from(EvaluationOutbox))
    action_count = await postgres_session.scalar(
        select(func.count()).select_from(ReviewAction).where(ReviewAction.report_id == source.id)
    )
    persisted = await postgres_session.get(EvaluationReport, source.id)
    assert job_count == 0
    assert outbox_count == baseline_outbox_count
    assert action_count == 0
    assert dispatched == []
    assert persisted is not None
    assert persisted.review_status is ReviewStatus.PROPOSED
