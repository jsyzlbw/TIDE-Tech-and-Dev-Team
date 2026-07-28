from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import Grade
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.providers.mock import MockEvaluationProvider
from app.evaluations.service import EvaluationService
from app.evaluations.types import JobReason, JobStatus, ReportOrigin, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.schemas import ReevaluateRequest
from app.reviews.service import ReviewService
from app.reviews.types import ReviewActionType
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


async def _initial_report(
    session: AsyncSession,
) -> tuple[User, EvaluationService, EvaluationReport]:
    teacher, _, _, submission = await _seed_subject(session)
    evaluation = EvaluationService(
        session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    report = await evaluation.evaluate_now(submission.id, JobReason.INITIAL)
    return teacher, evaluation, report


def _confirm_action(report: EvaluationReport, teacher: User) -> ReviewAction:
    return ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={"review_status": {"before": "proposed", "after": "confirmed"}},
        comment="",
    )


async def _queued_reevaluation(
    session: AsyncSession,
) -> tuple[User, EvaluationReport, EvaluationJob]:
    teacher, evaluation, report = await _initial_report(session)
    job = await ReviewService(session).reevaluate(
        report.id,
        teacher.id,
        ReevaluateRequest(comment="retry"),
        evaluation,
    )
    return teacher, report, job


@pytest.mark.asyncio
async def test_constraint_triggers_are_deferred_row_triggers(
    postgres_session: AsyncSession,
) -> None:
    rows = (
        await postgres_session.execute(
            text(
                """
                SELECT tgname, tgdeferrable, tginitdeferred, NOT tgisinternal AS user_defined
                FROM pg_trigger
                WHERE tgname IN (
                    'trg_review_actions_consistency',
                    'trg_evaluation_reports_review_consistency',
                    'trg_evaluation_jobs_review_consistency'
                )
                ORDER BY tgname
                """
            )
        )
    ).all()

    assert rows == [
        ("trg_evaluation_jobs_review_consistency", True, True, True),
        ("trg_evaluation_reports_review_consistency", True, True, True),
        ("trg_review_actions_consistency", True, True, True),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_status",
    [ReviewStatus.CONFIRMED, ReviewStatus.SUPERSEDED],
)
async def test_direct_status_change_without_action_fails_at_commit(
    postgres_session: AsyncSession,
    target_status: ReviewStatus,
) -> None:
    _, _, report = await _initial_report(postgres_session)

    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=target_status)
    )
    with pytest.raises(DBAPIError, match="review transition lacks matching evidence"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_historical_report_cannot_be_confirmed_with_a_late_action(
    postgres_session: AsyncSession,
) -> None:
    teacher, evaluation, historical = await _initial_report(postgres_session)
    current = await evaluation.evaluate_now(
        historical.submission_id,
        JobReason.PROVIDER_RETRY,
    )
    assert current.version == historical.version + 1

    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == historical.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    postgres_session.add(_confirm_action(historical, teacher))
    with pytest.raises(DBAPIError, match="confirmation requires the current report"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_confirm_cannot_race_an_existing_pending_reevaluation(
    postgres_session: AsyncSession,
) -> None:
    teacher, report, _ = await _queued_reevaluation(postgres_session)

    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    postgres_session.add(_confirm_action(report, teacher))
    with pytest.raises(DBAPIError, match="confirmation conflicts with pending re-evaluation"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_modify_cannot_bypass_an_existing_pending_reevaluation(
    postgres_session: AsyncSession,
) -> None:
    teacher, source, _ = await _queued_reevaluation(postgres_session)
    result = EvaluationReport(
        submission_id=source.submission_id,
        job_id=None,
        source_report_id=source.id,
        origin=ReportOrigin.TEACHER,
        created_by_teacher_id=teacher.id,
        version=source.version + 1,
        schema_version=source.schema_version,
        completeness=source.completeness,
        correctness=source.correctness,
        major_issues=source.major_issues,
        suggestions=source.suggestions,
        score=94,
        grade=Grade.A,
        confidence=source.confidence,
        limitations=source.limitations,
        raw_model_output=None,
        validation_status=source.validation_status,
        review_status=ReviewStatus.MODIFIED,
    )
    postgres_session.add(result)
    await postgres_session.flush([result])
    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == source.id)
        .values(review_status=ReviewStatus.SUPERSEDED)
    )
    postgres_session.add(
        ReviewAction(
            report_id=source.id,
            teacher_id=teacher.id,
            action=ReviewActionType.MODIFY,
            changes={
                "review_status": {"before": "proposed", "after": "superseded"},
                "result_report_id": {"before": None, "after": str(result.id)},
                "score": {"before": source.score, "after": 94},
                "grade": {"before": source.grade.value, "after": "A"},
            },
            comment="",
        )
    )

    with pytest.raises(DBAPIError, match="modification conflicts with pending re-evaluation"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_manual_retry_job_without_same_transaction_action_is_rejected(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, report = await _initial_report(postgres_session)
    postgres_session.add(
        EvaluationJob(
            submission_id=report.submission_id,
            requested_by=teacher.id,
            source_report_id=report.id,
            reason=JobReason.MANUAL_RETRY,
            status=JobStatus.QUEUED,
            idempotency_key=uuid.uuid4().hex + uuid.uuid4().hex,
            attempt_count=0,
            provider="mock",
            model="fixture-v1",
        )
    )

    with pytest.raises(DBAPIError, match="manual retry lacks matching review action"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_teacher_modified_report_without_action_is_rejected(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, source = await _initial_report(postgres_session)
    result = EvaluationReport(
        submission_id=source.submission_id,
        job_id=None,
        source_report_id=source.id,
        origin=ReportOrigin.TEACHER,
        created_by_teacher_id=teacher.id,
        version=source.version + 1,
        schema_version=source.schema_version,
        completeness=source.completeness,
        correctness=source.correctness,
        major_issues=source.major_issues,
        suggestions=source.suggestions,
        score=source.score,
        grade=source.grade,
        confidence=source.confidence,
        limitations=source.limitations,
        raw_model_output=None,
        validation_status=source.validation_status,
        review_status=ReviewStatus.MODIFIED,
    )
    postgres_session.add(result)

    with pytest.raises(DBAPIError, match="teacher report lacks matching modify action"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_successful_reevaluation_requires_exact_new_report_and_audit(
    postgres_session: AsyncSession,
) -> None:
    _, report, job = await _queued_reevaluation(postgres_session)
    now = datetime.now(UTC)
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED,
            attempt_count=1,
            started_at=now,
            finished_at=now,
        )
    )
    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.SUPERSEDED)
    )

    with pytest.raises(DBAPIError, match="successful re-evaluation lacks exact report evidence"):
        await postgres_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [JobStatus.FAILED, JobStatus.CANCELLED])
async def test_unsuccessful_reevaluation_cannot_supersede_source(
    postgres_session: AsyncSession,
    terminal_status: JobStatus,
) -> None:
    _, report, job = await _queued_reevaluation(postgres_session)
    values: dict[str, object] = {
        "status": terminal_status,
        "finished_at": datetime.now(UTC),
    }
    if terminal_status is JobStatus.FAILED:
        values.update(
            attempt_count=1,
            error_code="internal_error",
            error_message="evaluation failed",
        )
    await postgres_session.execute(
        update(EvaluationJob).where(EvaluationJob.id == job.id).values(**values)
    )
    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.SUPERSEDED)
    )

    with pytest.raises(DBAPIError, match="unsuccessful re-evaluation changed source status"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_failed_reevaluation_preserves_the_recorded_source_status(
    postgres_session: AsyncSession,
) -> None:
    teacher, report, job = await _queued_reevaluation(postgres_session)
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(status=JobStatus.CANCELLED, finished_at=datetime.now(UTC))
    )
    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    postgres_session.add(_confirm_action(report, teacher))

    with pytest.raises(DBAPIError, match="unsuccessful re-evaluation changed source status"):
        await postgres_session.commit()


@pytest.mark.asyncio
async def test_deferred_constraints_preserve_cross_transaction_worker_success(
    postgres_session: AsyncSession,
) -> None:
    teacher, report, job = await _queued_reevaluation(postgres_session)
    await postgres_session.rollback()
    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    result = await evaluation.execute_job(job.id)

    assert result.version == report.version + 1
    assert result.job_id == job.id
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
        )
        is ReviewStatus.SUPERSEDED
    )
