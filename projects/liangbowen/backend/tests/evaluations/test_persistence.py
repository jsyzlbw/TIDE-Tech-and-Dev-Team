from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.assignments.model import Assignment
from app.audit.model import AuditLog
from app.audit.service import AuditLogCreate, append_audit_log
from app.db.types import (
    AssignmentStatus,
    Grade,
    Role,
    SubmissionContentType,
    SubmissionSource,
    SubmissionStatus,
)
from app.evaluations.model import EvaluationJob, EvaluationOutbox, EvaluationReport
from app.evaluations.providers.base import RepairErrorCode
from app.evaluations.types import (
    JobReason,
    JobStatus,
    ReportOrigin,
    ReviewStatus,
    ValidationStatus,
)
from app.submissions.model import Submission
from app.users.model import User


def _ids() -> dict[str, uuid.UUID]:
    return {
        name: uuid.uuid4()
        for name in (
            "teacher",
            "student",
            "assignment",
            "submission",
            "other_submission",
            "job",
            "report",
        )
    }


async def _domain_rows(session: AsyncSession) -> dict[str, uuid.UUID]:
    ids = _ids()
    now = datetime.now(UTC)
    session.add_all(
        [
            User(
                id=ids["teacher"],
                username=f"teacher-{uuid.uuid4().hex}",
                display_name="Teacher",
                role=Role.TEACHER,
                password_hash="hash",
            ),
            User(
                id=ids["student"],
                username=f"student-{uuid.uuid4().hex}",
                display_name="Student",
                role=Role.STUDENT,
                password_hash="hash",
            ),
        ]
    )
    await session.flush()
    assignment = Assignment(
        id=ids["assignment"],
        code=f"HW-{uuid.uuid4().hex[:12]}",
        title="Shortest path",
        question="Explain Dijkstra.",
        notes="",
        rubric={"required_points": ["relaxation"]},
        due_at=now + timedelta(days=1),
        status=AssignmentStatus.PUBLISHED,
        created_by=ids["teacher"],
        published_at=now,
    )
    session.add(assignment)
    await session.flush()
    session.add_all(
        [
            Submission(
                id=ids[name],
                assignment_id=ids["assignment"],
                student_id=ids["student"],
                version=version,
                content_type=SubmissionContentType.TEXT,
                content_text=f"answer {version}",
                status=SubmissionStatus.SUBMITTED,
                submitted_at=now,
                source=SubmissionSource.WEB,
            )
            for name, version in (("submission", 1), ("other_submission", 2))
        ]
    )
    await session.flush()
    return ids


def _job(ids: dict[str, uuid.UUID], **overrides: object) -> EvaluationJob:
    values: dict[str, object] = {
        "id": ids["job"],
        "submission_id": ids["submission"],
        "requested_by": ids["teacher"],
        "reason": JobReason.INITIAL,
        "status": JobStatus.QUEUED,
        "idempotency_key": uuid.uuid4().hex + uuid.uuid4().hex,
        "attempt_count": 0,
        "provider": "mock",
        "model": "fixture-v1",
    }
    values.update(overrides)
    return EvaluationJob(**values)


def _job_insert_values(ids: dict[str, uuid.UUID], **overrides: object) -> dict[str, object]:
    job = _job(ids, **overrides)
    return {
        column.key: getattr(job, column.key)
        for column in EvaluationJob.__table__.columns
        if column.key in job.__dict__
    }


def _report(ids: dict[str, uuid.UUID], **overrides: object) -> EvaluationReport:
    values: dict[str, object] = {
        "id": ids["report"],
        "submission_id": ids["submission"],
        "job_id": ids["job"],
        "origin": ReportOrigin.AGENT,
        "version": 1,
        "schema_version": "1.0",
        "completeness": {
            "level": "partial",
            "covered_points": ["relaxation"],
            "missing_points": ["complexity"],
            "rationale": "Partial answer.",
        },
        "correctness": {"judgment": "mostly_correct", "rationale": "Good direction."},
        "major_issues": [],
        "suggestions": [{"priority": "high", "action": "Add complexity.", "example": ""}],
        "score": 72,
        "grade": Grade.C,
        "confidence": Decimal("0.8600"),
        "limitations": ["Text-only evaluation."],
        "raw_model_output": '{"score":72}',
        "validation_status": ValidationStatus.VALID,
        "review_status": ReviewStatus.PROPOSED,
    }
    values.update(overrides)
    return EvaluationReport(**values)


def _report_insert_values(ids: dict[str, uuid.UUID], **overrides: object) -> dict[str, object]:
    report = _report(ids, **overrides)
    return {
        column.key: getattr(report, column.key)
        for column in EvaluationReport.__table__.columns
        if column.key in report.__dict__
    }


def _failed_audit_insert_values(
    ids: dict[str, uuid.UUID],
    job: EvaluationJob,
    *,
    retry_count: int,
    failures: list[str],
) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "job_id": job.id,
        "submission_id": ids["submission"],
        "provider": job.provider,
        "model": job.model,
        "prompt_template_version": "grading-v1",
        "schema_version": "1.0",
        "duration_ms": 1,
        "retry_count": retry_count,
        "raw_model_output": None,
        "validation_status": None,
        "validation_failures": failures,
        "error_type": job.error_code,
        "error_message": job.error_message,
        "final_report_id": None,
    }


@pytest.mark.asyncio
async def test_agent_and_teacher_report_origin_contract(postgres_session: AsyncSession) -> None:
    ids = await _domain_rows(postgres_session)
    job = _job(ids)
    postgres_session.add(job)
    await postgres_session.flush()
    agent_report = _report(ids)
    postgres_session.add(agent_report)
    await postgres_session.flush()

    teacher_report = _report(
        ids,
        id=uuid.uuid4(),
        job_id=None,
        source_report_id=agent_report.id,
        origin=ReportOrigin.TEACHER,
        created_by_teacher_id=ids["teacher"],
        version=2,
        raw_model_output=None,
        review_status=ReviewStatus.MODIFIED,
    )
    postgres_session.add(teacher_report)
    await postgres_session.flush()

    assert agent_report.origin is ReportOrigin.AGENT
    assert agent_report.job_id == job.id
    assert agent_report.created_by_teacher_id is None
    assert teacher_report.origin is ReportOrigin.TEACHER
    assert teacher_report.source_report_id == agent_report.id
    assert teacher_report.job_id is None
    assert teacher_report.raw_model_output is None
    assert agent_report.created_at.tzinfo is not None
    assert job.queued_at.tzinfo is not None


