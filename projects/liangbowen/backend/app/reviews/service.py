from __future__ import annotations

import copy
import uuid

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.migration_gate import migration_gated_sessionmaker
from app.db.types import Role, grade_for_score
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.service import EvaluationService, EvaluationSubjectNotFound
from app.evaluations.types import JobReason, JobStatus, ReportOrigin, ReviewStatus
from app.reviews.model import ReviewAction
from app.reviews.schemas import ConfirmRequest, ReevaluateRequest, ReportPatch
from app.reviews.types import ReviewActionType
from app.submissions.model import Submission
from app.users.model import User


class ReviewNotFound(LookupError):
    """The report is absent without revealing another user's data."""


class ReviewForbidden(PermissionError):
    """The actor is not an active teacher."""


class ReviewConflict(RuntimeError):
    """The requested transition is incompatible with current review state."""


class ReviewValidation(ValueError):
    """A review request is valid JSON but violates a business rule."""


class ReviewService:
    """Own teacher-review transactions without touching caller-pending state."""

    def __init__(self, session: AsyncSession) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("session must be an AsyncSession")
        bind = session.bind
        if not isinstance(bind, AsyncEngine):
            raise TypeError("session must be bound to an AsyncEngine")
        self._database_engine = bind
        self._session_factory = migration_gated_sessionmaker(bind, expire_on_commit=False)

    def _require_caller_session(self, session: AsyncSession) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("session must be an AsyncSession")
        if session.bind is not self._database_engine:
            raise ValueError("session must use the review service database engine")

    @staticmethod
    async def _require_teacher(session: AsyncSession, teacher_id: uuid.UUID) -> User:
        if not isinstance(teacher_id, uuid.UUID):
            raise TypeError("teacher_id must be a UUID")
        teacher = await session.scalar(
            select(User).where(
                User.id == teacher_id,
                User.role == Role.TEACHER,
                User.is_active.is_(True),
            )
        )
        if teacher is None:
            raise ReviewForbidden("active teacher role required")
        return teacher

    @staticmethod
    async def _lock_report(
        session: AsyncSession,
        report_id: uuid.UUID,
    ) -> EvaluationReport:
        if not isinstance(report_id, uuid.UUID):
            raise TypeError("report_id must be a UUID")
        submission_id = await session.scalar(
            select(EvaluationReport.submission_id).where(EvaluationReport.id == report_id)
        )
        if submission_id is None:
            raise ReviewNotFound("report not found")
        submission = await session.scalar(
            select(Submission).where(Submission.id == submission_id).with_for_update()
        )
        if submission is None:
            raise ReviewNotFound("report not found")
        report = await session.scalar(
            select(EvaluationReport)
            .where(EvaluationReport.id == report_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if report is None:
            raise ReviewNotFound("report not found")
        return report

    @staticmethod
    async def _require_current_report(
        session: AsyncSession,
        report: EvaluationReport,
    ) -> None:
        latest_version = await session.scalar(
            select(func.max(EvaluationReport.version)).where(
                EvaluationReport.submission_id == report.submission_id
            )
        )
        if report.version != latest_version or report.review_status is ReviewStatus.SUPERSEDED:
            raise ReviewConflict("report is not the current review version")

    @classmethod
    async def _lock_current_report(
        cls,
        session: AsyncSession,
        report_id: uuid.UUID,
    ) -> EvaluationReport:
        report = await cls._lock_report(session, report_id)
        await cls._require_current_report(session, report)
        return report

    @staticmethod
    async def _reject_pending_reevaluation(
        session: AsyncSession,
        report_id: uuid.UUID,
    ) -> None:
        pending = await session.scalar(
            select(EvaluationJob.id).where(
                EvaluationJob.source_report_id == report_id,
                EvaluationJob.reason == JobReason.MANUAL_RETRY,
                EvaluationJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
        )
        if pending is not None:
            raise ReviewConflict("report has a pending re-evaluation")

    async def confirm(
        self,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ConfirmRequest,
    ) -> EvaluationReport:
        async with self._session_factory() as session, session.begin():
            report = await self.confirm_in_transaction(session, report_id, teacher_id, data)
        return report

    async def confirm_in_transaction(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ConfirmRequest,
    ) -> EvaluationReport:
        self._require_caller_session(session)
        try:
            validated = ConfirmRequest.model_validate(data.model_dump())
        except (AttributeError, ValidationError) as exc:
            raise ValueError("invalid confirmation request") from exc
        await self._require_teacher(session, teacher_id)
        report = await self._lock_current_report(session, report_id)
        existing = await session.scalar(
            select(ReviewAction).where(
                ReviewAction.report_id == report.id,
                ReviewAction.action == ReviewActionType.CONFIRM,
            )
        )
        if existing is not None:
            if existing.teacher_id == teacher_id and report.review_status is ReviewStatus.CONFIRMED:
                return report
            raise ReviewConflict("report was already confirmed")
        await self._reject_pending_reevaluation(session, report.id)
        if report.review_status is not ReviewStatus.PROPOSED:
            raise ReviewConflict("only a proposed report can be confirmed")
        report.review_status = ReviewStatus.CONFIRMED
        await session.flush([report])
        session.add(
            ReviewAction(
                report_id=report.id,
                teacher_id=teacher_id,
                action=ReviewActionType.CONFIRM,
                changes={
                    "review_status": {
                        "before": ReviewStatus.PROPOSED.value,
                        "after": ReviewStatus.CONFIRMED.value,
                    }
                },
                comment=validated.comment,
            )
        )
        return report

    async def require_current_report_in_transaction(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
    ) -> EvaluationReport:
        self._require_caller_session(session)
        await self._require_teacher(session, teacher_id)
        return await self._lock_current_report(session, report_id)

    async def modify(
        self,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ReportPatch,
    ) -> EvaluationReport:
        try:
            validated = ReportPatch.model_validate(data.model_dump(exclude_unset=True))
        except (AttributeError, ValidationError) as exc:
            raise ValueError("invalid report patch") from exc

        async with self._session_factory() as session, session.begin():
            await self._require_teacher(session, teacher_id)
            source = await self._lock_current_report(session, report_id)
            await self._reject_pending_reevaluation(session, source.id)
            if source.review_status is not ReviewStatus.PROPOSED:
                raise ReviewConflict("only a proposed report can be modified")

            patch = validated.model_dump(exclude_unset=True, mode="json")
            comment = str(patch.pop("comment", ""))
            if (
                "score" in patch
                and abs(int(patch["score"]) - source.score) >= 10
                and not comment.strip()
            ):
                raise ReviewValidation("comment is required when score changes by 10 or more")
            values: dict[str, object] = {
                "completeness": copy.deepcopy(source.completeness),
                "correctness": copy.deepcopy(source.correctness),
                "major_issues": copy.deepcopy(source.major_issues),
                "suggestions": copy.deepcopy(source.suggestions),
                "score": source.score,
                "limitations": copy.deepcopy(source.limitations),
            }
            values.update(patch)
            score = int(values["score"])
            grade = grade_for_score(score)
            modified = EvaluationReport(
                submission_id=source.submission_id,
                job_id=None,
                source_report_id=source.id,
                origin=ReportOrigin.TEACHER,
                created_by_teacher_id=teacher_id,
                version=source.version + 1,
                schema_version=source.schema_version,
                completeness=values["completeness"],
                correctness=values["correctness"],
                major_issues=values["major_issues"],
                suggestions=values["suggestions"],
                score=score,
                grade=grade,
                confidence=source.confidence,
                limitations=values["limitations"],
                raw_model_output=None,
                validation_status=source.validation_status,
                review_status=ReviewStatus.MODIFIED,
            )
            session.add(modified)
            await session.flush([modified])

            changes: dict[str, object] = {
                "review_status": {
                    "before": ReviewStatus.PROPOSED.value,
                    "after": ReviewStatus.SUPERSEDED.value,
                },
                "result_report_id": {"before": None, "after": str(modified.id)},
            }
            report_attribute = {
                "completeness": "completeness",
                "correctness": "correctness",
                "major_issues": "major_issues",
                "suggestions": "suggestions",
                "score": "score",
                "limitations": "limitations",
            }
            for field_name, attribute_name in report_attribute.items():
                before = copy.deepcopy(getattr(source, attribute_name))
                after = copy.deepcopy(values[field_name])
                if before != after:
                    changes[field_name] = {
                        "before": before,
                        "after": after,
                    }
            if source.grade != grade:
                changes["grade"] = {
                    "before": source.grade.value,
                    "after": grade.value,
                }
            if comment:
                changes["comment"] = {"before": "", "after": comment}

            source.review_status = ReviewStatus.SUPERSEDED
            await session.flush([source])
            session.add(
                ReviewAction(
                    report_id=source.id,
                    teacher_id=teacher_id,
                    action=ReviewActionType.MODIFY,
                    changes=changes,
                    comment=comment,
                )
            )
        return modified

    async def reevaluate(
        self,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ReevaluateRequest,
        evaluation: EvaluationService,
    ) -> EvaluationJob:
        async with self._session_factory() as session, session.begin():
            job, created_action = await self._reevaluate_in_transaction(
                session,
                report_id,
                teacher_id,
                data,
                evaluation,
            )

        if created_action:
            await evaluation.redrive_after_commit((uuid.UUID(str(job.id)),))
        return job

    async def reevaluate_in_transaction(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ReevaluateRequest,
        evaluation: EvaluationService,
    ) -> EvaluationJob:
        job, _ = await self._reevaluate_in_transaction(
            session,
            report_id,
            teacher_id,
            data,
            evaluation,
        )
        return job

    async def _reevaluate_in_transaction(
        self,
        session: AsyncSession,
        report_id: uuid.UUID,
        teacher_id: uuid.UUID,
        data: ReevaluateRequest,
        evaluation: EvaluationService,
    ) -> tuple[EvaluationJob, bool]:
        self._require_caller_session(session)
        try:
            validated = ReevaluateRequest.model_validate(data.model_dump())
        except (AttributeError, ValidationError) as exc:
            raise ValueError("invalid re-evaluation request") from exc
        if not isinstance(evaluation, EvaluationService):
            raise TypeError("evaluation must be an EvaluationService")
        if evaluation.requested_by != teacher_id:
            raise ReviewForbidden("evaluation requester must match the reviewing teacher")

        await self._require_teacher(session, teacher_id)
        source = await self._lock_report(session, report_id)
        existing = await session.scalar(
            select(ReviewAction)
            .where(
                ReviewAction.report_id == source.id,
                ReviewAction.action == ReviewActionType.REEVALUATE,
            )
            .order_by(ReviewAction.created_at.desc(), ReviewAction.id.desc())
            .limit(1)
        )
        if existing is not None:
            raw_job_id = existing.changes.get("evaluation_job_id", {}).get("after")
            try:
                existing_job_id = uuid.UUID(str(raw_job_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ReviewConflict("re-evaluation evidence is invalid") from exc
            job = await session.get(EvaluationJob, existing_job_id)
            if job is None or job.requested_by != teacher_id or job.source_report_id != source.id:
                raise ReviewConflict("re-evaluation evidence is invalid")
            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED}:
                return job, False

        await self._require_current_report(session, source)
        if source.review_status not in {
            ReviewStatus.PROPOSED,
            ReviewStatus.MODIFIED,
            ReviewStatus.CONFIRMED,
        }:
            raise ReviewConflict("report cannot be re-evaluated")
        try:
            job = await evaluation.enqueue_in_transaction(
                session,
                source.submission_id,
                JobReason.MANUAL_RETRY,
                source_report_id=source.id,
            )
        except EvaluationSubjectNotFound as exc:
            raise ReviewConflict("report cannot be re-evaluated") from exc
        session.add(
            ReviewAction(
                report_id=source.id,
                teacher_id=teacher_id,
                action=ReviewActionType.REEVALUATE,
                changes={
                    "evaluation_job_id": {"before": None, "after": str(job.id)},
                    "source_review_status": {
                        "before": source.review_status.value,
                        "after": source.review_status.value,
                    },
                },
                comment=validated.comment,
            )
        )
        return job, True
