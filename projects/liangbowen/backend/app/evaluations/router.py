import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_roles
from app.core.config import get_settings
from app.db.session import get_session
from app.db.types import Role
from app.evaluations.api_schemas import (
    AssignmentEvaluationCreate,
    EvaluationBatchRead,
    EvaluationJobRead,
    EvaluationReportRead,
    SubmissionEvaluationCreate,
)
from app.evaluations.engine import EvaluationEngine
from app.evaluations.model import EvaluationJob, EvaluationReport
from app.evaluations.providers.factory import create_evaluation_provider
from app.evaluations.service import (
    EvaluationBatchTooLarge,
    EvaluationService,
    EvaluationSubjectNotFound,
)
from app.evaluations.types import JobReason
from app.submissions.model import Submission
from app.users.model import User

router = APIRouter(tags=["evaluations"])
logger = logging.getLogger(__name__)


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found")


async def _database_role(session: AsyncSession, user_id: uuid.UUID) -> tuple[Role, bool] | None:
    row = (
        await session.execute(select(User.role, User.is_active).where(User.id == user_id))
    ).one_or_none()
    return None if row is None else (row.role, row.is_active)


async def _require_database_teacher(session: AsyncSession, user_id: uuid.UUID) -> None:
    if await _database_role(session, user_id) != (Role.TEACHER, True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")


async def _dispatch(job_id: uuid.UUID) -> None:
    from app.evaluations.worker import enqueue_evaluation_task

    await enqueue_evaluation_task(job_id)


@asynccontextmanager
async def evaluation_service_context(
    session: AsyncSession,
    teacher_id: uuid.UUID,
) -> AsyncIterator[EvaluationService]:
    settings = get_settings()
    provider = create_evaluation_provider(settings)
    service = EvaluationService(
        session,
        EvaluationEngine(provider),
        requested_by=teacher_id,
        dispatch=_dispatch if settings.evaluation_dispatch_enabled else None,
        mock_fixture_key=settings.agent_mock_fixture,
    )
    try:
        yield service
    finally:
        close = getattr(provider, "aclose", None)
        if callable(close):
            try:
                await close()
            except (Exception, SystemExit, MemoryError) as exc:  # noqa: BLE001
                logger.warning(
                    "evaluation provider close failed error_type=%s",
                    type(exc).__name__,
                )


@router.post(
    "/assignments/{assignment_id}/evaluations",
    response_model=EvaluationBatchRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def evaluate_assignment(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    body: Annotated[AssignmentEvaluationCreate | None, Body()] = None,
) -> EvaluationBatchRead:
    del body
    try:
        async with evaluation_service_context(session, current_user.id) as service:
            batch = await service.request_assignment(assignment_id, JobReason.INITIAL)
    except EvaluationSubjectNotFound:
        raise _not_found("assignment") from None
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role"
        ) from None
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="evaluation provider unavailable",
        ) from None
    except EvaluationBatchTooLarge:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="evaluation batch is too large",
        ) from None
    return EvaluationBatchRead(
        batch_id=batch.batch_id,
        queued=batch.queued,
        skipped=batch.skipped,
        job_ids=list(batch.job_ids),
    )


@router.post(
    "/submissions/{submission_id}/evaluations",
    response_model=EvaluationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def evaluate_submission(
    submission_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    body: Annotated[SubmissionEvaluationCreate | None, Body()] = None,
) -> EvaluationJob:
    reason = JobReason.INITIAL if body is None else JobReason(body.reason)
    try:
        async with evaluation_service_context(session, current_user.id) as service:
            return await service.request(submission_id, reason)
    except EvaluationSubjectNotFound:
        raise _not_found("submission") from None
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role"
        ) from None
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="evaluation provider unavailable",
        ) from None


@router.get("/evaluation-jobs/{job_id}", response_model=EvaluationJobRead)
async def get_evaluation_job(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> EvaluationJob:
    await _require_database_teacher(session, current_user.id)
    job = await session.get(EvaluationJob, job_id)
    if job is None:
        raise _not_found("evaluation job")
    return job


@router.get(
    "/submissions/{submission_id}/reports",
    response_model=list[EvaluationReportRead],
)
async def list_reports(
    submission_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[EvaluationReport]:
    database_identity = await _database_role(session, current_user.id)
    if database_identity is None or database_identity[1] is not True:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    role = database_identity[0]
    if role not in {Role.TEACHER, Role.STUDENT}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    conditions = [Submission.id == submission_id]
    if role is Role.STUDENT:
        conditions.append(Submission.student_id == current_user.id)
    visible_submission = await session.scalar(select(Submission.id).where(*conditions))
    if visible_submission is None:
        raise _not_found("submission")
    reports = await session.scalars(
        select(EvaluationReport)
        .where(EvaluationReport.submission_id == submission_id)
        .order_by(EvaluationReport.version.desc(), EvaluationReport.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(reports.all())
