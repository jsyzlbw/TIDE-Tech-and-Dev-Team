from __future__ import annotations

import importlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.contracts.review_v1 import (
    CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL,
    CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL,
    CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL,
    CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL,
)
from app.db.types import Grade, Role
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.types import JobReason, ReportOrigin, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.types import ReviewActionType
from app.users.model import User
from tests.evaluations.test_jobs import _seed_subject


async def _persist_modify_pair(
    postgres_session: AsyncSession,
    *,
    score: int | None = None,
    grade: Grade | None = None,
    limitations: list[str] | None = None,
    confidence: Decimal | None = None,
) -> tuple[User, EvaluationReport, EvaluationReport]:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    source = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
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
        score=source.score if score is None else score,
        grade=source.grade if grade is None else grade,
        confidence=source.confidence if confidence is None else confidence,
        limitations=source.limitations if limitations is None else limitations,
        raw_model_output=None,
        validation_status=source.validation_status,
        review_status=ReviewStatus.MODIFIED,
    )
    postgres_session.add(result)
    await postgres_session.flush([result])
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    assert persisted_source is not None
    persisted_source.review_status = ReviewStatus.SUPERSEDED
    await postgres_session.flush([persisted_source])
    return teacher, persisted_source, result


def _required_modify_changes(result_id: uuid.UUID) -> dict[str, object]:
    return {
        "review_status": {"before": "proposed", "after": "superseded"},
        "result_report_id": {"before": None, "after": str(result_id)},
    }


