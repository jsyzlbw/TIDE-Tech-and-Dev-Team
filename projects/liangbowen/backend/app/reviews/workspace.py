from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from app.assignments.model import Assignment
from app.db.migration_gate import migration_gated_sessionmaker
from app.db.types import Grade, Role
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.types import JobReason, ReportOrigin, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.schemas import (
    MAX_WORKSPACE_GRADING_NOTES_CHARACTERS,
    MAX_WORKSPACE_RUBRIC_POINT_CHARACTERS,
    MAX_WORKSPACE_RUBRIC_POINTS,
)
from app.submissions.model import Submission
from app.users.model import User

from .service import ReviewForbidden, ReviewNotFound

TIMELINE_LIMIT = 100


def _empty_workspace_rubric() -> dict[str, object]:
    return {"required_points": [], "grading_notes": ""}


def project_workspace_rubric(value: object) -> dict[str, object]:
    """Project an untrusted legacy rubric onto the bounded workspace contract."""
    if type(value) is not dict:
        return _empty_workspace_rubric()

    raw_points = value.get("required_points")
    points: list[str] = []
    if type(raw_points) is list and len(raw_points) <= MAX_WORKSPACE_RUBRIC_POINTS:
        for raw_point in raw_points:
            if not isinstance(raw_point, str):
                points = []
                break
            point = raw_point.strip()
            if not point or len(point) > MAX_WORKSPACE_RUBRIC_POINT_CHARACTERS:
                points = []
                break
            points.append(point)

    raw_notes = value.get("grading_notes")
    notes = raw_notes.strip() if isinstance(raw_notes, str) else ""
    if len(notes) > MAX_WORKSPACE_GRADING_NOTES_CHARACTERS:
        notes = ""
    return {"required_points": points, "grading_notes": notes}


@dataclass(frozen=True)
class ReportTimelineItem:
    report: EvaluationReport
    creator: User | None
    action: ReviewAction | None
    action_teacher: User | None


@dataclass(frozen=True)
class ReportTimelineSummary:
    id: uuid.UUID
    version: int
    origin: ReportOrigin
    review_status: ReviewStatus
    score: int
    grade: Grade
    created_at: datetime
    creator: User | None
    action: ReviewAction | None
    action_teacher: User | None


@dataclass(frozen=True)
class ReportWorkspaceSnapshot:
    assignment: Assignment
    rubric: dict[str, object]
    submission: Submission
    student: User
    selected_item: ReportTimelineItem
    current_report_id: uuid.UUID
    timeline: tuple[ReportTimelineSummary, ...]
    timeline_total: int
    reevaluation_job: EvaluationJob | None