@pytest.mark.asyncio
async def test_direct_sql_cancelled_audit_requires_exact_provider_evidence(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    await postgres_session.execute(
        insert(EvaluationJob).values(
            _job_insert_values(
                ids,
                status=JobStatus.CANCELLED,
                attempt_count=1,
                queued_at=now,
                started_at=now,
                finished_at=now,
            )
        )
    )
    values = {
        "id": uuid.uuid4(),
        "job_id": ids["job"],
        "submission_id": ids["submission"],
        "provider": "mock",
        "model": "fixture-v1",
        "prompt_template_version": "evaluation-v1",
        "schema_version": "1.0",
        "duration_ms": 7,
        "retry_count": 0,
        "raw_model_output": '{"score":72}',
        "validation_status": None,
        "validation_failures": [],
        "error_type": "cancelled",
        "error_message": "evaluation cancelled after provider execution",
        "final_report_id": None,
    }
    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(AuditLog).values(
                    values | {"id": uuid.uuid4(), "error_message": "withdrawn secret"}
                )
            )
    await postgres_session.execute(insert(AuditLog).values(values))
    await postgres_session.flush()
    audit = await postgres_session.scalar(select(AuditLog).where(AuditLog.job_id == ids["job"]))
    assert audit.error_type == "cancelled"
    assert audit.raw_model_output == '{"score":72}'