def test_review_action_model_has_the_frozen_evidence_contract() -> None:
    table = ReviewAction.__table__

    assert set(table.columns.keys()) == {
        "id",
        "report_id",
        "teacher_id",
        "action",
        "changes",
        "comment",
        "created_at",
    }
    assert table.c.report_id.nullable is False
    assert table.c.teacher_id.nullable is False
    assert table.c.action.nullable is False
    assert table.c.changes.nullable is False
    assert table.c.comment.nullable is False
    assert {member.value for member in ReviewActionType} == {
        "confirm",
        "modify",
        "reevaluate",
    }
    assert all(
        constraint.name != "uq_review_actions_report_action" for constraint in table.constraints
    )
    non_retry_unique = next(
        index for index in table.indexes if index.name == "uq_review_actions_report_non_reevaluate"
    )
    assert non_retry_unique.unique is True
    predicate = non_retry_unique.dialect_options["postgresql"]["where"]
    assert predicate is not None
    assert "reevaluate" in str(
        predicate.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_review_action_validation_sql_is_identical_in_orm_and_migration() -> None:
    orm_sql = next(
        listener.statement
        for listener in ReviewAction.__table__.dispatch.after_create
        if "CREATE OR REPLACE FUNCTION validate_review_action()" in listener.statement
    )
    migration_sql = importlib.import_module(
        "migrations.versions.0004_reviews"
    ).VALIDATE_REVIEW_ACTION
    assert "".join(orm_sql.split()) == "".join(migration_sql.split())


def test_deferred_review_consistency_sql_is_identical_in_orm_and_migration() -> None:
    expected = {
        CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL,
        CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL,
        CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL,
        CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL,
    }
    orm_sql = {
        listener.statement
        for listener in ReviewAction.__table__.dispatch.after_create
        if "review_consistency" in listener.statement
    }
    migration = importlib.import_module("migrations.versions.0004_reviews")
    migration_sql = {
        migration.CREATE_REVIEW_CONSISTENCY_FUNCTION_SQL,
        migration.CREATE_REVIEW_ACTION_CONSTRAINT_TRIGGER_SQL,
        migration.CREATE_REVIEW_REPORT_CONSTRAINT_TRIGGER_SQL,
        migration.CREATE_REVIEW_JOB_CONSTRAINT_TRIGGER_SQL,
    }

    assert orm_sql == expected
    assert migration_sql == expected


def test_immediate_review_source_lock_sql_is_identical_in_orm_and_migration() -> None:
    expected = {
        CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL,
        CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL,
        CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL,
        CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL,
    }
    orm_sql = {
        listener.statement
        for listener in ReviewAction.__table__.dispatch.after_create
        if "source_lock" in listener.statement or "lock_review_source_report" in listener.statement
    }
    migration = importlib.import_module("migrations.versions.0004_reviews")
    migration_sql = {
        migration.CREATE_REVIEW_SOURCE_LOCK_FUNCTION_SQL,
        migration.CREATE_REVIEW_ACTION_SOURCE_LOCK_TRIGGER_SQL,
        migration.CREATE_REVIEW_REPORT_SOURCE_LOCK_TRIGGER_SQL,
        migration.CREATE_REVIEW_JOB_SOURCE_LOCK_TRIGGER_SQL,
    }

    assert orm_sql == expected
    assert migration_sql == expected


def test_manual_retry_job_model_persists_exact_source_report() -> None:
    table = EvaluationJob.__table__

    assert "source_report_id" in table.columns
    assert table.c.source_report_id.nullable is True
    assert any(
        constraint.name == "fk_evaluation_jobs_source_submission"
        for constraint in table.constraints
    )
    assert any(
        constraint.name == "ck_evaluation_jobs_manual_source" for constraint in table.constraints
    )


@pytest.mark.asyncio
async def test_review_action_requires_active_teacher_and_is_append_only(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    action = ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={
            "review_status": {
                "before": ReviewStatus.PROPOSED.value,
                "after": ReviewStatus.CONFIRMED.value,
            }
        },
        comment="已核验",
    )
    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    await postgres_session.flush()
    postgres_session.add(action)
    await postgres_session.commit()
    action_id = action.id

    stored = await postgres_session.get(ReviewAction, action_id)
    assert stored is not None
    assert stored.changes == action.changes

    with pytest.raises(DBAPIError, match="review actions are append-only"):
        await postgres_session.execute(
            update(ReviewAction).where(ReviewAction.id == action_id).values(comment="rewritten")
        )
    await postgres_session.rollback()

    with pytest.raises(DBAPIError, match="review actions are append-only"):
        await postgres_session.execute(delete(ReviewAction).where(ReviewAction.id == action_id))
    await postgres_session.rollback()

    inactive = User(
        username=f"inactive-reviewer-{uuid.uuid4().hex}",
        display_name="Inactive reviewer",
        role=Role.TEACHER,
        password_hash="hash",
        is_active=False,
    )
    postgres_session.add(inactive)
    await postgres_session.commit()
    postgres_session.add(
        ReviewAction(
            report_id=report.id,
            teacher_id=inactive.id,
            action=ReviewActionType.REEVALUATE,
            changes={"evaluation_job_id": {"before": None, "after": str(uuid.uuid4())}},
            comment="",
        )
    )
    with pytest.raises(IntegrityError, match="active teacher"):
        await postgres_session.flush()
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_manual_job_source_must_match_reason_and_submission(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)

    job = EvaluationJob(
        submission_id=submission.id,
        requested_by=teacher.id,
        reason=JobReason.MANUAL_RETRY,
        status="queued",
        idempotency_key=uuid.uuid4().hex + uuid.uuid4().hex,
        attempt_count=0,
        provider="mock",
        model="fixture-v1",
        source_report_id=report.id,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    assert job.source_report_id == report.id

    job.reason = JobReason.INITIAL
    with pytest.raises(IntegrityError, match="manual_source"):
        await postgres_session.flush()
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_review_linked_manual_job_identity_is_immutable_before_audit(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService
    from app.reviews.schemas import ReevaluateRequest
    from app.reviews.service import ReviewService

    evaluation = EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    )
    report = await evaluation.evaluate_now(submission.id, JobReason.INITIAL)
    job = await ReviewService(postgres_session).reevaluate(
        report.id,
        teacher.id,
        ReevaluateRequest(),
        evaluation,
    )

    await postgres_session.rollback()
    with pytest.raises(DBAPIError, match="review-linked evaluation job identity is immutable"):
        await postgres_session.execute(
            update(EvaluationJob)
            .where(EvaluationJob.id == job.id)
            .values(reason=JobReason.INITIAL, source_report_id=None)
        )
        await postgres_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_teacher_report_requires_an_active_teacher(postgres_session: AsyncSession) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    source = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
    teacher.is_active = False
    await postgres_session.commit()

    copied = EvaluationReport(
        submission_id=submission.id,
        source_report_id=source.id,
        origin="teacher",
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
    postgres_session.add(copied)
    with pytest.raises(IntegrityError, match="active teacher"):
        await postgres_session.flush()
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_review_status_rejects_undocumented_transitions(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)

    from app.reviews.schemas import ConfirmRequest
    from app.reviews.service import ReviewService

    await ReviewService(postgres_session).confirm(
        report.id,
        teacher.id,
        ConfirmRequest(comment="legal transition"),
    )
    await postgres_session.rollback()
    assert (
        await postgres_session.scalar(
            select(EvaluationReport.review_status).where(EvaluationReport.id == report.id)
        )
        is ReviewStatus.CONFIRMED
    )

    with pytest.raises(DBAPIError, match="review status transition"):
        await postgres_session.execute(
            update(EvaluationReport)
            .where(EvaluationReport.id == report.id)
            .values(review_status=ReviewStatus.PROPOSED)
        )
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_review_action_rejects_malformed_changes_oversized_comment_and_truncate(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    report = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)

    forged = ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={"review_status": {"before": "proposed", "after": "confirmed"}},
        comment="",
    )
    with pytest.raises(IntegrityError, match="invalid confirm review changes"):
        async with postgres_session.begin_nested():
            postgres_session.add(forged)
            await postgres_session.flush()

    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    await postgres_session.flush()

    malformed = ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={
            "review_status": {
                "before": "proposed",
                "after": "confirmed",
                "forged": "evidence",
            }
        },
        comment="",
    )
    with pytest.raises(IntegrityError, match="exact before and after"):
        async with postgres_session.begin_nested():
            postgres_session.add(malformed)
            await postgres_session.flush()

    oversized = ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={"review_status": {"before": "proposed", "after": "confirmed"}},
        comment="😀" * 2_001,
    )
    with pytest.raises(IntegrityError, match="ck_review_actions_comment_bytes"):
        async with postgres_session.begin_nested():
            postgres_session.add(oversized)
            await postgres_session.flush()

    too_many_characters = ReviewAction(
        report_id=report.id,
        teacher_id=teacher.id,
        action=ReviewActionType.CONFIRM,
        changes={"review_status": {"before": "proposed", "after": "confirmed"}},
        comment="x" * 4_001,
    )
    with pytest.raises(IntegrityError, match="ck_review_actions_comment_characters"):
        async with postgres_session.begin_nested():
            postgres_session.add(too_many_characters)
            await postgres_session.flush()

    with pytest.raises(DBAPIError, match="evaluation evidence cannot be truncated"):
        await postgres_session.execute(text("TRUNCATE review_actions"))
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_modify_action_rejects_field_evidence_that_disagrees_with_reports(
    postgres_session: AsyncSession,
) -> None:
    teacher, _, _, submission = await _seed_subject(postgres_session)
    from app.evaluations.engine import EvaluationEngine
    from app.evaluations.providers.mock import MockEvaluationProvider
    from app.evaluations.service import EvaluationService

    source = await EvaluationService(
        postgres_session,
        EvaluationEngine(MockEvaluationProvider()),
        requested_by=teacher.id,
        dispatch=None,
    ).evaluate_now(submission.id, JobReason.INITIAL)
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
    persisted_source = await postgres_session.get(EvaluationReport, source.id)
    assert persisted_source is not None
    persisted_source.review_status = ReviewStatus.SUPERSEDED
    await postgres_session.flush([persisted_source])

    forged = ReviewAction(
        report_id=source.id,
        teacher_id=teacher.id,
        action=ReviewActionType.MODIFY,
        changes={
            "review_status": {"before": "proposed", "after": "superseded"},
            "result_report_id": {"before": None, "after": str(result.id)},
            "score": {"before": 0, "after": 94},
            "grade": {"before": "F", "after": "A"},
        },
        comment="",
    )
    with pytest.raises(IntegrityError, match="modify"):
        postgres_session.add(forged)
        await postgres_session.flush()
    await postgres_session.rollback()