class ReviewWorkspaceService:
    """Read review data from one repeatable-read database snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        bind = session.bind
        if not isinstance(bind, AsyncEngine):
            raise TypeError("session must be bound to an AsyncEngine")
        snapshot_engine = bind.execution_options(
            isolation_level="REPEATABLE READ",
            postgresql_readonly=True,
        )
        self._session_factory = migration_gated_sessionmaker(
            snapshot_engine,
            expire_on_commit=False,
        )

    @staticmethod
    async def _require_teacher(session: AsyncSession, teacher_id: uuid.UUID) -> None:
        teacher = await session.scalar(
            select(User.id).where(
                User.id == teacher_id,
                User.role == Role.TEACHER,
                User.is_active.is_(True),
            )
        )
        if teacher is None:
            raise ReviewForbidden("active teacher role required")

    async def workspace(
        self,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
    ) -> ReportWorkspaceSnapshot:
        async with self._session_factory() as session, session.begin():
            await self._require_teacher(session, teacher_id)
            selected_creator = aliased(User)
            row = (
                await session.execute(
                    select(EvaluationReport, Submission, Assignment, User, selected_creator)
                    .join(Submission, Submission.id == EvaluationReport.submission_id)
                    .join(Assignment, Assignment.id == Submission.assignment_id)
                    .join(User, User.id == Submission.student_id)
                    .outerjoin(
                        selected_creator,
                        selected_creator.id == EvaluationReport.created_by_teacher_id,
                    )
                    .where(EvaluationReport.id == report_id)
                )
            ).one_or_none()
            if row is None:
                raise ReviewNotFound("report not found")
            selected, submission, assignment, student, selected_report_creator = row

            creator = aliased(User)
            report_rows = (
                await session.execute(
                    select(
                        EvaluationReport.id,
                        EvaluationReport.version,
                        EvaluationReport.origin,
                        EvaluationReport.review_status,
                        EvaluationReport.score,
                        EvaluationReport.grade,
                        EvaluationReport.created_at,
                        creator,
                    )
                    .outerjoin(creator, creator.id == EvaluationReport.created_by_teacher_id)
                    .where(EvaluationReport.submission_id == submission.id)
                    .order_by(EvaluationReport.version.desc(), EvaluationReport.id.desc())
                    .limit(TIMELINE_LIMIT)
                )
            ).all()
            report_ids = list({row.id for row in report_rows} | {selected.id})
            timeline_total = int(
                await session.scalar(
                    select(func.count(EvaluationReport.id)).where(
                        EvaluationReport.submission_id == submission.id
                    )
                )
                or 0
            )

            action_teacher = aliased(User)
            action_rows = (
                await session.execute(
                    select(ReviewAction, action_teacher)
                    .join(action_teacher, action_teacher.id == ReviewAction.teacher_id)
                    .where(ReviewAction.report_id.in_(report_ids))
                    .order_by(
                        ReviewAction.report_id,
                        ReviewAction.created_at.desc(),
                        ReviewAction.id.desc(),
                    )
                )
            ).all()
            latest_actions: dict[uuid.UUID, tuple[ReviewAction, User]] = {}
            for action, teacher in action_rows:
                latest_actions.setdefault(action.report_id, (action, teacher))

            timeline = tuple(
                ReportTimelineSummary(
                    id=row.id,
                    version=row.version,
                    origin=row.origin,
                    review_status=row.review_status,
                    score=row.score,
                    grade=row.grade,
                    created_at=row.created_at,
                    creator=row[-1],
                    action=latest_actions.get(row.id, (None, None))[0],
                    action_teacher=latest_actions.get(row.id, (None, None))[1],
                )
                for row in report_rows
            )
            selected_action, selected_action_teacher = latest_actions.get(
                selected.id,
                (None, None),
            )
            selected_item = ReportTimelineItem(
                report=selected,
                creator=selected_report_creator,
                action=selected_action,
                action_teacher=selected_action_teacher,
            )
            current = next(
                (item for item in timeline if item.review_status is not ReviewStatus.SUPERSEDED),
                None,
            )
            if current is None:
                raise ReviewNotFound("current report not found")

            latest_job = await session.scalar(
                select(EvaluationJob)
                .where(
                    EvaluationJob.submission_id == submission.id,
                    EvaluationJob.reason == JobReason.MANUAL_RETRY,
                )
                .order_by(EvaluationJob.queued_at.desc(), EvaluationJob.id.desc())
                .limit(1)
            )
            return ReportWorkspaceSnapshot(
                assignment=assignment,
                rubric=project_workspace_rubric(assignment.rubric),
                submission=submission,
                student=student,
                selected_item=selected_item,
                current_report_id=current.id,
                timeline=timeline,
                timeline_total=timeline_total,
                reevaluation_job=latest_job,
            )

    async def raw_output(self, report_id: uuid.UUID, teacher_id: uuid.UUID) -> str | None:
        async with self._session_factory() as session, session.begin():
            await self._require_teacher(session, teacher_id)
            row = (
                await session.execute(
                    select(EvaluationReport.id, EvaluationReport.raw_model_output).where(
                        EvaluationReport.id == report_id
                    )
                )
            ).one_or_none()
            if row is None:
                raise ReviewNotFound("report not found")
            return row.raw_model_output