def test_cancelled_audit_application_contract_requires_nonempty_provider_output() -> None:
    values = {
        "job_id": uuid.uuid4(),
        "submission_id": uuid.uuid4(),
        "provider": "mock",
        "model": "fixture-v1",
        "prompt_template_version": "evaluation-v1",
        "schema_version": "1.0",
        "duration_ms": 7,
        "retry_count": 0,
        "validation_status": None,
        "validation_failures": (),
        "error_type": "cancelled",
        "error_message": "evaluation cancelled after provider execution",
        "final_report_id": None,
    }
    for raw_output in (None, ""):
        with pytest.raises((TypeError, ValueError), match="raw_model_output"):
            AuditLogCreate(raw_model_output=raw_output, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"raw_model_output": None},
        {"raw_model_output": ""},
        {"provider": "other-provider"},
        {"model": "other-model"},
        {"duration_ms": -1},
    ],
)
async def test_direct_sql_cancelled_audit_rejects_incomplete_or_forged_evidence(
    postgres_session: AsyncSession,
    override: dict[str, object],
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    await postgres_session.execute(
        insert(EvaluationJob).values(
            _job_insert_values(
                ids,
                status=JobStatus.CANCELLED,
                attempt_count=1,
                queued_at=now,
                started_at=now,
                finished_at=now,
            )
        )
    )
    values = {
        "id": uuid.uuid4(),
        "job_id": ids["job"],
        "submission_id": ids["submission"],
        "provider": "mock",
        "model": "fixture-v1",
        "prompt_template_version": "evaluation-v1",
        "schema_version": "1.0",
        "duration_ms": 7,
        "retry_count": 0,
        "raw_model_output": '{"score":72}',
        "validation_status": None,
        "validation_failures": [],
        "error_type": "cancelled",
        "error_message": "evaluation cancelled after provider execution",
        "final_report_id": None,
    }
    with pytest.raises(IntegrityError):
        await postgres_session.execute(insert(AuditLog).values(values | override))


@pytest.mark.asyncio
async def test_direct_sql_cancelled_audit_requires_a_provider_attempt(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    await postgres_session.execute(
        insert(EvaluationJob).values(
            _job_insert_values(
                ids,
                status=JobStatus.CANCELLED,
                attempt_count=0,
                queued_at=now,
                started_at=now,
                finished_at=now,
            )
        )
    )
    with pytest.raises(IntegrityError, match="cancelled audit evidence"):
        await postgres_session.execute(
            insert(AuditLog).values(
                id=uuid.uuid4(),
                job_id=ids["job"],
                submission_id=ids["submission"],
                provider="mock",
                model="fixture-v1",
                prompt_template_version="evaluation-v1",
                schema_version="1.0",
                duration_ms=1,
                retry_count=0,
                raw_model_output='{"provider":"called"}',
                validation_status=None,
                validation_failures=[],
                error_type="cancelled",
                error_message="evaluation cancelled after provider execution",
                final_report_id=None,
            )
        )


@pytest.mark.asyncio
async def test_notification_outbox_report_must_belong_to_same_job(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    first_job = _job(ids)
    second_job = _job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2)
    postgres_session.add_all([first_job, second_job])
    await postgres_session.flush()
    first_report = _report(ids)
    second_report = _report(
        ids,
        id=uuid.uuid4(),
        job_id=second_job.id,
        version=2,
    )
    postgres_session.add(first_report)
    await postgres_session.flush()
    postgres_session.add(second_report)
    await postgres_session.flush()

    with pytest.raises(IntegrityError, match="fk_evaluation_outbox_report_job"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(EvaluationOutbox).values(
                    id=uuid.uuid4(),
                    kind="notification",
                    job_id=first_job.id,
                    report_id=second_report.id,
                )
            )

    await postgres_session.execute(
        insert(EvaluationOutbox).values(
            id=uuid.uuid4(),
            kind="notification",
            job_id=second_job.id,
            report_id=second_report.id,
        )
    )
    assert await postgres_session.scalar(
        select(EvaluationOutbox.id).where(EvaluationOutbox.job_id == second_job.id)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("version", 0, "ck_evaluation_reports_version_positive"),
        ("score", -1, "ck_evaluation_reports_score_range"),
        ("score", 101, "ck_evaluation_reports_score_range"),
        ("confidence", Decimal("1.0001"), "ck_evaluation_reports_confidence_range"),
        ("schema_version", "2.0", "ck_evaluation_reports_schema_version"),
        ("completeness", [], "ck_evaluation_reports_completeness_object"),
        ("correctness", [], "ck_evaluation_reports_correctness_object"),
        ("major_issues", {}, "ck_evaluation_reports_major_issues_array"),
        ("suggestions", {}, "ck_evaluation_reports_suggestions_array"),
        ("limitations", {}, "ck_evaluation_reports_limitations_array"),
    ],
)
async def test_report_database_constraints_reject_invalid_values(
    postgres_session: AsyncSession,
    field: str,
    value: object,
    constraint: str,
) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids))
    await postgres_session.flush()
    postgres_session.add(_report(ids, **{field: value}))

    with pytest.raises(IntegrityError, match=constraint):
        await postgres_session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"job_id": None},
        {"created_by_teacher_id": uuid.uuid4()},
        {"source_report_id": uuid.uuid4()},
        {"raw_model_output": None},
        {
            "origin": ReportOrigin.TEACHER,
            "job_id": uuid.uuid4(),
            "source_report_id": None,
            "created_by_teacher_id": None,
            "raw_model_output": '{"secret":true}',
        },
    ],
)
async def test_report_origin_rules_cannot_be_bypassed_with_direct_orm(
    postgres_session: AsyncSession,
    overrides: dict[str, object],
) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids))
    await postgres_session.flush()
    postgres_session.add(_report(ids, **overrides))

    with pytest.raises(IntegrityError, match="ck_evaluation_reports_origin_fields"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_report_links_cannot_cross_submissions(postgres_session: AsyncSession) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids))
    await postgres_session.flush()
    postgres_session.add(_report(ids, submission_id=ids["other_submission"]))

    with pytest.raises(IntegrityError, match="fk_evaluation_reports_job_submission"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_teacher_report_must_reference_agent_report(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids))
    await postgres_session.flush()
    agent = _report(ids)
    postgres_session.add(agent)
    await postgres_session.flush()
    first_teacher = _report(
        ids,
        id=uuid.uuid4(),
        job_id=None,
        source_report_id=agent.id,
        origin=ReportOrigin.TEACHER,
        created_by_teacher_id=ids["teacher"],
        version=2,
        raw_model_output=None,
        review_status=ReviewStatus.MODIFIED,
    )
    postgres_session.add(first_teacher)
    await postgres_session.flush()
    postgres_session.add(
        _report(
            ids,
            id=uuid.uuid4(),
            job_id=None,
            source_report_id=first_teacher.id,
            origin=ReportOrigin.TEACHER,
            created_by_teacher_id=ids["teacher"],
            version=3,
            raw_model_output=None,
            review_status=ReviewStatus.MODIFIED,
        )
    )

    with pytest.raises(Exception, match="teacher reports must reference an agent report"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_job_requester_and_report_editor_roles_are_enforced(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    with pytest.raises(Exception, match="evaluation job requester must be teacher or admin"):
        async with postgres_session.begin_nested():
            postgres_session.add(_job(ids, requested_by=ids["student"]))
            await postgres_session.flush()

    job = _job(ids)
    postgres_session.add(job)
    await postgres_session.flush()
    agent = _report(ids)
    postgres_session.add(agent)
    await postgres_session.flush()
    postgres_session.add(
        _report(
            ids,
            id=uuid.uuid4(),
            job_id=None,
            source_report_id=agent.id,
            origin=ReportOrigin.TEACHER,
            created_by_teacher_id=ids["student"],
            version=2,
            raw_model_output=None,
            review_status=ReviewStatus.MODIFIED,
        )
    )
    with pytest.raises(Exception, match="report editor must be an active teacher"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_report_version_and_job_are_unique(postgres_session: AsyncSession) -> None:
    ids = await _domain_rows(postgres_session)
    first_job = _job(ids)
    second_job = _job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2)
    postgres_session.add_all([first_job, second_job])
    await postgres_session.flush()
    postgres_session.add(_report(ids))
    await postgres_session.flush()

    for duplicate in (
        _report(ids, id=uuid.uuid4(), job_id=second_job.id),
        _report(ids, id=uuid.uuid4(), version=2),
    ):
        with pytest.raises(IntegrityError):
            async with postgres_session.begin_nested():
                postgres_session.add(duplicate)
                await postgres_session.flush()


@pytest.mark.asyncio
async def test_report_versions_must_start_at_one_and_have_no_gaps(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    jobs = [_job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2) for _ in range(4)]
    postgres_session.add_all(jobs)
    await postgres_session.flush()

    with pytest.raises(Exception, match="evaluation report version must be the next version"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(EvaluationReport).values(
                    **_report_insert_values(ids, job_id=jobs[0].id, version=2)
                )
            )

    await postgres_session.execute(
        insert(EvaluationReport).values(**_report_insert_values(ids, job_id=jobs[1].id, version=1))
    )
    with pytest.raises(Exception, match="evaluation report version must be the next version"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(EvaluationReport).values(
                    **_report_insert_values(ids, job_id=jobs[2].id, version=3)
                )
            )
    with pytest.raises(Exception, match="evaluation report version must be the next version"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(EvaluationReport).values(
                    **_report_insert_values(
                        ids,
                        id=uuid.uuid4(),
                        job_id=jobs[3].id,
                        version=1,
                    )
                )
            )


@pytest.mark.asyncio
async def test_report_content_and_rows_are_immutable(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids))
    await postgres_session.flush()
    report = _report(ids)
    postgres_session.add(report)
    await postgres_session.flush()

    with pytest.raises(Exception, match="evaluation report versions are immutable"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                update(EvaluationReport)
                .where(EvaluationReport.id == report.id)
                .values(raw_model_output='{"tampered":true}')
            )
    with pytest.raises(Exception, match="evaluation report versions cannot be deleted"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                delete(EvaluationReport).where(EvaluationReport.id == report.id)
            )

    await postgres_session.execute(
        update(EvaluationReport)
        .where(EvaluationReport.id == report.id)
        .values(review_status=ReviewStatus.CONFIRMED)
    )
    await postgres_session.refresh(report)
    assert report.review_status is ReviewStatus.CONFIRMED


@pytest.mark.asyncio
async def test_evaluation_evidence_cannot_be_truncated_directly_or_by_cascade(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.SUCCEEDED,
        attempt_count=1,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )
    report = _report(ids)
    postgres_session.add(job)
    await postgres_session.flush()
    postgres_session.add(report)
    await postgres_session.commit()

    with pytest.raises(DBAPIError, match="evaluation evidence cannot be truncated"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(text("TRUNCATE evaluation_reports CASCADE"))

    await postgres_session.execute(
        insert(AuditLog).values(
            id=uuid.uuid4(),
            job_id=job.id,
            submission_id=ids["submission"],
            provider=job.provider,
            model=job.model,
            prompt_template_version="grading-v1",
            schema_version=report.schema_version,
            duration_ms=1,
            retry_count=0,
            raw_model_output=report.raw_model_output,
            validation_status=report.validation_status,
            validation_failures=[],
            error_type=None,
            error_message=None,
            final_report_id=report.id,
        )
    )
    await postgres_session.commit()

    for statement in (
        "TRUNCATE audit_logs",
        "TRUNCATE evaluation_jobs CASCADE",
        "TRUNCATE submissions CASCADE",
    ):
        with pytest.raises(DBAPIError, match="evaluation evidence cannot be truncated"):
            async with postgres_session.begin_nested():
                await postgres_session.execute(text(statement))
        assert await postgres_session.scalar(select(text("1"))) == 1

    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationReport)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(EvaluationJob)) == 1
    assert await postgres_session.scalar(select(func.count()).select_from(Submission)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"attempt_count": -1}, "ck_evaluation_jobs_attempt_count_range"),
        ({"attempt_count": 4}, "ck_evaluation_jobs_attempt_count_range"),
        ({"idempotency_key": "short"}, "ck_evaluation_jobs_idempotency_key"),
        ({"provider": " mock"}, "ck_evaluation_jobs_provider_safe"),
        ({"provider": "mock\nforge"}, "ck_evaluation_jobs_provider_safe"),
        ({"model": ""}, "ck_evaluation_jobs_model_safe"),
        ({"execution_generation": -1}, "ck_evaluation_jobs_execution_generation"),
        ({"execution_token": uuid.uuid4()}, "ck_evaluation_jobs_execution_token_state"),
        ({"error_code": "timeout"}, "ck_evaluation_jobs_error_fields"),
        (
            {"status": JobStatus.SUCCEEDED, "finished_at": None},
            "ck_evaluation_jobs_status_timestamps",
        ),
    ],
)
async def test_job_database_constraints_reject_invalid_values(
    postgres_session: AsyncSession,
    overrides: dict[str, object],
    constraint: str,
) -> None:
    ids = await _domain_rows(postgres_session)
    postgres_session.add(_job(ids, **overrides))

    with pytest.raises(IntegrityError, match=constraint):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_database_identifier_contract_rejects_all_invisible_unicode_classes(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    unsafe_unicode = (
        "mock\u202eoverride",
        "mock\u2066isolate",
        "mock\u2069isolate",
        "mock\u200czwnj",
        "mock\u200dzwj",
        "mock\ufe0fvariation",
        "mock\u00adsoft-hyphen",
        "mock\ufeffbom",
        "mock\ue000private",
        "mock\U000f0000private",
        "mock\u0378unassigned",
        "mock\u00a0space",
        "mock\u2003space",
        "mock\u3000space",
        "\u0301",
        "mock\u0600format",
        "mock\u070fformat",
    )

    for field in ("provider", "model"):
        maximum = 128 if field == "provider" else 256
        oversized_utf8 = "😀" * (maximum // 4 + 1)
        for unsafe in (*unsafe_unicode, "", " ", " leading", oversized_utf8):
            with pytest.raises(IntegrityError, match=f"ck_evaluation_jobs_{field}_safe"):
                async with postgres_session.begin_nested():
                    await postgres_session.execute(
                        insert(EvaluationJob).values(
                            **_job_insert_values(
                                ids,
                                id=uuid.uuid4(),
                                idempotency_key=uuid.uuid4().hex * 2,
                                **{field: unsafe},
                            )
                        )
                    )

            with pytest.raises(IntegrityError, match=f"ck_audit_logs_{field}_safe"):
                async with postgres_session.begin_nested():
                    values = {
                        "id": uuid.uuid4(),
                        "job_id": uuid.uuid4(),
                        "submission_id": uuid.uuid4(),
                        "provider": "mock",
                        "model": "fixture-v1",
                        "prompt_template_version": "grading-v1",
                        "schema_version": "1.0",
                        "duration_ms": 0,
                        "retry_count": 0,
                        "validation_failures": [],
                        "error_type": "configuration",
                        "error_message": "evaluation provider configuration is invalid",
                        "final_report_id": None,
                    }
                    values[field] = unsafe
                    await postgres_session.execute(insert(AuditLog).values(**values))

    with pytest.raises(DBAPIError, match="invalid Unicode surrogate pair"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(text("SELECT U&'\\D800'"))


@pytest.mark.asyncio
async def test_database_identifier_contract_accepts_visible_unicode_and_combining_marks(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)

    for identifier in ("mock provider", "通义 千问", "模型😀", "Cafe\u0301"):
        values = _job_insert_values(
            ids,
            id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex * 2,
            provider=identifier,
            model=identifier,
            status=JobStatus.FAILED,
            attempt_count=0,
            error_code="configuration",
            error_message="evaluation provider configuration is invalid",
            queued_at=now,
            finished_at=now,
        )
        await postgres_session.execute(insert(EvaluationJob).values(**values))
        job = await postgres_session.get(EvaluationJob, values["id"])
        assert job is not None
        await postgres_session.execute(
            insert(AuditLog).values(
                **_failed_audit_insert_values(ids, job, retry_count=0, failures=[])
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {
            "status": JobStatus.SUCCEEDED,
            "attempt_count": 0,
        },
        {
            "status": JobStatus.FAILED,
            "attempt_count": 1,
            "error_code": "configuration",
            "error_message": "evaluation provider configuration is invalid",
        },
        {
            "status": JobStatus.FAILED,
            "attempt_count": 0,
            "error_code": "network",
            "error_message": "evaluation provider network unavailable",
        },
        {
            "status": JobStatus.FAILED,
            "attempt_count": 0,
            "error_code": "validation_exhausted",
            "error_message": "evaluation output validation exhausted",
        },
    ],
)
async def test_terminal_jobs_reject_impossible_attempt_contracts(
    postgres_session: AsyncSession,
    overrides: dict[str, object],
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    postgres_session.add(
        _job(
            ids,
            queued_at=now,
            started_at=now,
            finished_at=now,
            **overrides,
        )
    )

    with pytest.raises(IntegrityError, match="ck_evaluation_jobs_terminal_attempt_contract"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_job_lifecycle_updates_remain_available_before_audit(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    job = _job(ids)
    postgres_session.add(job)
    await postgres_session.flush()
    now = datetime.now(UTC)

    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(
            status=JobStatus.RUNNING,
            started_at=now,
            execution_token=uuid.uuid4(),
            execution_generation=1,
        )
    )
    await postgres_session.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED,
            attempt_count=1,
            execution_token=None,
            finished_at=now,
        )
    )
    await postgres_session.refresh(job)

    assert job.status is JobStatus.SUCCEEDED
    assert job.attempt_count == 1


@pytest.mark.asyncio
async def test_idempotency_key_is_unique(postgres_session: AsyncSession) -> None:
    ids = await _domain_rows(postgres_session)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    postgres_session.add(_job(ids, idempotency_key=key))
    await postgres_session.flush()
    postgres_session.add(_job(ids, id=uuid.uuid4(), idempotency_key=key))

    with pytest.raises(IntegrityError, match="uq_evaluation_jobs_idempotency_key"):
        await postgres_session.flush()


@pytest.mark.asyncio
async def test_idempotency_and_report_version_uniqueness_are_concurrency_safe(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    await postgres_session.commit()
    engine = postgres_session.bind
    assert isinstance(engine, AsyncEngine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    shared_key = uuid.uuid4().hex * 2

    async def insert_job(job_id: uuid.UUID, key: str) -> str:
        async with sessions() as session:
            session.add(_job(ids, id=job_id, idempotency_key=key))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return "conflict"
            return "inserted"

    first_job_id, second_job_id = uuid.uuid4(), uuid.uuid4()
    outcomes = await asyncio.gather(
        insert_job(first_job_id, shared_key),
        insert_job(second_job_id, shared_key),
    )
    assert sorted(outcomes) == ["conflict", "inserted"]
    async with sessions() as session:
        winning_job_id = await session.scalar(
            select(EvaluationJob.id).where(EvaluationJob.idempotency_key == shared_key)
        )
        assert winning_job_id in {first_job_id, second_job_id}
        third_job = _job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2)
        session.add(third_job)
        await session.commit()
        third_job_id = third_job.id

    async def insert_report(report_id: uuid.UUID, job_id: uuid.UUID) -> str:
        async with sessions() as session:
            session.add(_report(ids, id=report_id, job_id=job_id, version=1))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return "conflict"
            return "inserted"

    outcomes = await asyncio.gather(
        insert_report(uuid.uuid4(), winning_job_id),
        insert_report(uuid.uuid4(), third_job_id),
    )
    assert sorted(outcomes) == ["conflict", "inserted"]


@pytest.mark.asyncio
async def test_rolled_back_report_insert_does_not_leave_a_version_gap(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    first_job = _job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2)
    second_job = _job(ids, id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex * 2)
    postgres_session.add_all([first_job, second_job])
    await postgres_session.commit()
    engine = postgres_session.bind
    assert isinstance(engine, AsyncEngine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    first = sessions()
    second = sessions()
    try:
        await first.execute(
            insert(EvaluationReport).values(
                **_report_insert_values(ids, job_id=first_job.id, version=1)
            )
        )
        started = asyncio.Event()

        async def try_version_two() -> str:
            started.set()
            try:
                await second.execute(
                    insert(EvaluationReport).values(
                        **_report_insert_values(ids, job_id=second_job.id, version=2)
                    )
                )
                await second.commit()
            except IntegrityError:
                await second.rollback()
                return "rejected"
            return "inserted"

        contender = asyncio.create_task(try_version_two())
        await started.wait()
        await asyncio.sleep(0.02)
        await first.rollback()
        assert await contender == "rejected"
    finally:
        await first.close()
        await second.close()

    async with sessions() as verification:
        assert (
            await verification.scalar(
                select(EvaluationReport.id).where(
                    EvaluationReport.submission_id == ids["submission"]
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_append_audit_log_is_structured_and_does_not_commit(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.SUCCEEDED,
        attempt_count=2,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    report = _report(ids, validation_status=ValidationStatus.REPAIRED)
    postgres_session.add(report)
    await postgres_session.flush()

    record = await append_audit_log(
        postgres_session,
        AuditLogCreate(
            job_id=job.id,
            submission_id=ids["submission"],
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=41,
            retry_count=1,
            raw_model_output='{"score":72}',
            validation_status=ValidationStatus.REPAIRED,
            validation_failures=(RepairErrorCode.INVALID_JSON,),
            error_type=None,
            error_message=None,
            final_report_id=report.id,
        ),
    )

    assert record.id is not None
    assert record.validation_failures == ["invalid_json"]
    assert record.created_at.tzinfo is not None
    assert postgres_session.in_transaction()
    assert await postgres_session.get(AuditLog, record.id) is record

    await postgres_session.rollback()
    assert await postgres_session.get(AuditLog, record.id) is None


def test_audit_contract_accepts_formatted_raw_json_and_rejects_unsafe_error_type() -> None:
    common: dict[str, object] = {
        "job_id": uuid.uuid4(),
        "submission_id": uuid.uuid4(),
        "provider": "mock",
        "model": "fixture-v1",
        "prompt_template_version": "grading-v1",
        "schema_version": "1.0",
        "duration_ms": 1,
        "retry_count": 0,
        "validation_failures": (),
    }
    successful = AuditLogCreate(
        **common,
        raw_model_output='{\n  "score": 72\n}',
        validation_status=ValidationStatus.VALID,
        error_type=None,
        error_message=None,
        final_report_id=uuid.uuid4(),
    )
    assert successful.raw_model_output.startswith("{\n")

    with pytest.raises(ValueError, match="error_type"):
        AuditLogCreate(
            **common,
            raw_model_output=None,
            validation_status=None,
            error_type="bad type",
            error_message="safe",
            final_report_id=None,
        )


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "Authorization: Bearer top-secret",
        "Bearer sk-live-secret",
        "postgresql://grader:password@db/grader",
        "student_answer=the private response",
        "safe\u202eforged",
    ],
)
def test_audit_contract_rejects_free_text_and_secret_error_messages(
    unsafe_message: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        AuditLogCreate(
            job_id=uuid.uuid4(),
            submission_id=uuid.uuid4(),
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=1,
            retry_count=0,
            raw_model_output=None,
            validation_status=None,
            validation_failures=(),
            error_type="timeout",
            error_message=unsafe_message,
            final_report_id=None,
        )
    assert unsafe_message not in str(captured.value)


@pytest.mark.asyncio
async def test_audit_database_evidence_must_match_job_and_final_report(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.SUCCEEDED,
        attempt_count=1,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    report = _report(ids, raw_model_output='{\n  "score": 72\n}')
    postgres_session.add(report)
    await postgres_session.flush()

    valid = {
        "id": uuid.uuid4(),
        "job_id": job.id,
        "submission_id": ids["submission"],
        "provider": job.provider,
        "model": job.model,
        "prompt_template_version": "grading-v1",
        "schema_version": report.schema_version,
        "duration_ms": 11,
        "retry_count": 0,
        "raw_model_output": report.raw_model_output,
        "validation_status": report.validation_status,
        "validation_failures": [],
        "error_type": None,
        "error_message": None,
        "final_report_id": report.id,
    }
    for field, mismatched in (
        ("provider", "other-provider"),
        ("model", "other-model"),
        ("raw_model_output", '{"score":99}'),
        ("validation_status", ValidationStatus.REPAIRED),
    ):
        values = valid | {"id": uuid.uuid4(), field: mismatched}
        with pytest.raises(Exception, match="audit evidence does not match"):
            async with postgres_session.begin_nested():
                await postgres_session.execute(insert(AuditLog).values(**values))

    await postgres_session.execute(insert(AuditLog).values(**valid))
    assert (
        await postgres_session.scalar(select(AuditLog.id).where(AuditLog.job_id == job.id))
        == valid["id"]
    )
    with pytest.raises(Exception, match="audited evaluation job evidence is immutable"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                update(EvaluationJob).where(EvaluationJob.id == job.id).values(provider="tampered")
            )


@pytest.mark.asyncio
async def test_audit_retry_count_matches_job_attempt_count(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.SUCCEEDED,
        attempt_count=2,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    report = _report(ids, validation_status=ValidationStatus.VALID)
    postgres_session.add(report)
    await postgres_session.flush()
    values = {
        "id": uuid.uuid4(),
        "job_id": job.id,
        "submission_id": ids["submission"],
        "provider": job.provider,
        "model": job.model,
        "prompt_template_version": "grading-v1",
        "schema_version": report.schema_version,
        "duration_ms": 11,
        "retry_count": 0,
        "raw_model_output": report.raw_model_output,
        "validation_status": report.validation_status,
        "validation_failures": [],
        "final_report_id": report.id,
    }

    with pytest.raises(Exception, match="audit retry count does not match job attempts"):
        await postgres_session.execute(insert(AuditLog).values(**values))


@pytest.mark.asyncio
async def test_failed_audit_uses_fixed_job_error_and_cannot_fake_success(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    safe_message = "evaluation provider timed out"
    job = _job(
        ids,
        status=JobStatus.FAILED,
        attempt_count=1,
        error_code="timeout",
        error_message=safe_message,
        queued_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    report = _report(ids)
    postgres_session.add(report)
    await postgres_session.flush()

    failure_values = {
        "id": uuid.uuid4(),
        "job_id": job.id,
        "submission_id": ids["submission"],
        "provider": job.provider,
        "model": job.model,
        "prompt_template_version": "grading-v1",
        "schema_version": "1.0",
        "duration_ms": 11,
        "retry_count": 0,
        "raw_model_output": None,
        "validation_status": None,
        "validation_failures": [],
        "error_type": job.error_code,
        "error_message": job.error_message,
        "final_report_id": None,
    }
    for field, mismatch in (
        ("provider", "other-provider"),
        ("error_type", "network"),
        ("error_message", "evaluation provider network unavailable"),
    ):
        with pytest.raises(Exception, match="audit evidence does not match"):
            async with postgres_session.begin_nested():
                await postgres_session.execute(
                    insert(AuditLog).values(
                        **(failure_values | {"id": uuid.uuid4(), field: mismatch})
                    )
                )

    fake_success = failure_values | {
        "id": uuid.uuid4(),
        "raw_model_output": report.raw_model_output,
        "validation_status": report.validation_status,
        "error_type": None,
        "error_message": None,
        "final_report_id": report.id,
    }
    with pytest.raises(Exception, match="successful audit requires a succeeded job"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(insert(AuditLog).values(**fake_success))

    await postgres_session.execute(insert(AuditLog).values(**failure_values))


@pytest.mark.asyncio
async def test_database_enforces_terminal_failure_evidence_matrix(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    cases = [
        (
            _job(
                ids,
                id=uuid.uuid4(),
                idempotency_key=uuid.uuid4().hex * 2,
                status=JobStatus.FAILED,
                attempt_count=0,
                error_code="configuration",
                error_message="evaluation provider configuration is invalid",
                queued_at=now,
                finished_at=now,
            ),
            0,
            [],
            [RepairErrorCode.INVALID_JSON.value],
        ),
        (
            _job(
                ids,
                id=uuid.uuid4(),
                idempotency_key=uuid.uuid4().hex * 2,
                status=JobStatus.FAILED,
                attempt_count=2,
                error_code="network",
                error_message="evaluation provider network unavailable",
                queued_at=now,
                started_at=now,
                finished_at=now,
            ),
            1,
            [RepairErrorCode.INVALID_JSON.value],
            [RepairErrorCode.INVALID_JSON.value, RepairErrorCode.SCHEMA.value],
        ),
        (
            _job(
                ids,
                id=uuid.uuid4(),
                idempotency_key=uuid.uuid4().hex * 2,
                status=JobStatus.FAILED,
                attempt_count=2,
                error_code="validation_exhausted",
                error_message="evaluation output validation exhausted",
                queued_at=now,
                started_at=now,
                finished_at=now,
            ),
            1,
            [RepairErrorCode.INVALID_JSON.value, RepairErrorCode.SCHEMA.value],
            [RepairErrorCode.INVALID_JSON.value],
        ),
    ]
    postgres_session.add_all([case[0] for case in cases])
    await postgres_session.flush()

    for job, retry_count, valid_failures, invalid_failures in cases:
        with pytest.raises(IntegrityError, match="ck_audit_logs_retry_evidence"):
            async with postgres_session.begin_nested():
                await postgres_session.execute(
                    insert(AuditLog).values(
                        **_failed_audit_insert_values(
                            ids,
                            job,
                            retry_count=retry_count,
                            failures=invalid_failures,
                        )
                    )
                )
        await postgres_session.execute(
            insert(AuditLog).values(
                **_failed_audit_insert_values(
                    ids,
                    job,
                    retry_count=retry_count,
                    failures=valid_failures,
                )
            )
        )


@pytest.mark.asyncio
async def test_audit_insert_and_job_update_serialize_on_the_same_job_row(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    jobs = [
        _job(
            ids,
            id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex * 2,
            status=JobStatus.FAILED,
            attempt_count=1,
            error_code="network",
            error_message="evaluation provider network unavailable",
            queued_at=now,
            started_at=now,
            finished_at=now,
        )
        for _ in range(2)
    ]
    postgres_session.add_all(jobs)
    await postgres_session.commit()
    engine = postgres_session.bind
    assert isinstance(engine, AsyncEngine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def update_provider(job_id: uuid.UUID, provider: str) -> str:
        async with sessions() as session:
            try:
                await session.execute(
                    update(EvaluationJob)
                    .where(EvaluationJob.id == job_id)
                    .values(provider=provider)
                )
                await session.commit()
            except DBAPIError:
                await session.rollback()
                return "rejected"
            return "updated"

    async def insert_failure_audit(job: EvaluationJob) -> str:
        async with sessions() as session:
            try:
                await session.execute(
                    insert(AuditLog).values(
                        **_failed_audit_insert_values(ids, job, retry_count=0, failures=[])
                    )
                )
                await session.commit()
            except DBAPIError:
                await session.rollback()
                return "rejected"
            return "inserted"

    async with sessions() as audit_first:
        await audit_first.execute(
            insert(AuditLog).values(
                **_failed_audit_insert_values(ids, jobs[0], retry_count=0, failures=[])
            )
        )
        blocked_update = asyncio.create_task(update_provider(jobs[0].id, "updated-provider"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(blocked_update), timeout=0.1)
        await audit_first.commit()
        assert await asyncio.wait_for(blocked_update, timeout=2) == "rejected"

    async with sessions() as update_first:
        await update_first.execute(
            update(EvaluationJob)
            .where(EvaluationJob.id == jobs[1].id)
            .values(provider="updated-provider")
        )
        blocked_audit = asyncio.create_task(insert_failure_audit(jobs[1]))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(blocked_audit), timeout=0.1)
        await update_first.commit()
        assert await asyncio.wait_for(blocked_audit, timeout=2) == "rejected"

    async with sessions() as verification:
        assert await verification.scalar(select(AuditLog.id).where(AuditLog.job_id == jobs[0].id))
        assert (
            await verification.scalar(
                select(EvaluationJob.provider).where(EvaluationJob.id == jobs[0].id)
            )
            == "mock"
        )
        assert not await verification.scalar(
            select(AuditLog.id).where(AuditLog.job_id == jobs[1].id)
        )
        assert (
            await verification.scalar(
                select(EvaluationJob.provider).where(EvaluationJob.id == jobs[1].id)
            )
            == "updated-provider"
        )


@pytest.mark.asyncio
async def test_database_rejects_free_text_job_and_audit_errors(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError, match="ck_evaluation_jobs_error_safe"):
        async with postgres_session.begin_nested():
            postgres_session.add(
                _job(
                    ids,
                    status=JobStatus.FAILED,
                    error_code="timeout",
                    error_message="Authorization: Bearer secret",
                    queued_at=now,
                    finished_at=now,
                )
            )
            await postgres_session.flush()

    with pytest.raises(IntegrityError, match="ck_audit_logs_error_type_safe"):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                insert(AuditLog).values(
                    id=uuid.uuid4(),
                    job_id=uuid.uuid4(),
                    submission_id=uuid.uuid4(),
                    provider="mock",
                    model="fixture-v1",
                    prompt_template_version="grading-v1",
                    schema_version="1.0",
                    duration_ms=0,
                    retry_count=0,
                    validation_failures=[],
                    error_type="timeout",
                    error_message="student_answer=private response",
                    final_report_id=None,
                )
            )


def test_audit_contract_rejects_inconsistent_retry_evidence() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        AuditLogCreate(
            job_id=uuid.uuid4(),
            submission_id=uuid.uuid4(),
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=1,
            retry_count=2,
            raw_model_output='{"score":72}',
            validation_status=ValidationStatus.REPAIRED,
            validation_failures=(RepairErrorCode.INVALID_JSON,),
            error_type=None,
            error_message=None,
            final_report_id=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    ("error_type", "retry_count", "failures"),
    [
        ("configuration", 0, ()),
        ("network", 1, (RepairErrorCode.INVALID_JSON,)),
        (
            "validation_exhausted",
            1,
            (RepairErrorCode.INVALID_JSON, RepairErrorCode.SCHEMA),
        ),
    ],
)
def test_audit_contract_accepts_terminal_failure_evidence_matrix(
    error_type: str,
    retry_count: int,
    failures: tuple[RepairErrorCode, ...],
) -> None:
    messages = {
        "configuration": "evaluation provider configuration is invalid",
        "network": "evaluation provider network unavailable",
        "validation_exhausted": "evaluation output validation exhausted",
    }

    record = AuditLogCreate(
        job_id=uuid.uuid4(),
        submission_id=uuid.uuid4(),
        provider="mock",
        model="fixture-v1",
        prompt_template_version="grading-v1",
        schema_version="1.0",
        duration_ms=1,
        retry_count=retry_count,
        raw_model_output=None,
        validation_status=None,
        validation_failures=failures,
        error_type=error_type,
        error_message=messages[error_type],
        final_report_id=None,
    )

    assert record.validation_failures == failures


@pytest.mark.parametrize(
    ("error_type", "retry_count", "failures"),
    [
        (
            "configuration",
            1,
            (RepairErrorCode.INVALID_JSON, RepairErrorCode.SCHEMA),
        ),
        (
            "network",
            1,
            (RepairErrorCode.INVALID_JSON, RepairErrorCode.SCHEMA),
        ),
        ("validation_exhausted", 1, (RepairErrorCode.INVALID_JSON,)),
    ],
)
def test_audit_contract_rejects_impossible_terminal_failure_evidence(
    error_type: str,
    retry_count: int,
    failures: tuple[RepairErrorCode, ...],
) -> None:
    messages = {
        "configuration": "evaluation provider configuration is invalid",
        "network": "evaluation provider network unavailable",
        "validation_exhausted": "evaluation output validation exhausted",
    }

    with pytest.raises(ValueError, match="terminal failure evidence"):
        AuditLogCreate(
            job_id=uuid.uuid4(),
            submission_id=uuid.uuid4(),
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=1,
            retry_count=retry_count,
            raw_model_output=None,
            validation_status=None,
            validation_failures=failures,
            error_type=error_type,
            error_message=messages[error_type],
            final_report_id=None,
        )


@pytest.mark.asyncio
async def test_append_service_revalidates_constructed_contract(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    job = _job(ids)
    postgres_session.add(job)
    await postgres_session.flush()
    forged = object.__new__(AuditLogCreate)
    for field, value in {
        "job_id": job.id,
        "submission_id": ids["submission"],
        "provider": "\u200b",
        "model": "fixture-v1",
        "prompt_template_version": "grading-v1",
        "schema_version": "1.0",
        "duration_ms": 1,
        "retry_count": 0,
        "raw_model_output": None,
        "validation_status": None,
        "validation_failures": (),
        "error_type": "timeout",
        "error_message": "evaluation provider timed out",
        "final_report_id": None,
    }.items():
        object.__setattr__(forged, field, value)

    with pytest.raises(ValueError, match="provider"):
        await append_audit_log(postgres_session, forged)


@pytest.mark.asyncio
async def test_audit_log_is_database_append_only(postgres_session: AsyncSession) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.FAILED,
        attempt_count=1,
        error_code="timeout",
        error_message="evaluation provider timed out",
        queued_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    audit = await append_audit_log(
        postgres_session,
        AuditLogCreate(
            job_id=job.id,
            submission_id=ids["submission"],
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=0,
            retry_count=0,
            raw_model_output=None,
            validation_status=None,
            validation_failures=(),
            error_type="timeout",
            error_message="evaluation provider timed out",
            final_report_id=None,
        ),
    )

    for statement in (
        update(AuditLog).where(AuditLog.id == audit.id).values(duration_ms=1),
        delete(AuditLog).where(AuditLog.id == audit.id),
    ):
        with pytest.raises(Exception, match="audit_logs are append-only"):
            async with postgres_session.begin_nested():
                await postgres_session.execute(statement)


@pytest.mark.asyncio
async def test_audit_log_rejects_unbounded_validation_json(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.FAILED,
        attempt_count=1,
        error_code="timeout",
        error_message="evaluation provider timed out",
        queued_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()

    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            await postgres_session.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, job_id, submission_id, provider, model, prompt_template_version, "
                    "schema_version, duration_ms, retry_count, validation_failures, "
                    "error_type, error_message) VALUES "
                    "(gen_random_uuid(), :job_id, :submission_id, 'mock', 'fixture-v1', "
                    "'grading-v1', '1.0', 0, 0, CAST(:failures AS jsonb), "
                    "'timeout', 'evaluation provider timed out')"
                ),
                {
                    "job_id": job.id,
                    "submission_id": ids["submission"],
                    "failures": '["x","x","x","x"]',
                },
            )


@pytest.mark.asyncio
async def test_audit_final_report_must_belong_to_same_job(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    completed = {
        "status": JobStatus.SUCCEEDED,
        "attempt_count": 1,
        "queued_at": now,
        "started_at": now,
        "finished_at": now,
    }
    first_job = _job(ids, **completed)
    second_job = _job(
        ids,
        id=uuid.uuid4(),
        idempotency_key=uuid.uuid4().hex * 2,
        **completed,
    )
    postgres_session.add_all([first_job, second_job])
    await postgres_session.flush()
    report = _report(ids)
    postgres_session.add(report)
    await postgres_session.flush()

    with pytest.raises(IntegrityError, match="fk_audit_logs_final_report_job"):
        await postgres_session.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, job_id, submission_id, provider, model, prompt_template_version, "
                "schema_version, duration_ms, retry_count, raw_model_output, "
                "validation_status, validation_failures, final_report_id) VALUES "
                "(gen_random_uuid(), :job_id, :submission_id, 'mock', 'fixture-v1', "
                "'grading-v1', '1.0', 0, 0, :raw_output, 'valid', '[]'::jsonb, :report_id)"
            ),
            {
                "job_id": second_job.id,
                "submission_id": ids["submission"],
                "report_id": report.id,
                "raw_output": report.raw_model_output,
            },
        )


@pytest.mark.asyncio
async def test_append_service_does_not_autoflush_unrelated_pending_rows(
    postgres_session: AsyncSession,
) -> None:
    ids = await _domain_rows(postgres_session)
    now = datetime.now(UTC)
    job = _job(
        ids,
        status=JobStatus.FAILED,
        attempt_count=1,
        error_code="timeout",
        error_message="evaluation provider timed out",
        queued_at=now,
        finished_at=now,
    )
    postgres_session.add(job)
    await postgres_session.flush()
    pending = User(
        username=f"pending-{uuid.uuid4().hex}",
        display_name="Pending",
        role=Role.STUDENT,
        password_hash="hash",
    )
    postgres_session.add(pending)

    await append_audit_log(
        postgres_session,
        AuditLogCreate(
            job_id=job.id,
            submission_id=ids["submission"],
            provider="mock",
            model="fixture-v1",
            prompt_template_version="grading-v1",
            schema_version="1.0",
            duration_ms=0,
            retry_count=0,
            raw_model_output=None,
            validation_status=None,
            validation_failures=(),
            error_type="timeout",
            error_message="evaluation provider timed out",
            final_report_id=None,
        ),
    )

    assert pending.id is None
    with postgres_session.no_autoflush:
        assert not await postgres_session.scalar(
            select(User).where(User.username == pending.username)
        )
