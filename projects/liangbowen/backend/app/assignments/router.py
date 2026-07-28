import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assignments.model import Assignment
from app.assignments.schemas import (
    AssignmentCreate,
    AssignmentListItem,
    AssignmentRead,
    AssignmentStudentRead,
)
from app.assignments.service import (
    InvalidAssignmentTransition,
    close_assignment,
    create_assignment,
    get_student_visible_assignment,
    publish_assignment,
    reopen_assignment,
)
from app.assignments.summary import AssignmentSummary, build_assignment_summary
from app.auth.dependencies import require_roles
from app.db.session import get_session
from app.db.types import AssignmentStatus, Role
from app.users.model import User

router = APIRouter(prefix="/assignments", tags=["assignments"])

VISIBLE_STUDENT_STATUSES = (AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED)
Transition = Callable[[AsyncSession, uuid.UUID], Awaitable[Assignment | None]]


def assignment_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="assignment not found",
    )


async def _get_assignment_or_404(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment:
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        raise assignment_not_found()
    return assignment


async def _transition_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
    operation: Transition,
) -> Assignment:
    try:
        assignment = await operation(session, assignment_id)
    except InvalidAssignmentTransition:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="invalid assignment transition",
        ) from None
    if assignment is None:
        raise assignment_not_found()
    return assignment


@router.post("", response_model=AssignmentRead, status_code=status.HTTP_201_CREATED)
async def create(
    assignment_create: AssignmentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> Assignment:
    return await create_assignment(session, assignment_create, current_user.id)


@router.get("", response_model=list[AssignmentListItem])
async def list_assignments(
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        User,
        Depends(require_roles(Role.TEACHER, Role.STUDENT)),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[AssignmentListItem]:
    statement = select(
        Assignment.id,
        Assignment.code,
        Assignment.title,
        Assignment.due_at,
        Assignment.status,
        Assignment.created_at,
        Assignment.published_at,
    ).order_by(
        Assignment.created_at.desc(),
        Assignment.id.desc(),
    )
    if current_user.role is Role.STUDENT:
        statement = statement.where(Assignment.status.in_(VISIBLE_STUDENT_STATUSES))
    statement = statement.limit(limit).offset(offset)
    result = await session.execute(statement)
    return [AssignmentListItem.model_validate(item) for item in result.mappings().all()]


@router.get(
    "/{assignment_id}/summary",
    response_model=AssignmentSummary,
)
async def get_assignment_summary(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> AssignmentSummary:
    del current_user
    summary = await build_assignment_summary(
        session,
        assignment_id,
        limit=limit,
        offset=offset,
    )
    if summary is None:
        raise assignment_not_found()
    return summary


@router.get(
    "/{assignment_id}",
    response_model=AssignmentRead | AssignmentStudentRead,
)
async def get_assignment(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[
        User,
        Depends(require_roles(Role.TEACHER, Role.STUDENT)),
    ],
) -> AssignmentRead | AssignmentStudentRead:
    if current_user.role is Role.STUDENT:
        assignment = await get_student_visible_assignment(session, assignment_id)
    else:
        assignment = await _get_assignment_or_404(session, assignment_id)
    if assignment is None:
        raise assignment_not_found()
    if current_user.role is Role.STUDENT:
        return AssignmentStudentRead.model_validate(assignment)
    return AssignmentRead.model_validate(assignment)


@router.post("/{assignment_id}/publish", response_model=AssignmentRead)
async def publish(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> Assignment:
    del current_user
    return await _transition_assignment(session, assignment_id, publish_assignment)


@router.post("/{assignment_id}/close", response_model=AssignmentRead)
async def close(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> Assignment:
    del current_user
    return await _transition_assignment(session, assignment_id, close_assignment)


@router.post("/{assignment_id}/reopen", response_model=AssignmentRead)
async def reopen(
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    current_user: Annotated[User, Depends(require_roles(Role.TEACHER))],
) -> Assignment:
    del current_user
    return await _transition_assignment(session, assignment_id, reopen_assignment)