@pytest.mark.asyncio
async def test_modify_action_rejects_omitted_score_and_grade_differences(
    postgres_session: AsyncSession,
) -> None:
    teacher, source, result = await _persist_modify_pair(
        postgres_session,
        score=94,
        grade=Grade.A,
    )
    action = ReviewAction(
        report_id=source.id,
        teacher_id=teacher.id,
        action=ReviewActionType.MODIFY,
        changes=_required_modify_changes(result.id),
        comment="",
    )
    with pytest.raises(IntegrityError, match="modify"):
        postgres_session.add(action)
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_modify_action_rejects_immutable_evidence_divergence(
    postgres_session: AsyncSession,
) -> None:
    teacher, source, result = await _persist_modify_pair(
        postgres_session,
        confidence=Decimal("0.1234"),
    )
    action = ReviewAction(
        report_id=source.id,
        teacher_id=teacher.id,
        action=ReviewActionType.MODIFY,
        changes=_required_modify_changes(result.id),
        comment="",
    )
    with pytest.raises(IntegrityError, match="modify"):
        postgres_session.add(action)
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_modify_action_rejects_declared_but_unchanged_field(
    postgres_session: AsyncSession,
) -> None:
    teacher, source, result = await _persist_modify_pair(postgres_session)
    changes = _required_modify_changes(result.id)
    changes["score"] = {"before": source.score, "after": result.score}
    action = ReviewAction(
        report_id=source.id,
        teacher_id=teacher.id,
        action=ReviewActionType.MODIFY,
        changes=changes,
        comment="",
    )
    with pytest.raises(IntegrityError, match="invalid modify review changes"):
        postgres_session.add(action)
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_modify_action_rejects_one_omitted_difference_from_multiple_changes(
    postgres_session: AsyncSession,
) -> None:
    teacher, source, result = await _persist_modify_pair(
        postgres_session,
        score=94,
        grade=Grade.A,
        limitations=["changed limitation"],
    )
    changes = _required_modify_changes(result.id)
    changes.update(
        {
            "score": {"before": source.score, "after": result.score},
            "grade": {"before": source.grade.value, "after": result.grade.value},
        }
    )
    action = ReviewAction(
        report_id=source.id,
        teacher_id=teacher.id,
        action=ReviewActionType.MODIFY,
        changes=changes,
        comment="",
    )
    with pytest.raises(IntegrityError, match="invalid modify review changes"):
        postgres_session.add(action)
        await postgres_session.flush()
