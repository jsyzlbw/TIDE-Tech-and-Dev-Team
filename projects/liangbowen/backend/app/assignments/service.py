import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.assignments.content import validate_assignment_content
from app.assignments.model import ASSIGNMENT_CODE_SEQUENCE, Assignment
from app.assignments.schemas import AssignmentCreate
from app.db.types import AssignmentStatus, Role
from app.users.model import User

VISIBLE_STUDENT_STATUSES = (AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED)
ASSIGNMENT_CODE_ADVISORY_LOCK_KEY = 2_197_203_010_001


class InvalidAssignmentTransition(ValueError):
    """Raised when an assignment lifecycle transition is not allowed."""


async def acquire_assignment_code_lock(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": ASSIGNMENT_CODE_ADVISORY_LOCK_KEY},
    )


async def next_assignment_code(session: AsyncSession) -> str:
    with session.no_autoflush:
        await acquire_assignment_code_lock(session)
        sequence_value = await session.scalar(select(ASSIGNMENT_CODE_SEQUENCE.next_value()))
    if sequence_value is None:
        raise RuntimeError("assignment code sequence returned no value")
    return f"HW-{sequence_value:04d}"


async def create_assignment(
    session: AsyncSession,
    assignment_create: AssignmentCreate,
    teacher_id: uuid.UUID,
) -> Assignment:
    try:
        assignment = await _create_assignment_in_transaction(
            session,
            assignment_create,
            teacher_id,
            status=AssignmentStatus.DRAFT,
            mattermost_channel_id=None,
            published_at=None,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return assignment


async def _create_assignment_in_transaction(
    session: AsyncSession,
    assignment_create: AssignmentCreate,
    teacher_id: uuid.UUID,
    *,
    status: AssignmentStatus,
    mattermost_channel_id: str | None,
    published_at: datetime | None,
) -> Assignment:
    assignment = Assignment(
        code=await next_assignment_code(session),
        status=status,
        created_by=teacher_id,
        published_at=published_at,
        mattermost_channel_id=mattermost_channel_id,
        **assignment_create.model_dump(),
    )
    session.add(assignment)
    await session.flush([assignment])
    await session.refresh(assignment)
    return assignment


async def create_published_assignment_in_transaction(
    session: AsyncSession,
    assignment_create: AssignmentCreate,
    teacher_id: uuid.UUID,
    mattermost_channel_id: str,
) -> Assignment:
    """Create a published assignment without owning the caller's transaction."""
    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if not isinstance(assignment_create, AssignmentCreate):
        raise TypeError("assignment_create must be an AssignmentCreate")
    if not isinstance(teacher_id, uuid.UUID):
        raise TypeError("teacher_id must be a UUID")
    if (
        type(mattermost_channel_id) is not str
        or not mattermost_channel_id
        or mattermost_channel_id != mattermost_channel_id.strip()
        or "\0" in mattermost_channel_id
        or len(mattermost_channel_id.encode("utf-8")) > 128
    ):
        raise ValueError("mattermost channel id is invalid")
    actor = await session.scalar(
        select(User.id).where(
            User.id == teacher_id,
            User.role == Role.TEACHER,
            User.is_active.is_(True),
        )
    )
    if actor is None:
        raise PermissionError("active teacher required")
    published_at = datetime.now(UTC)
    if assignment_create.due_at <= published_at:
        raise InvalidAssignmentTransition("cannot publish assignment after its deadline")
    return await _create_assignment_in_transaction(
        session,
        assignment_create,
        teacher_id,
        status=AssignmentStatus.PUBLISHED,
        mattermost_channel_id=mattermost_channel_id,
        published_at=published_at,
    )


async def get_student_visible_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment | None:
    return await session.scalar(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.status.in_(VISIBLE_STUDENT_STATUSES),
        )
    )


async def lock_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment | None:
    return await session.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _require_status(
    assignment: Assignment,
    expected: AssignmentStatus,
    operation: str,
) -> None:
    if assignment.status is not expected:
        raise InvalidAssignmentTransition(
            f"cannot {operation} assignment with status {assignment.status.value}"
        )


def _require_future_deadline(assignment: Assignment, operation: str) -> datetime:
    now = datetime.now(UTC)
    if assignment.due_at <= now:
        raise InvalidAssignmentTransition(f"cannot {operation} assignment after its deadline")
    return now


async def _commit_transition(
    session: AsyncSession,
    assignment: Assignment,
    new_status: AssignmentStatus,
    *,
    published_at: datetime | None = None,
    replace_published_at: bool = False,
) -> Assignment:
    previous_status = assignment.status
    previous_published_at = assignment.published_at
    assignment.status = new_status
    if replace_published_at:
        assignment.published_at = published_at
    try:
        await session.flush([assignment])
        await session.refresh(assignment)
        await session.commit()
    except Exception:
        await session.rollback()
        set_committed_value(assignment, "status", previous_status)
        set_committed_value(assignment, "published_at", previous_published_at)
        raise
    return assignment


async def publish_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment | None:
    assignment = await lock_assignment(session, assignment_id)
    if assignment is None:
        await session.rollback()
        return None
    try:
        validate_assignment_content(
            title=assignment.title,
            question=assignment.question,
            rubric=assignment.rubric,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        await session.rollback()
        raise InvalidAssignmentTransition("assignment content cannot be evaluated") from None
    try:
        _require_status(assignment, AssignmentStatus.DRAFT, "publish")
        published_at = _require_future_deadline(assignment, "publish")
    except InvalidAssignmentTransition:
        await session.rollback()
        raise

    return await _commit_transition(
        session,
        assignment,
        AssignmentStatus.PUBLISHED,
        published_at=published_at,
        replace_published_at=True,
    )


async def close_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment | None:
    assignment = await lock_assignment(session, assignment_id)
    if assignment is None:
        await session.rollback()
        return None
    try:
        _require_status(assignment, AssignmentStatus.PUBLISHED, "close")
    except InvalidAssignmentTransition:
        await session.rollback()
        raise

    return await _commit_transition(session, assignment, AssignmentStatus.CLOSED)


async def reopen_assignment(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> Assignment | None:
    assignment = await lock_assignment(session, assignment_id)
    if assignment is None:
        await session.rollback()
        return None
    try:
        _require_status(assignment, AssignmentStatus.CLOSED, "reopen")
        _require_future_deadline(assignment, "reopen")
    except InvalidAssignmentTransition:
        await session.rollback()
        raise

    return await _commit_transition(session, assignment, AssignmentStatus.PUBLISHED)
