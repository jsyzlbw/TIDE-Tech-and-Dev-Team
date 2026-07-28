import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.assignments.service import get_student_visible_assignment
from app.auth.dependencies import get_current_user, require_roles
from app.db.session import get_session
from app.db.types import Role, SubmissionSource
from app.submissions.model import Submission
from app.submissions.schemas import SubmissionCreate, SubmissionRead
from app.submissions.service import (
    SubmissionAssignmentNotFound,
    SubmissionClosed,
    SubmissionTooLong,
    create_submission,
)
from app.users.model import User

router = APIRouter(tags=["submissions"])


def assignment_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="assignment not found",
    )


def submission_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="submission not found",
    )


async def _get_assignment_or_404(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment:
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        await session.rollback()
        raise assignment_not_found()
    return assignment


async def _get_student_visible_assignment_or_404(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment:
    assignment = await get_student_visible_assignment(session, assignment_id)
    if assignment is None:
        raise assignment_not_found()
    return assignment


@router.post(
    "/assignments/{assignment_id}/submissions",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit(
    assignment_id: uuid.UUID,
    data: SubmissionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.STUDENT))],
) -> Submission:
    try:
        assignment = await _get_student_visible_assignment_or_404(session, assignment_id)
        submission = await create_submission(
            session,
            assignment,
            current_user.id,
            data,
            SubmissionSource.WEB,
        )
        await session.commit()
        return submission
    except HTTPException:
        await session.rollback()
        raise
    except SubmissionAssignmentNotFound:
        await session.rollback()
        raise assignment_not_found() from None
    except SubmissionClosed:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="assignment is not accepting submissions",
        ) from None
    except SubmissionTooLong:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="submission content is too long",
        ) from None
    except BaseException:
        await session.rollback()
        raise


@router.get(
    "/assignments/{assignment_id}/submissions/me",
    response_model=list[SubmissionRead],
)
async def list_my_submissions(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.STUDENT))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[Submission]:
    await _get_student_visible_assignment_or_404(session, assignment_id)
    result = await session.execute(
        select(Submission)
        .where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == current_user.id,
        )
        .order_by(Submission.version.desc(), Submission.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.get(
    "/assignments/{assignment_id}/submissions",
    response_model=list[SubmissionRead],
)
async def list_latest_submissions(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        User,
        Depends(require_roles(Role.TEACHER)),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[Submission]:
    del current_user
    await _get_assignment_or_404(session, assignment_id)
    latest_versions = (
        select(
            Submission.assignment_id.label("assignment_id"),
            Submission.student_id.label("student_id"),
            func.max(Submission.version).label("version"),
        )
        .where(Submission.assignment_id == assignment_id)
        .group_by(Submission.assignment_id, Submission.student_id)
        .subquery()
    )
    result = await session.execute(
        select(Submission)
        .join(
            latest_versions,
            and_(
                Submission.assignment_id == latest_versions.c.assignment_id,
                Submission.student_id == latest_versions.c.student_id,
                Submission.version == latest_versions.c.version,
            ),
        )
        .where(Submission.assignment_id == assignment_id)
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionRead,
)
async def get_submission(
    submission_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Submission:
    if current_user.role not in {Role.STUDENT, Role.TEACHER}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role",
        )
    submission = await session.get(Submission, submission_id)
    if submission is None or (
        current_user.role is Role.STUDENT and submission.student_id != current_user.id
    ):
        raise submission_not_found()
    return submission
