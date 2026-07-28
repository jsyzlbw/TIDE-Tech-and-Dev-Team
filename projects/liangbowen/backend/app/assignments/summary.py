"""Assignment summary projection.

Deployments must cap active student accounts at ``SUMMARY_MAX_ACTIVE_STUDENTS``.
The response intentionally includes a full assignment version map, so this bound
keeps the non-paginated part of the response predictably sized.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import String, and_, cast, func, literal, null, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.db.types import Grade, Role, SubmissionStatus
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.types import JobStatus, ReviewStatus
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionRead
from app.users.model import User

SUMMARY_MAX_ACTIVE_STUDENTS = 10_000


class AssignmentSummaryStudent(BaseModel):
    student_id: uuid.UUID
    username: str
    display_name: str
    latest_submission: SubmissionRead | None
    latest_version: int | None
    submitted_at: datetime | None
    evaluation_status: str | None
    evaluation_error_code: str | None
    report_status: str | None
    latest_report_id: uuid.UUID | None
    score: float | None
    grade: Grade | None
    evaluation_error: str | None


class AssignmentSummary(BaseModel):
    assignment_id: uuid.UUID
    total_students: int
    submitted_students: int
    missing_students: int
    latest_submission_at: datetime | None
    latest_submission_versions: dict[uuid.UUID, int]
    pending_evaluation: int
    queued: int
    evaluating: int
    pending_review: int
    reviewed: int
    failed: int
    limit: int
    offset: int
    students: list[AssignmentSummaryStudent]


def _latest_submitted(assignment_id: uuid.UUID):
    return (
        select(
            Submission.id.label("submission_id"),
            Submission.assignment_id,
            Submission.student_id,
            Submission.version,
            Submission.submitted_at,
            func.row_number()
            .over(
                partition_by=(Submission.assignment_id, Submission.student_id),
                order_by=(
                    Submission.version.desc(),
                    Submission.submitted_at.desc(),
                    Submission.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(
            Submission.assignment_id == assignment_id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
        .cte("latest_submitted")
    )


def _active_student_filter():
    return User.role == Role.STUDENT, User.is_active.is_(True)


def _summary_statement(
    assignment_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
):
    ranked = _latest_submitted(assignment_id)
    latest = (
        select(
            ranked.c.submission_id,
            ranked.c.student_id,
            ranked.c.version,
            ranked.c.submitted_at,
        )
        .where(ranked.c.row_number == 1)
        .cte("latest_submitted_submission")
    )
    ranked_jobs = (
        select(
            EvaluationJob.submission_id,
            EvaluationJob.status.label("evaluation_status"),
            EvaluationJob.error_code.label("evaluation_error_code"),
            EvaluationJob.error_message.label("evaluation_error"),
            func.row_number()
            .over(
                partition_by=EvaluationJob.submission_id,
                order_by=(EvaluationJob.queued_at.desc(), EvaluationJob.id.desc()),
            )
            .label("row_number"),
        )
        .join(Submission, Submission.id == EvaluationJob.submission_id)
        .where(Submission.assignment_id == assignment_id)
        .cte("ranked_evaluation_jobs")
    )
    latest_job = (
        select(
            ranked_jobs.c.submission_id,
            ranked_jobs.c.evaluation_status,
            ranked_jobs.c.evaluation_error_code,
            ranked_jobs.c.evaluation_error,
        )
        .where(ranked_jobs.c.row_number == 1)
        .cte("latest_evaluation_job")
    )
    ranked_reports = (
        select(
            EvaluationReport.id.label("latest_report_id"),
            EvaluationReport.submission_id,
            EvaluationReport.review_status.label("report_status"),
            EvaluationReport.score,
            EvaluationReport.grade,
            func.row_number()
            .over(
                partition_by=EvaluationReport.submission_id,
                order_by=(
                    EvaluationReport.version.desc(),
                    EvaluationReport.created_at.desc(),
                    EvaluationReport.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .join(Submission, Submission.id == EvaluationReport.submission_id)
        .where(
            Submission.assignment_id == assignment_id,
            EvaluationReport.review_status != ReviewStatus.SUPERSEDED,
        )
        .cte("ranked_evaluation_reports")
    )
    latest_report = (
        select(
            ranked_reports.c.latest_report_id,
            ranked_reports.c.submission_id,
            ranked_reports.c.report_status,
            ranked_reports.c.score,
            ranked_reports.c.grade,
        )
        .where(ranked_reports.c.row_number == 1)
        .cte("latest_evaluation_report")
    )
    active_population = (
        select(
            User.id.label("student_id"),
            User.username,
            User.display_name,
            latest.c.submission_id,
            latest.c.version.label("latest_version"),
            latest.c.submitted_at,
            latest_job.c.evaluation_status,
            latest_job.c.evaluation_error_code,
            latest_job.c.evaluation_error,
            latest_report.c.latest_report_id,
            latest_report.c.report_status,
            latest_report.c.score,
            latest_report.c.grade,
        )
        .select_from(User)
        .outerjoin(latest, latest.c.student_id == User.id)
        .outerjoin(latest_job, latest_job.c.submission_id == latest.c.submission_id)
        .outerjoin(latest_report, latest_report.c.submission_id == latest.c.submission_id)
        .where(*_active_student_filter())
        .cte("active_student_population")
    )
    version_map = func.jsonb_object_agg(
        cast(active_population.c.student_id, String),
        active_population.c.latest_version,
    ).filter(active_population.c.submission_id.is_not(None))
    aggregate = (
        select(
            Assignment.id.label("assignment_id"),
            func.count(active_population.c.student_id).label("total_students"),
            func.count(active_population.c.submission_id).label("submitted_students"),
            func.max(active_population.c.submitted_at).label("latest_submission_at"),
            func.coalesce(version_map, cast(literal("{}"), JSONB)).label(
                "latest_submission_versions"
            ),
            func.count(active_population.c.student_id)
            .filter(
                active_population.c.submission_id.is_not(None),
                (
                    and_(
                        (
                            active_population.c.evaluation_status.is_(None)
                            | active_population.c.evaluation_status.not_in(
                                [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED]
                            )
                        ),
                        (
                            active_population.c.report_status.is_(None)
                            | (active_population.c.report_status == ReviewStatus.SUPERSEDED)
                        ),
                    )
                    | (active_population.c.evaluation_status == JobStatus.QUEUED)
                ),
            )
            .label("pending_evaluation"),
            func.count(active_population.c.student_id)
            .filter(active_population.c.evaluation_status == JobStatus.QUEUED)
            .label("queued"),
            func.count(active_population.c.student_id)
            .filter(active_population.c.evaluation_status == JobStatus.RUNNING)
            .label("evaluating"),
            func.count(active_population.c.student_id)
            .filter(
                active_population.c.evaluation_status.not_in(
                    [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED]
                ),
                active_population.c.report_status == ReviewStatus.PROPOSED,
            )
            .label("pending_review"),
            func.count(active_population.c.student_id)
            .filter(
                active_population.c.evaluation_status.not_in(
                    [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED]
                ),
                active_population.c.report_status.in_(
                    [ReviewStatus.CONFIRMED, ReviewStatus.MODIFIED]
                ),
            )
            .label("reviewed"),
            func.count(active_population.c.student_id)
            .filter(active_population.c.evaluation_status == JobStatus.FAILED)
            .label("failed"),
        )
        .select_from(Assignment)
        .outerjoin(active_population, true())
        .where(Assignment.id == assignment_id)
        .group_by(Assignment.id)
        .cte("summary_aggregate")
    )
    paged_students = (
        select(
            active_population.c.student_id,
            active_population.c.username,
            active_population.c.display_name,
            active_population.c.submission_id,
            active_population.c.latest_version,
            active_population.c.submitted_at,
            active_population.c.evaluation_status,
            active_population.c.evaluation_error_code,
            active_population.c.evaluation_error,
            active_population.c.latest_report_id,
            active_population.c.report_status,
            active_population.c.score,
            active_population.c.grade,
        )
        .select_from(active_population)
        .order_by(
            active_population.c.display_name.asc(),
            active_population.c.username.asc(),
            active_population.c.student_id.asc(),
        )
        .limit(limit)
        .offset(offset)
        .cte("paged_students")
    )

    aggregate_sentinel = select(
        literal(0).label("row_kind"),
        aggregate.c.assignment_id,
        aggregate.c.total_students,
        aggregate.c.submitted_students,
        aggregate.c.latest_submission_at,
        aggregate.c.latest_submission_versions,
        aggregate.c.pending_evaluation,
        aggregate.c.queued,
        aggregate.c.evaluating,
        aggregate.c.pending_review,
        aggregate.c.reviewed,
        aggregate.c.failed,
        cast(null(), User.id.type).label("student_id"),
        cast(null(), User.username.type).label("username"),
        cast(null(), User.display_name.type).label("display_name"),
        cast(null(), Submission.version.type).label("latest_version"),
        cast(null(), Submission.submitted_at.type).label("submitted_at"),
        cast(null(), Submission.id.type).label("submission_id"),
        cast(null(), Submission.assignment_id.type).label("submission_assignment_id"),
        cast(null(), Submission.content_type.type).label("content_type"),
        cast(null(), Submission.content_text.type).label("content_text"),
        cast(null(), Submission.content_json.type).label("content_json"),
        cast(null(), Submission.status.type).label("submission_status"),
        cast(null(), Submission.source.type).label("source"),
        cast(null(), EvaluationJob.status.type).label("evaluation_status"),
        cast(null(), EvaluationJob.error_code.type).label("evaluation_error_code"),
        cast(null(), EvaluationJob.error_message.type).label("evaluation_error"),
        cast(null(), EvaluationReport.id.type).label("latest_report_id"),
        cast(null(), EvaluationReport.review_status.type).label("report_status"),
        cast(null(), EvaluationReport.score.type).label("score"),
        cast(null(), EvaluationReport.grade.type).label("grade"),
    ).select_from(aggregate)
    page_rows = (
        select(
            literal(1).label("row_kind"),
            cast(null(), Assignment.id.type).label("assignment_id"),
            cast(null(), aggregate.c.total_students.type).label("total_students"),
            cast(null(), aggregate.c.submitted_students.type).label("submitted_students"),
            cast(null(), aggregate.c.latest_submission_at.type).label("latest_submission_at"),
            cast(null(), aggregate.c.latest_submission_versions.type).label(
                "latest_submission_versions"
            ),
            cast(null(), aggregate.c.pending_evaluation.type).label("pending_evaluation"),
            cast(null(), aggregate.c.queued.type).label("queued"),
            cast(null(), aggregate.c.evaluating.type).label("evaluating"),
            cast(null(), aggregate.c.pending_review.type).label("pending_review"),
            cast(null(), aggregate.c.reviewed.type).label("reviewed"),
            cast(null(), aggregate.c.failed.type).label("failed"),
            paged_students.c.student_id,
            paged_students.c.username,
            paged_students.c.display_name,
            paged_students.c.latest_version,
            paged_students.c.submitted_at,
            Submission.id.label("submission_id"),
            Submission.assignment_id.label("submission_assignment_id"),
            Submission.content_type,
            Submission.content_text,
            Submission.content_json,
            Submission.status.label("submission_status"),
            Submission.source,
            paged_students.c.evaluation_status,
            paged_students.c.evaluation_error_code,
            paged_students.c.evaluation_error,
            paged_students.c.latest_report_id,
            paged_students.c.report_status,
            paged_students.c.score,
            paged_students.c.grade,
        )
        .select_from(aggregate)
        .join(paged_students, true())
        .outerjoin(Submission, Submission.id == paged_students.c.submission_id)
    )
    summary_rows = aggregate_sentinel.union_all(page_rows).cte("summary_rows")

    return select(summary_rows).order_by(
        summary_rows.c.row_kind.asc(),
        summary_rows.c.display_name.asc().nulls_last(),
        summary_rows.c.username.asc().nulls_last(),
        summary_rows.c.student_id.asc().nulls_last(),
    )


async def build_assignment_summary(
    session: AsyncSession,
    assignment_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> AssignmentSummary | None:
    result = await session.execute(
        _summary_statement(
            assignment_id,
            limit=limit,
            offset=offset,
        )
    )
    rows = result.mappings().all()
    if not rows:
        return None

    students: list[AssignmentSummaryStudent] = []
    for row in rows:
        if row["row_kind"] == 0:
            continue
        latest_submission = None
        if row["submission_id"] is not None:
            latest_submission = SubmissionRead(
                id=row["submission_id"],
                assignment_id=row["submission_assignment_id"],
                student_id=row["student_id"],
                version=row["latest_version"],
                content_type=row["content_type"],
                content_text=row["content_text"],
                content_json=row["content_json"],
                status=row["submission_status"],
                submitted_at=row["submitted_at"],
                source=row["source"],
            )
        students.append(
            AssignmentSummaryStudent(
                student_id=row["student_id"],
                username=row["username"],
                display_name=row["display_name"],
                latest_submission=latest_submission,
                latest_version=row["latest_version"],
                submitted_at=row["submitted_at"],
                evaluation_status=(
                    row["evaluation_status"].value
                    if hasattr(row["evaluation_status"], "value")
                    else row["evaluation_status"]
                ),
                evaluation_error_code=row["evaluation_error_code"],
                report_status=(
                    row["report_status"].value
                    if hasattr(row["report_status"], "value")
                    else row["report_status"]
                ),
                latest_report_id=row["latest_report_id"],
                score=row["score"],
                grade=row["grade"],
                evaluation_error=row["evaluation_error"],
            )
        )

    aggregate = rows[0]
    total_students = aggregate["total_students"]
    submitted_students = aggregate["submitted_students"]
    return AssignmentSummary(
        assignment_id=aggregate["assignment_id"],
        total_students=total_students,
        submitted_students=submitted_students,
        missing_students=total_students - submitted_students,
        latest_submission_at=aggregate["latest_submission_at"],
        latest_submission_versions=aggregate["latest_submission_versions"],
        pending_evaluation=aggregate["pending_evaluation"],
        queued=aggregate["queued"],
        evaluating=aggregate["evaluating"],
        pending_review=aggregate["pending_review"],
        reviewed=aggregate["reviewed"],
        failed=aggregate["failed"],
        limit=limit,
        offset=offset,
        students=students,
    )
